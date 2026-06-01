"""Echo agent — a starter calfkit agent for testing the bridge end-to-end.

The agent subscribes to one or more configured Discord channels via Kafka,
filters incoming events with two gates (not-from-self, slash-addressed-to-me),
and replies to each accepted event with ``echo: <content>`` posted via
:class:`DiscordPersonaSender` as an inline reply.

This is a *hand-coded* calfkit runtime kept alongside the declarative
``agents/echo.md`` definition. Identity fields (``agent_id``,
``display_name``, ``avatar_url``) are read from the parsed ``echo.md`` at
runtime startup so this file and the bridge's registry cannot drift. The
system-prompt body of ``echo.md`` is unused here — this runtime echoes
content directly without invoking an LLM.

**Do not run this script alongside ``calfkit-agent`` (all-mode).** The
all-agents runner will spin up a factory-built ``echo`` node for
``agents/echo.md`` using ``group_id=echo``; this script registers the same
consumer group for the same channels. Running both at once causes the two
processes to contend for the same Kafka partitions. Pick one runtime per
environment: this script *or* the all-mode runner.

Configuration (environment variables):

    DISCORD_BOT_TOKEN              required — REST access for persona sends
    DISCORD_APPLICATION_ID         required by DiscordSettings
    ECHO_CHANNEL_IDS               comma-separated channel IDs this agent
                                   listens on (e.g. "12345,67890");
                                   falls back to DISCORD_DEFAULT_CHANNEL_ID
    CALF_HOST_URL                  Kafka bootstrap; defaults to "localhost"

Run::

    uv run python agents/echo.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from calfkit.client import Client
from calfkit.models import NodeResult, SessionRunContext, Silent, State
from calfkit.nodes import BaseNodeDef
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfkit_organization.agents.definition import parse_agent_md
from calfkit_organization.agents.gates import make_addressable_gate, make_addressed_to_me_gate
from calfkit_organization.bridge.wire import WireMessage
from calfkit_organization.discord.persona import (
    DiscordPersonaSender,
    Persona,
    ReplyContext,
    ReplyStyle,
)
from calfkit_organization.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)


class EchoNode(BaseNodeDef):
    """Replies ``echo: <content>`` as an inline reply to the bridge's slash echo."""

    def __init__(
        self,
        *,
        node_id: str,
        subscribe_topics: list[str],
        persona: Persona,
        persona_sender: DiscordPersonaSender,
        reply_style: ReplyStyle,
    ) -> None:
        super().__init__(node_id=node_id, subscribe_topics=subscribe_topics)
        self._persona_sender = persona_sender
        self._persona = persona
        self._reply_style = reply_style

    async def run(self, ctx: SessionRunContext) -> NodeResult[State]:
        wire = WireMessage.model_validate(ctx.deps.provided_deps["discord"])

        sent = await self._persona_sender.send(
            persona=self._persona,
            channel_id=wire.channel_id,
            content=f"echo: {wire.content}",
            reply_to=ReplyContext.from_wire(wire, style=self._reply_style),
        )
        logger.info(
            "echoed event_id=%s reply_to=%s reply_id=%s channel=%s",
            wire.event_id,
            wire.message_id,
            sent.id,
            wire.channel_id,
        )
        return Silent()


def _resolve_channel_ids() -> list[int]:
    raw = os.getenv("ECHO_CHANNEL_IDS") or os.getenv("DISCORD_DEFAULT_CHANNEL_ID")
    if not raw:
        raise SystemExit(
            "ECHO_CHANNEL_IDS (or DISCORD_DEFAULT_CHANNEL_ID as fallback) is required: "
            "comma-separated channel IDs the echo agent should listen on."
        )
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def _resolve_reply_style() -> ReplyStyle:
    """Pick the inline-reply UI style for this echo run.

    Defaults to ``"button"`` so the current diagnostic run shows the
    Link-button option without env-var setup. Set
    ``ECHO_REPLY_STYLE=embed`` to switch to the PluralKit-style embed.
    """
    raw = (os.getenv("ECHO_REPLY_STYLE") or "button").lower()
    if raw not in ("embed", "button"):
        raise SystemExit(
            f"ECHO_REPLY_STYLE must be 'embed' or 'button', got {raw!r}"
        )
    return raw  # type: ignore[return-value]


async def _amain() -> None:
    definition = parse_agent_md(Path(__file__).with_name("echo.md"))
    persona = Persona(name=definition.display_name, avatar_url=definition.avatar_url)

    settings = DiscordSettings()  # type: ignore[call-arg]
    channel_ids = _resolve_channel_ids()
    reply_style = _resolve_reply_style()
    subscribe_topics = [f"discord.channel.{cid}.in" for cid in channel_ids]
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async with DiscordPersonaSender(settings) as persona_sender, Client.connect(server_urls) as client:
        node = EchoNode(
            node_id=definition.agent_id,
            subscribe_topics=subscribe_topics,
            persona=persona,
            persona_sender=persona_sender,
            reply_style=reply_style,
        )
        # AND-semantics: both gates must accept. Authorship check first so
        # we short-circuit on self/unknown-bot before doing content-based
        # addressed-to-me checks.
        node.gate(make_addressable_gate(definition.agent_id))
        node.gate(make_addressed_to_me_gate(definition.agent_id))

        worker = Worker(client, [node])
        logger.info(
            "echo agent starting on channels=%s broker=%s reply_style=%s",
            channel_ids,
            server_urls,
            reply_style,
        )
        await worker.run()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("echo agent shutting down")


if __name__ == "__main__":
    main()
