"""CLI entry point for the ``calfkit-router`` deployment.

Hosts the singleton built-in router agent plus its fan-out consumer on
a single calfkit :class:`Worker`. Standalone process — separate from
the bridge, the agents runner, and the tools runner — so the router's
lifecycle is decoupled from every other deployment.

Two nodes co-tenant on this Worker:

* The router :class:`~calfkit.nodes.Agent` (subscribed to
  ``discord.ambient.in``, publishes :class:`RoutingDecision` to
  ``routing.decisions``).
* The fan-out :class:`~calfkit.nodes.ConsumerNodeDef` (subscribed to
  ``routing.decisions``, publishes synthesized wires to
  ``bridge.synthesized.in``).

The runner needs only Kafka + an LLM API key — no Discord access.
Specifically:

* :class:`calfkit.client.Client` — connected with a private reply
  topic distinct from the bridge's ``discord.outbox`` so the router's
  ReturnCall on its ``publish_topic`` doesn't get echoed to the bridge
  outbox consumer. The router's reply ALSO goes to
  ``_calf.ambient.callback-discard`` (the bridge ingress's discard
  topic for ambient invocations); we use our own reply topic so the
  client's reply dispatcher has somewhere to listen.

The factory's :class:`DiscordPersonaSender` parameter is unused on the
router build path, so we pass ``None`` here rather than instantiating
one (which would force a ``DISCORD_BOT_TOKEN`` requirement we
otherwise don't have). The factory's constructor accepts ``None`` for
this parameter — see :class:`AgentFactory.__init__`.

Run::

    uv run calfkit-router
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from calfkit.client import Client
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.router.definition import ROUTER_AGENT_ID, build_router_definition
from calfkit_organization.router.fanout import build_fanout_consumer

logger = logging.getLogger(__name__)


class BootstrapError(RuntimeError):
    """A recoverable router-startup failure that produces a clean CLI exit.

    Mirrors :class:`calfkit_organization.agents.runner.BootstrapError`:
    raised when a required external resource (e.g. upstream Codex prompts)
    cannot be fetched at boot time. :func:`main` converts this into
    ``SystemExit(message)`` so operators see the actionable error on
    stderr without a traceback.
    """

_REPLY_TOPIC = "calfkit.router.reply"
"""Named reply topic for the router client. ``Client.connect``
requires a reply topic; no envelope is ever actually delivered here
because the router process never makes an outgoing call that
returns to itself (the fan-out's ``invoke_node_with_metadata`` calls
target ``bridge.synthesized.in``, and the synthesized-in consumer
neither replies nor produces a ReturnCall on the router's
correlation_id). Picking a unique name keeps the dispatcher
subscription off any topic the bridge consumes."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="calfkit-router",
        description="Run the calfkit built-in routing agent.",
    )
    return parser.parse_args(argv)


async def _run_worker(worker: Worker) -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Mirrors :func:`calfkit_organization.tools.runner._run_worker`
    exactly; the agents-runner variant
    (:func:`calfkit_organization.agents.runner._run_worker`) is
    similar but takes an additional ``num_agents`` kwarg for its
    boot logging. Extract the shared core to a
    ``calfkit_organization/runtime.py`` module when the
    near-duplication starts itching — see follow-up tracked in
    ``docs/ambient-routing.md``'s "Out of scope" section.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    worker_task = asyncio.create_task(worker.run())
    stop_task = asyncio.create_task(stop.wait())
    worker_exc: BaseException | None = None
    try:
        await asyncio.wait(
            {worker_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task.done() and not stop_task.done():
            worker_exc = worker_task.exception()
            if worker_exc is not None:
                logger.error("worker crashed during runtime; exiting non-zero", exc_info=worker_exc)
            else:
                # Same "clean return without signal" handling as
                # tools/runner.py: synthesize a RuntimeError so
                # supervisors restart us.
                worker_exc = RuntimeError(
                    "worker.run() returned unexpectedly without a shutdown signal"
                )
                logger.error("%s; exiting non-zero", worker_exc)
        else:
            logger.info("shutdown signal received, draining router worker")
    finally:
        for t in (worker_task, stop_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(worker_task, stop_task, return_exceptions=True)

    if worker_exc is not None:
        raise worker_exc


def _build_router_nodes(
    factory: AgentFactory,
    client: Client,
    definition: AgentDefinition | None = None,
) -> list:
    """Construct the router agent + fan-out consumer.

    Extracted from ``_amain`` so the boot wiring can be exercised in
    tests without standing up Kafka.

    ``definition`` is an optional pre-built router definition; when
    ``None`` (the default, used by tests), :func:`build_router_definition`
    is called internally. ``_amain`` passes a pre-built definition so it
    can inspect ``definition.provider`` before this call and prewarm the
    Codex prompt cache if needed (avoiding a re-read of the router env
    vars on the hot path).
    """
    if definition is None:
        definition = build_router_definition()
    router_node = factory.build_node(definition, state=None, store=None)
    fanout_node = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
    return [router_node, fanout_node]


async def _amain() -> None:
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    definition = build_router_definition()
    needs_codex = definition.provider == "openai-codex"
    if needs_codex:
        # Lazy import: keeps authlib + openhands-sdk auth machinery out of
        # the import graph for deployments that don't use Codex subscription.
        from calfkit_organization.providers.codex import (
            CodexPromptsUnavailableError,
            prewarm_codex_prompts,
        )
        try:
            await prewarm_codex_prompts()
        except CodexPromptsUnavailableError as exc:
            raise BootstrapError(
                f"openai-codex router declared but upstream Codex prompts "
                f"are unavailable: {exc}. Check internet connectivity, or "
                f"run once: uv run calfkit-auth codex refresh-prompts"
            ) from exc

    async with Client.connect(server_urls, reply_topic=_REPLY_TOPIC) as client:
        # Eagerly start the broker so the reply dispatcher is live
        # before the worker's first inbound envelope. Mirrors the
        # bridge's eager start.
        if not client.broker.running:
            await client.broker.start()

        # The factory's persona_sender is unused on the router build
        # path; ``None`` is the explicit "I don't need Discord" call
        # site signal. See module docstring.
        factory = AgentFactory(persona_sender=None, calfkit_client=client)
        nodes = _build_router_nodes(factory, client, definition=definition)

        worker = Worker(client, nodes)
        logger.info(
            "starting calfkit-router worker broker=%s reply_topic=%s nodes=%s",
            server_urls,
            _REPLY_TOPIC,
            [n.node_id for n in nodes],
        )
        await _run_worker(worker)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    _parse_args()
    try:
        asyncio.run(_amain())
    except BootstrapError as e:
        raise SystemExit(str(e)) from None
    except KeyboardInterrupt:
        logger.info("calfkit-router shutting down")


if __name__ == "__main__":
    main()
