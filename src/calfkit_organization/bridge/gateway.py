"""Discord ingress gateway daemon and CLI entry point.

Holds the long-lived gateway WebSocket, wires the slash command manager
and the agent ingress publisher together, and exposes ``main()`` as the
script entry point. Run via::

    uv run calfkit-bridge

The daemon depends on a running Kafka broker reachable at ``CALF_HOST_URL``
(defaults to ``localhost``) and a Discord bot configured via the
``DISCORD_*`` environment variables (see ``.env.example``).

The bridge process does both halves of the Discord I/O:

* **Ingress** — Discord events normalize into :class:`WireMessage` and
  fire-and-forget through :class:`BridgeIngress` onto Kafka.
* **Egress** — every agent reply landing on ``discord.outbox`` is posted
  to Discord by a long-lived calfkit consumer
  (:func:`build_outbox_consumer`). Replies are no longer awaited
  inline; this is what lets multiple agents respond to the same
  inbound event without losing all but the fastest reply (calfkit's
  reply dispatcher dedupes by ``correlation_id``, so the request/reply
  shape we used before was inherently single-agent).

The consumer's handler is registered on the same calfkit
:class:`~calfkit.Client` connection that the ingress publishes through.
We deliberately register handlers without calling
:meth:`Worker.run` — see the rationale at the call site in :func:`main`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import OrderedDict
from pathlib import Path

import discord
from calfkit.client import Client
from calfkit.worker import Worker

from calfkit_organization.bridge.history import ChannelHistoryFetcher
from calfkit_organization.bridge.ingress import (
    AmbientRosterEmptyError,
    BridgeIngress,
)
from calfkit_organization.bridge.normalizer import (
    MessageNormalizer,
    SlashNormalizer,
    UnknownAgentMentionError,
)
from calfkit_organization.bridge.outbox import build_outbox_consumer
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.slash import SlashCommandManager
from calfkit_organization.bridge.synthesized import build_synthesized_consumer
from calfkit_organization.discord.persona import DiscordPersonaSender
from calfkit_organization.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

_REPLY_TOPIC = "discord.outbox"
_SEEN_MESSAGE_IDS_CAPACITY = 1024


class DiscordIngressGateway:
    """Long-lived gateway daemon. Translates Discord events into agent invocations."""

    def __init__(
        self,
        settings: DiscordSettings,
        ingress: BridgeIngress,
        registry: AgentRegistry,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._ingress = ingress
        self._client = _GatewayClient(self)

        # MessageNormalizer needs bot_user_id, which we don't know until on_ready.
        self._message_normalizer: MessageNormalizer | None = None
        self._bot_user_id: int | None = None
        self._slash_normalizer = SlashNormalizer(
            registry=registry,
            human_owner_id=settings.owner_user_id,
        )
        self._slash = SlashCommandManager(
            client=self._client,
            registry=registry,
            ingress=self._ingress,
            slash_normalizer=self._slash_normalizer,
            owner_user_id=settings.owner_user_id,
        )
        # Per-agent invocation slashes (``/echo``, ``/scribe``, …) are
        # disabled in favour of ``@<agent_id>`` text-prefix invocation parsed
        # by MessageNormalizer. To re-enable them, uncomment the next line.
        # self._slash.register_all()
        # The /thinking-effort operator slash is always registered so the
        # tree is non-empty and stale per-agent slashes get pruned on sync.
        self._slash.register_thinking_effort()

        # Bounded LRU of Discord message ids we've already invoked an agent
        # for. discord.py can redeliver MESSAGE_CREATE on gateway reconnect;
        # without this guard we'd double-spend on LLM tokens and double-post
        # to Discord. The bridge handles many channels but redelivery is
        # bursty around reconnects, so 1024 entries covers any realistic
        # window.
        self._seen_message_ids: OrderedDict[int, None] = OrderedDict()

    async def start(self) -> None:
        """Connect to the Discord gateway. Blocks until cancelled or disconnect."""
        logger.info(
            "DiscordIngressGateway starting (guild_id=%s)",
            self._settings.guild_id,
        )
        await self._client.start(self._settings.bot_token.get_secret_value())

    async def close(self) -> None:
        """Disconnect cleanly. Idempotent."""
        if not self._client.is_closed():
            await self._client.close()

    async def _on_ready(self) -> None:
        bot_user = self._client.user
        assert bot_user is not None, "on_ready fires after authentication completes"
        self._bot_user_id = bot_user.id
        self._message_normalizer = MessageNormalizer(
            registry=self._registry,
            bot_user_id=bot_user.id,
            human_owner_id=self._settings.owner_user_id,
        )
        # The history fetcher needs a live (post-handshake)
        # :class:`discord.Client` — get_channel/fetch_channel both
        # rely on the WebSocket having populated the guild/channel
        # cache. Construct here in ``_on_ready`` and inject into the
        # ingress. Before this point, the ingress's slash branch
        # degrades to empty ``message_history`` (see
        # :meth:`BridgeIngress._build_slash_message_history`).
        fetcher = ChannelHistoryFetcher(self._client, self._registry)
        self._ingress.set_fetcher(fetcher)
        logger.info("gateway ready as %s (id=%s); history fetcher injected", bot_user, bot_user.id)
        await self._slash.sync(self._settings.guild_id)

    async def _on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if self._settings.guild_id is not None and message.guild.id != self._settings.guild_id:
            return
        if self._message_normalizer is None:
            # Pre-ready; shouldn't fire in practice but defensive.
            return
        # Skip the bot's own non-webhook messages (e.g. error replies from
        # _reply_unknown_mention). These are bridge-internal infrastructure
        # noise; agents never need to react to them. Webhook messages (the
        # bot acting as an agent persona) are NOT filtered here — those flow
        # through so the originating agent can self-recognize and other
        # agents can see peer activity.
        if (
            self._bot_user_id is not None
            and message.author.id == self._bot_user_id
            and message.webhook_id is None
        ):
            return
        if self._already_seen(message.id):
            logger.debug("ignoring redelivered message id=%s", message.id)
            return
        try:
            wire = self._message_normalizer.normalize(message)
        except UnknownAgentMentionError as err:
            await self._reply_unknown_mention(message, err.unknown_names)
            return
        except Exception:
            logger.exception("failed to normalize message id=%s", message.id)
            return
        try:
            await self._ingress.handle(wire)
        except AmbientRosterEmptyError:
            # Deployment misconfiguration (no assistants registered).
            # Surface to the user via an inline reply so the missing
            # response isn't silent. The ingress already logged ERROR
            # naming this specific event_id and channel.
            await self._reply_empty_roster(message)
        except Exception:
            # Any other failure (broker hiccup, registry-mid-write,
            # unexpected validation error, etc.). The ingress's own
            # ``logger.exception`` already captured the stack trace
            # with full context; we add a user-facing inline reply
            # so the silence isn't unexplained. The reply helper
            # swallows its own Discord HTTPException so this handler
            # is best-effort.
            logger.exception("ingress publish failed for event_id=%s", wire.event_id)
            await self._reply_ingress_failure(message)

    def _already_seen(self, message_id: int) -> bool:
        """Bounded-LRU dedupe of Discord ``message.id``.

        Returns ``True`` if the id has been seen recently. On miss, records
        it and evicts the oldest entry when at capacity.
        """
        if message_id in self._seen_message_ids:
            self._seen_message_ids.move_to_end(message_id)
            return True
        self._seen_message_ids[message_id] = None
        if len(self._seen_message_ids) > _SEEN_MESSAGE_IDS_CAPACITY:
            self._seen_message_ids.popitem(last=False)
        return False

    async def _reply_unknown_mention(
        self,
        message: discord.Message,
        unknown_names: list[str],
    ) -> None:
        """Inline-reply to the user that one or more @<name> mentions are unknown.

        The original message is NOT published to Kafka — the user must fix the
        mention(s) and resend for any agent to receive it.
        """
        bad = ", ".join(f"`@{n}`" for n in unknown_names)
        # Filter out routers: they're not user-invocable via
        # @-mention (:meth:`MessageNormalizer._classify` treats them
        # as unknown by design), so advertising them in the
        # known-agents list would mislead the user into trying
        # ``@_router`` next and hitting the same rejection.
        known_specs = [
            s for s in self._registry.all() if s.role != "router"
        ]
        known_part = (
            f"Known agents: {', '.join(f'`@{s.agent_id}`' for s in known_specs)}."
            if known_specs
            else "No agents are currently registered."
        )
        text = (
            f"No agent matches {bad}. {known_part} "
            f"Please fix the mention and resend the message."
        )
        logger.info(
            "rejected unknown mention(s)=%s message_id=%s",
            unknown_names,
            message.id,
        )
        try:
            await message.reply(text)
        except discord.HTTPException:
            logger.exception("failed to send unknown-mention reply")

    async def _reply_empty_roster(self, message: discord.Message) -> None:
        """Inline-reply to the user that no assistant agents are configured.

        Triggered when :class:`BridgeIngress` raises
        :class:`AmbientRosterEmptyError` from the ambient path —
        meaning the registry contains only the built-in router and
        no assistants for the router to dispatch to. The user gets
        an operator-actionable message instead of silent
        non-response. The Discord HTTPException catch matches the
        ``_reply_unknown_mention`` shape: the bridge is in an
        error-recovery path and there's nothing useful to do if the
        reply itself fails.
        """
        text = (
            "No assistant agents are currently configured, so this "
            "message can't be routed. Please contact an operator to "
            "add an agent."
        )
        logger.info(
            "rejected ambient publish (empty roster) message_id=%s",
            message.id,
        )
        try:
            await message.reply(text)
        except discord.HTTPException:
            logger.exception("failed to send empty-roster reply")

    async def _reply_ingress_failure(self, message: discord.Message) -> None:
        """Inline-reply to the user that an unexpected failure occurred.

        Triggered by the broad ``except Exception`` in
        :meth:`_on_message` after the ingress raised something that
        wasn't :class:`AmbientRosterEmptyError` — broker hiccup,
        registry mid-write, unexpected validation error. The
        ingress's own ``logger.exception`` already captured the
        stack trace; this is the user-side counterpart so the
        absence of an agent reply isn't unexplained. Phrased
        generically (no internal detail) because we don't know what
        went wrong; the operator-actionable signal is the ingress
        log + a future retry from the user.
        """
        text = (
            "Something went wrong handling that message; please try "
            "again. If this keeps happening, an operator should "
            "check the bridge logs."
        )
        logger.info(
            "ingress failure surfaced to user message_id=%s",
            message.id,
        )
        try:
            await message.reply(text)
        except discord.HTTPException:
            logger.exception("failed to send ingress-failure reply")


class _GatewayClient(discord.Client):
    """``discord.Client`` subclass that delegates events to a ``DiscordIngressGateway``."""

    def __init__(self, gateway: DiscordIngressGateway) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self._gateway = gateway

    async def on_ready(self) -> None:
        await self._gateway._on_ready()

    async def on_message(self, message: discord.Message) -> None:
        await self._gateway._on_message(message)


def main() -> None:
    """CLI entry point. Loads config, constructs the gateway, runs until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise SystemExit("DISCORD_GUILD_ID is required (global slash sync is too slow for dev)")

    agents_dir = Path(os.getenv("CALFKIT_AGENTS_DIR", "agents"))
    registry = AgentRegistry.from_agents_dir(agents_dir)

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async def _run() -> None:
        # The bridge owns its own persona sender (separate from agent processes')
        # because it posts replies on behalf of every agent. The calfkit client
        # connects with a named reply topic so calfkit's reply dispatcher hears
        # every agent ReturnCall — even though we no longer await its futures
        # (the outbox consumer below handles every reply), the dispatcher's
        # subscriber is still registered as a side-effect of Client.connect.
        # Its "no pending future" WARNINGs on every reply are expected; see
        # calfkit_organization.bridge.outbox.
        async with DiscordPersonaSender(settings) as persona_sender:
            async with Client.connect(server_urls, reply_topic=_REPLY_TOPIC) as calfkit_client:
                pending_wires = PendingWires()
                ingress = BridgeIngress(
                    calfkit_client=calfkit_client,
                    registry=registry,
                    pending_wires=pending_wires,
                )
                consumer_node = build_outbox_consumer(
                    persona_sender=persona_sender,
                    registry=registry,
                    pending_wires=pending_wires,
                )
                # The synthesized-in consumer subscribes to
                # ``bridge.synthesized.in`` and re-feeds router fan-out
                # wires through the same ingress handler real Discord
                # events use. Co-tenants on the same Worker so it
                # shares the bridge's calfkit Client + broker (and the
                # same consumer-group-per-node-id contract).
                synthesized_node = build_synthesized_consumer(ingress)

                # Register the consumer's handler on the broker *before*
                # broker.start() so its consumer group joins ahead of the
                # gateway accepting Discord events. Otherwise an agent reply
                # arriving in the brief window after publish but before the
                # consumer-group has joined would be missed (subscribers
                # default to auto_offset_reset="latest").
                #
                # We use Worker only for handler registration — not Worker.run.
                # Calling Worker.run would (a) call register_handlers again
                # (which errors on the second call), and (b) start an inner
                # FastStream serve loop whose signal handling overlaps with
                # the loop we install below. broker.start() activates every
                # registered subscriber on its own, which is what we need.
                worker = Worker(calfkit_client, [consumer_node, synthesized_node])
                worker.register_handlers()
                # ``broker.running`` is the public-ish state flag faststream
                # sets True at the end of start() and False in stop(). Guarding
                # on it (rather than calling start() unconditionally) matters
                # because faststream's ``KafkaSubscriber.start`` is *not*
                # idempotent — a second call would build a fresh aiokafka
                # consumer, drop the previous reference, and re-subscribe.
                if not calfkit_client.broker.running:
                    await calfkit_client.broker.start()

                gateway = DiscordIngressGateway(settings, ingress, registry)

                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, stop.set)

                gateway_task = asyncio.create_task(gateway.start())
                stop_task = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait(
                        {gateway_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (gateway_task, stop_task):
                        if not t.done():
                            t.cancel()
                    await gateway.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
