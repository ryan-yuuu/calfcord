"""CLI entry point for the ``calfkit-tools`` deployment.

Hosts every :class:`ToolNodeDef` registered in :data:`TOOL_REGISTRY` on a
single calfkit :class:`Worker`. Standalone process — separate from the
bridge and the agent runner — so the tool lifecycle is decoupled from
both, matching calfkit's tool-as-deployment model.

Dependencies wired at boot and injected into each tool module:

* :class:`AgentRegistry` — loaded from ``agents/*.md`` so the tool can
  validate caller/target identities and resolve personas.
* :class:`DiscordSender` — REST-only client used by
  :class:`A2AChannelResolver` to discover/create the ``a2a-{x}-{y}``
  channels.
* :class:`DiscordPersonaSender` — webhook-based projector that posts
  request/response audit entries under each agent's persona.
* :class:`calfkit.client.Client` — connected with a private reply topic
  distinct from the bridge's ``discord.outbox``, so target-agent replies
  route back to this process and are NOT consumed by the bridge's
  outbox-to-Discord poster.

Run::

    uv run calfkit-tools
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from calfkit.client import Client
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfkit_organization.bridge.egress import A2AChannelResolver
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.discord.persona import DiscordPersonaSender
from calfkit_organization.discord.sender import DiscordSender
from calfkit_organization.discord.settings import DiscordSettings
from calfkit_organization.tools import TOOL_REGISTRY, private_chat

logger = logging.getLogger(__name__)

_AGENTS_DIR_ENV = "CALFKIT_AGENTS_DIR"
_AGENTS_DIR_DEFAULT = "agents"
_REPLY_TOPIC = "calfkit.tools.reply"
"""Named reply topic for the tools client. Must differ from the bridge's
``discord.outbox`` so target-agent ReturnCalls route here, not to the
bridge's outbox consumer (which would project them to Discord twice)."""
_TIMEOUT_ENV = "CALFKIT_TOOLS_TIMEOUT_SECONDS"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="calfkit-tools",
        description="Run the calfkit tools process (private_chat etc.).",
    )
    return parser.parse_args(argv)


def _resolve_timeout() -> float:
    """Read ``CALFKIT_TOOLS_TIMEOUT_SECONDS`` or fall back to the default."""
    raw = os.getenv(_TIMEOUT_ENV)
    if raw is None:
        return private_chat.DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as e:
        raise SystemExit(f"{_TIMEOUT_ENV} must be a number, got {raw!r}") from e
    if value <= 0:
        raise SystemExit(f"{_TIMEOUT_ENV} must be positive, got {value}")
    return value


async def _run_worker(worker: Worker) -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Mirrors :func:`calfkit_organization.agents.runner._run_worker` so the
    shutdown behavior is consistent across runners.
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
                logger.warning("worker.run() returned without an exception; exiting")
        else:
            logger.info("shutdown signal received, draining tools worker")
    finally:
        for t in (worker_task, stop_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(worker_task, stop_task, return_exceptions=True)

    if worker_exc is not None:
        raise worker_exc


async def _amain() -> None:
    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise SystemExit(
            "DISCORD_GUILD_ID is required for calfkit-tools (a2a channel resolver needs it)"
        )

    agents_dir = Path(os.getenv(_AGENTS_DIR_ENV, _AGENTS_DIR_DEFAULT))
    registry = AgentRegistry.from_agents_dir(agents_dir)
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    timeout_seconds = _resolve_timeout()

    async with (
        DiscordSender(settings) as sender,
        DiscordPersonaSender(settings) as persona_sender,
        Client.connect(server_urls, reply_topic=_REPLY_TOPIC) as client,
    ):
        # Eagerly start the broker so the reply dispatcher is live before
        # any tool tries to ``execute_node`` — mirrors the bridge's
        # boot-time eager start.
        if not client.broker._connection:
            await client.broker.start()

        resolver = A2AChannelResolver(sender, registry, settings.guild_id)
        private_chat.init(
            client=client,
            persona_sender=persona_sender,
            resolver=resolver,
            registry=registry,
            timeout_seconds=timeout_seconds,
        )

        tool_nodes = list(TOOL_REGISTRY.values())
        if not tool_nodes:
            # Defensive: registry should always contain at least one tool, but
            # surfacing this fail-fast prevents an inert worker that silently
            # consumes no topics.
            raise SystemExit("TOOL_REGISTRY is empty; nothing to host")

        worker = Worker(client, tool_nodes)
        logger.info(
            "starting calfkit-tools worker tools=%s broker=%s reply_topic=%s timeout_s=%.1f",
            sorted(TOOL_REGISTRY),
            server_urls,
            _REPLY_TOPIC,
            timeout_seconds,
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
    except KeyboardInterrupt:
        logger.info("calfkit-tools shutting down")


if __name__ == "__main__":
    main()
