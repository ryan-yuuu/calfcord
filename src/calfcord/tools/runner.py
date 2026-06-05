"""CLI entry point for the ``calfkit-tools`` deployment.

Hosts every :class:`ToolNodeDef` registered in :data:`TOOL_REGISTRY` on a
single calfkit :class:`Worker`. Standalone process — separate from the
bridge and the agent runner — so the tool lifecycle is decoupled from
both, matching calfkit's tool-as-deployment model.

This deployment intentionally has no read access to ``agents/*.md``.
Agent identities (display name, avatar, description, tools) arrive at
the tool body via the phonebook the bridge places in ``deps`` on every
invocation. The runner wires only the resources that exist on this
host:

* :class:`DiscordSender` — REST-only client used by
  :class:`A2AChannelResolver` to discover/create the unified A2A audit
  channel (configured via :envvar:`CALFKIT_A2A_CHANNEL_NAME`, default
  ``private-a2a-chats``). Uses the bot token from this deployment's env.
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
from typing import Any

from calfkit.client import Client
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfcord._provisioning import PROVISIONING, provision_and_start_broker
from calfcord._worker_runtime import run_worker_until_signal
from calfcord.bridge.egress import A2AChannelResolver
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.discord.sender import DiscordSender
from calfcord.discord.settings import DiscordSettings
from calfcord.tools import TOOL_REGISTRY
from calfcord.tools.builtin import private_chat

logger = logging.getLogger(__name__)

_REPLY_TOPIC = "calfkit.tools.reply"
"""Named reply topic for the tools client. Must differ from the bridge's
``discord.outbox`` so target-agent ReturnCalls route here, not to the
bridge's outbox consumer (which would project them to Discord twice)."""
_TIMEOUT_ENV = "CALFKIT_TOOLS_TIMEOUT_SECONDS"
_CATEGORY_ENV = "CALFKIT_A2A_CHANNEL_CATEGORY"
_CHANNEL_NAME_ENV = "CALFKIT_A2A_CHANNEL_NAME"
_DEFAULT_CHANNEL_NAME = "private-a2a-chats"
"""The single unified A2A audit channel. Every A2A conversation lives
inside a thread under this channel; operator setup collapses to one
channel + one permission overwrite. Overridable via
:data:`_CHANNEL_NAME_ENV`."""


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


def _resolve_category_name() -> str | None:
    """Read ``CALFKIT_A2A_CHANNEL_CATEGORY`` or return ``None``.

    Empty / whitespace-only values are treated as unset so an operator
    who leaves the line blank in ``.env`` gets the default uncategorized
    behavior rather than a category literally named "" or " ".
    """
    raw = os.getenv(_CATEGORY_ENV)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _resolve_channel_name() -> str:
    """Read ``CALFKIT_A2A_CHANNEL_NAME`` or fall back to the default.

    Mirrors :func:`_resolve_category_name`'s empty-as-unset normalization
    so a stray blank line in ``.env`` falls back to
    :data:`_DEFAULT_CHANNEL_NAME` rather than creating a literally-named
    channel.
    """
    raw = os.getenv(_CHANNEL_NAME_ENV)
    if raw is None:
        return _DEFAULT_CHANNEL_NAME
    stripped = raw.strip()
    return stripped or _DEFAULT_CHANNEL_NAME


def _resolve_tool_nodes(registry: dict[str, Any]) -> list[Any]:
    """Validate the tool registry has at least one tool and return its values.

    Extracted from ``_amain`` so the empty-registry guard can be tested
    without standing up Discord/Kafka. The guard prevents the worker from
    starting in an inert state where it subscribes to no topics — a
    failure mode that would be very confusing in production logs.

    Empty-registry is most commonly caused by a typo in
    ``CALFCORD_TOOLS_INCLUDE`` (per-tool images), so the SystemExit
    message includes the env var value to short-circuit the operator's
    "why is my registry empty" hunt. A complementary WARNING fires at
    discovery time naming the specific typo'd entries (see
    :func:`calfcord.tools.discovery.discover_tools`).
    """
    nodes = list(registry.values())
    if not nodes:
        include_filter = os.environ.get("CALFCORD_TOOLS_INCLUDE") or "<unset>"
        raise SystemExit(
            "TOOL_REGISTRY is empty; nothing to host "
            f"(CALFCORD_TOOLS_INCLUDE={include_filter})"
        )
    return nodes


async def _run_worker(worker: Worker) -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Delegates to the shared :func:`calfcord._worker_runtime.run_worker_until_signal`
    so the shutdown contract (signal-driven drain plus the
    "clean return without a signal is a crash" supervisor invariant) is
    defined in exactly one place across runners. Kept as a thin local
    wrapper because existing tests reference ``_run_worker`` by name.
    """
    await run_worker_until_signal(worker, drain_label="tools worker")


async def _amain() -> None:
    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise SystemExit(
            "DISCORD_GUILD_ID is required for calfkit-tools (a2a channel resolver needs it)"
        )

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    timeout_seconds = _resolve_timeout()
    category_name = _resolve_category_name()
    channel_name = _resolve_channel_name()

    async with (
        DiscordSender(settings) as sender,
        DiscordPersonaSender(settings) as persona_sender,
        Client.connect(server_urls, reply_topic=_REPLY_TOPIC, provisioning=PROVISIONING) as client,
    ):
        # Provision the reply topic, then eagerly start the broker so the reply
        # dispatcher is live before any tool tries to ``execute_node``. The
        # worker's tool-node topics are provisioned later by Worker.run()'s
        # startup hook (via _run_worker below).
        await provision_and_start_broker(client)

        resolver = A2AChannelResolver(
            sender,
            settings.guild_id,
            channel_name=channel_name,
            category_name=category_name,
        )
        # The thread-history fetch needs a live :class:`discord.Client`.
        # The persona sender already authenticates one on startup
        # (REST-only, no gateway) — reuse it rather than spinning up a
        # second connection just for thread reads. ``persona_sender.client``
        # raises if start() hasn't been awaited, so a future lifecycle
        # refactor that lazy-initializes the client will fail fast here
        # at boot rather than at first invocation.
        private_chat.init(
            client=client,
            persona_sender=persona_sender,
            resolver=resolver,
            discord_client=persona_sender.client,
            timeout_seconds=timeout_seconds,
        )

        tool_nodes = _resolve_tool_nodes(TOOL_REGISTRY)

        worker = Worker(client, tool_nodes)
        logger.info(
            "starting calfkit-tools worker tools=%s broker=%s reply_topic=%s "
            "timeout_s=%.1f a2a_channel=%s a2a_category=%s include_filter=%s",
            sorted(TOOL_REGISTRY),
            server_urls,
            _REPLY_TOPIC,
            timeout_seconds,
            channel_name,
            category_name,
            os.environ.get("CALFCORD_TOOLS_INCLUDE") or "<unset>",
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
