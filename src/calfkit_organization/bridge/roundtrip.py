"""Discord ↔ agent round-trip: invoke an agent and post its reply back to Discord.

Replaces the old fire-and-forget ``KafkaPublisher`` with an awaitable
request/response shape. The bridge holds the calfkit :class:`Client` whose
dispatcher is bound to the named reply topic ``discord.outbox``; every agent
:class:`ReturnCall` lands there. :class:`BridgeRoundTrip` uses
:meth:`Client.execute_node` to publish the inbound wire to
``discord.channel.{cid}.in`` and await the reply, then resolves the
responding agent's persona via :class:`AgentRegistry` and posts the reply
to the originating channel under that persona.

Identity is resolved from ``NodeResult.emitter_node_id``, which calfkit
0.3.0 populates from the inbound ``x-calf-emitter`` Kafka header — no
application-level identity stamping needed.

Concurrency: every inbound Discord message produces a fresh
:meth:`handle` coroutine. A semaphore caps outstanding invocations to
prevent runaway memory + Discord rate-limit pressure when the LLM stalls.

**Multi-agent reply semantics**: calfkit's reply dispatcher resolves at
most one reply per ``correlation_id`` (the rest are logged-and-dropped at
the dispatcher). When multiple agents both gate-accept the same inbound
event, only the first to finish reaches this code path; the others' work
is silently lost at the consumer. Acceptable for v1 (slash/mention flows
target a single agent); migrate to a non-dedupe outbox consumer when
ambient multi-agent flows matter.
"""

from __future__ import annotations

import asyncio
import logging

from calfkit.client import Client

from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireMessage
from calfkit_organization.discord.persona import (
    DiscordPersonaSender,
    Persona,
    ReplyContext,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_MAX_IN_FLIGHT = 32
_DEFAULT_INGRESS_TOPIC_TEMPLATE = "discord.channel.{cid}.in"


class BridgeRoundTrip:
    """Invoke an agent and post its reply back to Discord.

    Owned by :class:`DiscordIngressGateway`; one instance per bridge process.
    The bridge's ``DiscordPersonaSender`` and ``AgentRegistry`` are shared;
    the calfkit ``Client`` must be connected with ``reply_topic="discord.outbox"``
    so the dispatcher hears agent ReturnCalls.
    """

    def __init__(
        self,
        calfkit_client: Client,
        registry: AgentRegistry,
        persona_sender: DiscordPersonaSender,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_in_flight: int = _DEFAULT_MAX_IN_FLIGHT,
        ingress_topic_template: str = _DEFAULT_INGRESS_TOPIC_TEMPLATE,
    ) -> None:
        self._client = calfkit_client
        self._registry = registry
        self._persona_sender = persona_sender
        self._timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._ingress_topic_template = ingress_topic_template

    async def handle(self, wire: WireMessage) -> None:
        """Invoke the addressed agent and post its reply.

        Drops the event (logs only) on:
            - timeout (no agent responded within ``timeout_seconds``)
            - non-agent emitter on the reply (e.g. client republish)
            - unknown emitter id (registry miss)
            - empty agent output (no text to post)

        Discord HTTP errors from :meth:`DiscordPersonaSender.send` propagate.
        """
        async with self._semaphore:
            try:
                result = await self._client.execute_node(
                    user_prompt=wire.content,
                    topic=self._ingress_topic_template.format(cid=wire.channel_id),
                    correlation_id=wire.event_id,
                    deps={"discord": wire.model_dump(mode="json")},
                    output_type=str,
                    timeout=self._timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "agent reply timed out event_id=%s channel=%s",
                    wire.event_id,
                    wire.channel_id,
                )
                return

            if result.emitter_node_kind != "agent" or not result.emitter_node_id:
                logger.warning(
                    "non-agent emitter on reply event_id=%s id=%s kind=%s",
                    wire.event_id,
                    result.emitter_node_id,
                    result.emitter_node_kind,
                )
                return

            spec = self._registry.by_id(result.emitter_node_id)
            if spec is None:
                logger.warning(
                    "unknown agent emitter=%s event_id=%s",
                    result.emitter_node_id,
                    wire.event_id,
                )
                return

            text = (result.output or "").strip()
            if not text:
                logger.info(
                    "agent %s returned empty output event_id=%s; skipping post",
                    result.emitter_node_id,
                    wire.event_id,
                )
                return

            sent = await self._persona_sender.send(
                persona=Persona(name=spec.display_name, avatar_url=spec.avatar_url),
                channel_id=wire.channel_id,
                content=text,
                reply_to=ReplyContext.from_wire(wire),
            )
            logger.info(
                "posted reply event_id=%s agent=%s reply_id=%s channel=%s",
                wire.event_id,
                result.emitter_node_id,
                sent.id,
                wire.channel_id,
            )
