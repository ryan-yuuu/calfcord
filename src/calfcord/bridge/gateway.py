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

The consumer's handlers ride a calfkit :class:`~calfkit.Worker` driven as
``async with worker:`` (not :meth:`Worker.run`): the bridge embeds a
foreground (the Discord gateway WebSocket owns the loop), so it keeps its
own signal handling and gateway/stop race while the worker owns
register/provision/broker start/stop. See :func:`_run`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import discord
from calfkit.client import Client
from calfkit.worker import Worker

from calfcord._provisioning import PROVISIONING, bridge_infra_topics, provision_infra
from calfcord.bridge.history import ChannelHistoryFetcher
from calfcord.bridge.ingress import (
    AmbientRosterEmptyError,
    BridgeIngress,
)
from calfcord.bridge.normalizer import (
    MessageNormalizer,
    SlashNormalizer,
    UnknownAgentMentionError,
)
from calfcord.bridge.outbox import build_outbox_consumer
from calfcord.bridge.pending_wires import PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.slash import SlashCommandManager
from calfcord.bridge.steps import build_steps_consumer
from calfcord.bridge.steps_state import StepsState
from calfcord.bridge.steps_toggle import StepsToggleView
from calfcord.bridge.synthesized import build_synthesized_consumer
from calfcord.bridge.transcripts import (
    NullTranscriptStore,
    TranscriptStore,
    TranscriptStoreLike,
)

# NOTE: ``calfcord.control_plane.state_consumer`` imports
# :class:`AgentRegistry`, which in turn triggers ``bridge.__init__`` (which
# re-exports it). That ``__init__`` imports this module, so a top-level
# import of ``state_consumer`` here would create a circular import. We do
# top-level imports of ``publish_discovery_ping`` (whose module doesn't
# depend on the bridge package) but defer ``register_state_consumer`` to
# its single call site in ``main()``'s ``_run()``.
from calfcord.control_plane.publish import publish_discovery_ping
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.discord.settings import DiscordSettings
from calfcord.discord.typing import TypingNotifier
from calfcord.router.definition import build_router_definition

logger = logging.getLogger(__name__)

_REPLY_TOPIC = "discord.outbox"
_SEEN_MESSAGE_IDS_CAPACITY = 1024

# Discord caps thread names at 100 characters.
_THREAD_NAME_MAX_LEN = 100

# A plaintext ``/task`` command: a message whose content is the bare token
# ``/task`` optionally followed by the task text. Case-insensitive; DOTALL so
# a multi-line task body is captured whole. Anchored at the start so it is a
# real command, not a ``/task`` mentioned mid-sentence; ``/taskfoo`` does NOT
# match (the command must be the bare token ``/task``). A bare ``/task`` —
# nothing after it, or only whitespace — matches with an empty body, which
# :func:`_parse_task_command` reports as ``None``.
_TASK_COMMAND_RE = re.compile(r"^/task(?:\s+(?P<body>.*))?$", re.IGNORECASE | re.DOTALL)


def _thread_name_from_text(text: str, *, fallback: str = "Task", max_len: int = _THREAD_NAME_MAX_LEN) -> str:
    """Derive a thread title from the ``/task`` body text.

    Collapses runs of whitespace and truncates to Discord's ``max_len``-char
    thread-name cap (appending an ellipsis when truncated). Falls back to
    ``fallback`` when the text is empty after collapsing.
    """
    collapsed = " ".join(text.split())
    if not collapsed:
        return fallback
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1].rstrip() + "…"


def _parse_task_command(content: str) -> tuple[bool, str | None]:
    """Classify a message body as a plaintext ``/task`` command.

    Returns ``(is_task, body)``:

    - ``(False, None)`` — not a ``/task`` command; route the message normally.
    - ``(True, None)`` — a bare ``/task`` with no task text (reply with usage).
    - ``(True, "<text>")`` — a ``/task`` carrying task text (open a thread).

    The returned ``body`` is whitespace-stripped and used only to derive the
    thread title; the routed wire keeps the full original message content.
    """
    match = _TASK_COMMAND_RE.match(content)
    if match is None:
        return False, None
    body = match.group("body")
    if body is not None:
        body = body.strip()
    return True, (body or None)


@asynccontextmanager
async def _open_transcript_store(settings: DiscordSettings) -> AsyncIterator[TranscriptStoreLike]:
    """Open the transcript store, degrading to a no-op store on failure.

    Constructs the real :class:`TranscriptStore` and connects it. If the
    open fails (bad path, disk error, corrupt DB, …) the bridge MUST NOT
    abort — a crash here would take down all Discord routing, not just
    transcripts. Instead we log a loud ERROR and substitute a
    :class:`NullTranscriptStore` so the run continues with transcripts,
    tool-call replay, and the expand toggle disabled. Yields whichever
    store is in effect; the real store's connection (if any) is closed on
    exit, and ``NullTranscriptStore.close`` is a harmless no-op.
    """
    store = TranscriptStore(settings.transcript_db_path)
    yielded: TranscriptStoreLike = store
    try:
        try:
            await store.connect()
        except Exception:
            logger.error(
                "transcript store failed to open at %s — step transcripts, "
                "tool-call replay, and the expand toggle are DISABLED for this run",
                settings.transcript_db_path,
                exc_info=True,
            )
            yielded = NullTranscriptStore()
        yield yielded
    finally:
        await yielded.close()


async def _prune_on_startup(store: TranscriptStoreLike, settings: DiscordSettings) -> None:
    """Best-effort startup sweep: drop transcript rows past the retention window.

    The bridge is the sole writer and restarts on every deploy, so a
    startup prune bounds the store's growth without a background task.
    Disabled when ``transcript_retention_days <= 0`` (keep forever).

    Best-effort by contract: retention is housekeeping, so a prune failure
    (read-only volume, disk full, …) must NEVER abort bridge startup —
    that would take down all Discord routing, not just transcripts. Any
    exception is logged and swallowed. A :class:`NullTranscriptStore`
    (failed-open run) prunes nothing and reports zero, so this is a no-op
    there too.
    """
    if settings.transcript_retention_days <= 0:
        return
    try:
        cutoff = int(time.time()) - settings.transcript_retention_days * 86400
        pruned = await store.prune_older_than(cutoff)
        if pruned:
            logger.info(
                "pruned %d transcript row(s) older than %d days",
                pruned,
                settings.transcript_retention_days,
            )
    except Exception:
        logger.exception("transcript retention prune failed at startup; continuing")


class DiscordIngressGateway:
    """Long-lived gateway daemon. Translates Discord events into agent invocations."""

    def __init__(
        self,
        settings: DiscordSettings,
        ingress: BridgeIngress,
        registry: AgentRegistry,
        calfkit_client: Client,
        transcript_store: TranscriptStoreLike,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._ingress = ingress
        self._calfkit_client = calfkit_client
        self._transcript_store = transcript_store
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
            calfkit_client=calfkit_client,
            owner_user_id=settings.owner_user_id,
            guild_id=settings.guild_id,
        )
        # Per-agent invocation slashes (``/echo``, ``/scribe``, …) are
        # disabled in favour of ``@<agent_id>`` text-prefix invocation parsed
        # by MessageNormalizer. To re-enable them, uncomment the next line.
        # self._slash.register_all()
        # The /thinking-effort operator slash is always registered so the
        # tree is non-empty and stale per-agent slashes get pruned on sync.
        self._slash.register_thinking_effort()
        # The /clear operator slash lets the owner drop a per-channel
        # context boundary: it posts a marker message that the history
        # fetcher truncates at, so agents stop seeing messages above the
        # line. Registered unconditionally alongside /thinking-effort and
        # pushed to Discord by the same _on_ready sync().
        self._slash.register_clear()
        # NOTE: ``/task`` is intentionally NOT a Discord slash command. It is a
        # plaintext command (``/task <text>``) detected in :meth:`_on_message`
        # so the task's opening message is genuinely authored by the user — a
        # slash interaction can only post the anchor as the bot or a webhook.
        # See :meth:`_maybe_handle_task`.

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

        # Register the step-transcript expand toggle as a PERSISTENT view
        # (timeout=None, static custom_id). One instance handles every
        # click carrying ``steps:toggle`` on any agent reply — including
        # replies posted before this restart, since the dispatch is matched
        # purely by custom_id, not by an in-memory per-message view. The
        # outbox attaches throwaway buttons to individual replies; this is
        # the single registration that makes them interactable.
        self._client.add_view(StepsToggleView(self._transcript_store))

        # Publish a one-shot discovery ping so already-running agents
        # re-announce into the bridge's freshly-empty registry projection.
        # On a cold start (no agents up yet) the ping is a no-op; on a
        # bridge restart it's the only way to repopulate the registry
        # without restarting every agent process.
        try:
            await publish_discovery_ping(self._calfkit_client)
            logger.info("published discovery_ping; awaiting agent state events")
        except Exception:
            logger.exception("failed to publish discovery_ping; agents may need restart for visibility")

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
        if self._bot_user_id is not None and message.author.id == self._bot_user_id and message.webhook_id is None:
            return
        if self._already_seen(message.id):
            logger.debug("ignoring redelivered message id=%s", message.id)
            return
        # Plaintext ``/task`` command: open a thread off this (user-authored)
        # message and route the task into it. Checked AFTER the dedupe so a
        # reconnect redelivery can't spawn a duplicate thread, and after the
        # self-filter so the bot's own posts never trigger it. When it owns the
        # message it returns True and we stop — the message must not also flow
        # through normal ambient routing.
        if await self._maybe_handle_task(message):
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

    async def _maybe_handle_task(self, message: discord.Message) -> bool:
        """Handle a plaintext ``/task`` command; return whether it was owned.

        Returns ``True`` when the message is a ``/task`` command this method
        has fully handled (so the caller must stop and NOT route it normally),
        and ``False`` when the message is not a task and should flow through
        the usual ambient/slash routing.

        ``/task`` is a plaintext command rather than a Discord slash command on
        purpose: a slash invocation can only post the thread's opening message
        as the bot or a webhook, whereas this rides the user's own message — so
        the task's anchor is genuinely authored by the user. Discord's built-in
        ``/thread`` reads as the user for the same reason (it runs as the
        user's own client); a bot has no API to post a message as someone else,
        so the user must author the anchor and we thread off it.

        Flow once a task is recognised (every branch returns ``True``):

        1. Only humans start tasks — an agent-persona webhook (or any bot) that
           posts ``/task`` falls through to normal routing instead.
        2. Require a top-level text channel: reject inside an existing thread
           (Discord can't nest threads) or a non-text channel (forum/voice —
           the persona webhook needs a parent text channel anyway).
        3. A bare ``/task`` (no task text) gets a usage hint.
        4. Open a public thread anchored on the user's own message (the thread
           shares the message id) and route an ambient, thread-scoped wire so
           the router summons the agents and their replies + live-step progress
           post into the thread.

        Every Discord call is best-effort: a failure logs and inline-replies
        explaining what went wrong.
        """
        is_task, body = _parse_task_command(message.content)
        if not is_task:
            return False
        # Only genuine humans spawn tasks. Persona webhooks (and any other bot)
        # that happen to post "/task ..." flow through to normal routing so
        # peers still see the message — they don't open task threads.
        if message.webhook_id is not None or message.author.bot:
            return False

        logger.info(
            "task command invoked channel_id=%s user_id=%s message_id=%s",
            getattr(message.channel, "id", None),
            message.author.id,
            message.id,
        )

        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            await self._reply_best_effort(
                message,
                "`/task` only works in a top-level text channel — not inside a "
                "thread (threads can't be nested) or a forum/voice channel.",
            )
            return True

        if body is None:
            await self._reply_best_effort(
                message,
                "Usage: `/task <what you want done>` — describe the task and I'll "
                "open a thread for the agents to work in.",
            )
            return True

        if self._message_normalizer is None:
            # Pre-ready guard. In practice unreachable via _on_message (which
            # returns early when the normalizer is unset, before reaching here),
            # but enforced defensively because we have already claimed the
            # message. Make the drop loud rather than silent: log it and tell
            # the user to retry. No thread has been created yet, so a retry is
            # clean (no duplicate-thread risk).
            logger.warning(
                "task command arrived before gateway ready; dropping message_id=%s channel_id=%s",
                message.id,
                channel.id,
            )
            await self._reply_best_effort(
                message,
                "I'm still starting up and can't open a task thread yet — give me "
                "a few seconds and run `/task` again.",
            )
            return True

        # Open a public thread anchored on the user's own message. This is the
        # "Start Thread from Message" API — the thread shares the message id,
        # and the anchor stays the user's genuinely-authored message.
        try:
            thread = await message.create_thread(name=_thread_name_from_text(body))
        except discord.DiscordException:
            logger.exception(
                "task: failed to create thread channel_id=%s message_id=%s",
                channel.id,
                message.id,
            )
            await self._reply_best_effort(
                message,
                "Couldn't open a task thread (Discord rejected it). I may be "
                "missing the Create Public Threads permission here, or this "
                "message already has a thread.",
            )
            return True

        # Route the task ambiently; replies and live-step progress land in the
        # new thread (its source_channel_id differs from the parent channel_id).
        wire = self._message_normalizer.normalize_task(message, thread_id=thread.id)
        try:
            await self._ingress.handle(wire)
        except AmbientRosterEmptyError:
            logger.info(
                "task: created thread but roster empty channel_id=%s thread_id=%s event_id=%s",
                channel.id,
                thread.id,
                wire.event_id,
            )
            await self._reply_best_effort(
                message,
                f"Opened the task thread ({thread.jump_url}), but no assistant "
                "agents are configured to work it. Contact an operator to add one.",
            )
            return True
        except Exception:
            logger.exception(
                "task: ingress publish failed channel_id=%s thread_id=%s event_id=%s",
                channel.id,
                thread.id,
                wire.event_id,
            )
            await self._reply_best_effort(
                message,
                f"Opened the task thread ({thread.jump_url}), but dispatching it to "
                "the agents failed. The thread already exists, so don't re-run "
                "`/task` (that would create a duplicate) — an operator should check "
                f"the bridge logs for event `{wire.event_id}`.",
            )
            return True

        logger.info(
            "task dispatched channel_id=%s thread_id=%s anchor_id=%s event_id=%s",
            channel.id,
            thread.id,
            message.id,
            wire.event_id,
        )
        return True

    async def _reply_best_effort(self, message: discord.Message, text: str) -> None:
        """Inline-reply to ``message``, logging and swallowing Discord errors.

        Shared by the ``/task`` branches. A failed reply is in an error path
        with nowhere useful to escalate, so it is logged and swallowed rather
        than raised. Catches the broad ``DiscordException`` (not just
        ``HTTPException``): this helper is the sole user-feedback sink for every
        ``/task`` failure, and a non-HTTP Discord error here (e.g. a
        ``ConnectionClosed`` from the very gateway turbulence that triggered the
        failure being reported) would otherwise escape into discord.py's event
        dispatcher and be swallowed there with no log — the textbook silent
        failure. Swallowing it here keeps the log line and never crashes the
        handler.
        """
        try:
            await message.reply(text)
        except discord.DiscordException:
            logger.exception("failed to send task reply message_id=%s", message.id)

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
        known_specs = [s for s in self._registry.all() if s.role != "router"]
        known_part = (
            f"Known agents: {', '.join(f'`@{s.agent_id}`' for s in known_specs)}."
            if known_specs
            else "No agents are currently registered."
        )
        text = f"No agent matches {bad}. {known_part} Please fix the mention and resend the message."
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


async def _run(settings: DiscordSettings, registry: AgentRegistry, server_urls: str) -> None:
    """Boot the bridge and serve until SIGINT/SIGTERM.

    Resource nesting is load-bearing: the persona sender, calfkit client, and
    transcript store wrap the :class:`~calfkit.Worker` so they stay open while
    the broker drains consumers (the outbox + steps consumers) that still use
    them. Unlike the four standalone runners, the bridge does NOT hand the whole
    lifecycle to :meth:`Worker.run` — it embeds a foreground (the Discord
    gateway WebSocket owns the loop), so it drives ``async with worker:`` (which
    owns register → provision node topics → broker.start on enter, drain on
    exit) while keeping its OWN signal handling and the gateway/stop race inside
    that block. ``start()``/``stop()`` install no signal handlers by design, so
    the bridge stays the owner of shutdown ordering.
    """
    # The bridge owns its own persona sender (separate from agent processes')
    # because it posts replies on behalf of every agent. The calfkit client
    # connects with a named reply topic so calfkit's reply dispatcher hears
    # every agent ReturnCall — even though we no longer await its futures
    # (the outbox consumer below handles every reply), the dispatcher's
    # subscriber is still registered as a side-effect of Client.connect.
    # Its "no pending future" WARNINGs on every reply are expected; see
    # calfcord.bridge.outbox.
    # Kept as nested ``async with`` (not combined) so each context's
    # rationale comment stays attached to it and the degrade/lifecycle reads
    # top-to-bottom; the noqa silences SIM117's "combine them" hint.
    async with DiscordPersonaSender(settings) as persona_sender:  # noqa: SIM117
        async with Client.connect(
            server_urls, reply_topic=_REPLY_TOPIC, provisioning=PROVISIONING
        ) as calfkit_client:
            pending_wires = PendingWires()
            ingress = BridgeIngress(
                calfkit_client=calfkit_client,
                registry=registry,
                pending_wires=pending_wires,
            )
            # The transcript store is the bridge's persistence layer for
            # per-turn agent step transcripts: the outbox consumer writes
            # a row on each tool-using terminal hop, and the
            # StepsToggleView reads it back when a user clicks a reply's
            # expand toggle. Opened/closed as an async context so the
            # single long-lived aiosqlite connection's lifetime brackets
            # the consumer setup and the gateway run loop — and, crucially,
            # outlives the broker drain so a draining consumer can still
            # write its final transcript row.
            async with _open_transcript_store(settings) as transcript_store:
                # Inject the now-open store into the ingress so the
                # slash-history builder can splice each agent's prior
                # tool calls/returns into its reconstructed
                # ``message_history`` (tool-call replay, plan §7.6).
                # Mirrors ``set_fetcher`` — both are post-construction
                # injections that degrade gracefully while unset. Done
                # right after the connection opens and before the
                # gateway/consumers are built.
                ingress.set_transcript_store(transcript_store)
                # Retention: drop transcript rows older than the
                # configured window on startup. The bridge is the sole
                # writer and restarts on every deploy, so a startup sweep
                # bounds growth without a background task. Best-effort and
                # disabled when the setting is <= 0 — see
                # :func:`_prune_on_startup`.
                await _prune_on_startup(transcript_store, settings)
                # Construct the gateway early so its SlashCommandManager
                # exists before we register the state consumer — the
                # state consumer's callbacks must point at
                # slash.schedule_resync so first-seen / departure events
                # trigger debounced slash re-registration.
                # Typing-indicator firer for the steps consumer's per-hop
                # fire. Built from the persona sender's started REST client;
                # fire-and-forget so it never blocks the serial steps
                # consumer. The gateway deliberately does NOT fire typing —
                # only genuine, non-terminal agent work (a steps hop) raises
                # the indicator, so it never lingers past the final reply.
                # See discord/typing.py.
                typing_notifier = TypingNotifier(persona_sender.client)
                gateway = DiscordIngressGateway(
                    settings, ingress, registry, calfkit_client, transcript_store
                )
                consumer_node = build_outbox_consumer(
                    persona_sender=persona_sender,
                    registry=registry,
                    pending_wires=pending_wires,
                    calfkit_client=calfkit_client,
                    transcript_store=transcript_store,
                )
                # The synthesized-in consumer subscribes to
                # ``bridge.synthesized.in`` and re-feeds router fan-out
                # wires through the same ingress handler real Discord
                # events use. Co-tenants on the same Worker so it
                # shares the bridge's calfkit Client + broker (and the
                # same consumer-group-per-node-id contract).
                synthesized_node = build_synthesized_consumer(ingress)
                # The steps consumer subscribes to ``agent.steps`` (which
                # every assistant agent's ``publish_topic`` mirrors every
                # hop to) and projects intermediate text / tool calls /
                # tool results live. It posts/edits ONE transient
                # in-channel progress message (``⚙ running… N steps``)
                # under the agent persona and deletes it on the terminal
                # hop — no DB access here. The durable transcript + expand
                # toggle ride the outbox's final reply instead.
                steps_state = StepsState()
                steps_node = build_steps_consumer(
                    persona_sender=persona_sender,
                    registry=registry,
                    pending_wires=pending_wires,
                    steps_state=steps_state,
                    typing_notifier=typing_notifier,
                )

                # The Worker owns the broker lifecycle: ``async with worker``
                # registers handlers, provisions the nodes' own topics, and
                # starts the broker on enter (consumer groups join BEFORE we
                # accept Discord events — the Gap-2 join-before-serve
                # correctness is now a guarantee of start(), not hand-built),
                # then drains the broker on exit.
                worker = Worker(calfkit_client, [consumer_node, synthesized_node, steps_node])

                # Register the state-event projection subscriber on the broker
                # BEFORE the worker starts. It is a RAW subscriber (not a worker
                # node), so register_handlers() leaves it intact and the single
                # broker.start() inside the worker's start() joins its consumer
                # group together with the worker's own node subscribers. State
                # events from already-running agents arrive after on_ready
                # publishes the discovery ping; the subscriber must be live by
                # then. ``schedule_resync`` is a bound method whose signature
                # matches the consumer callback shape (``(agent_id: str) ->
                # None``), so pass it directly.
                #
                # Imported here (not at module top) to avoid a circular import:
                # ``state_consumer`` imports ``AgentRegistry``, which re-exports
                # through ``bridge.__init__``, which imports this module.
                from calfcord.control_plane.state_consumer import (
                    register_state_consumer,
                )

                register_state_consumer(
                    calfkit_client,
                    registry,
                    on_first_seen=gateway._slash.schedule_resync,
                    on_departed=gateway._slash.schedule_resync,
                )

                # Provision the control-plane topics node-walking can't see:
                # agent.state (a raw subscriber) and bridge.discovery (the
                # discovery ping is published at boot, before any agent may be
                # up). The worker provisions its own node topics inside start().
                # No-ops on an auto-creating broker; required on Tansu. (The
                # client reply topic provision_infra also covers is, for the
                # bridge, discord.outbox — already the outbox node's inbox, so
                # redundant-by-construction here; see _provisioning.)
                await provision_infra(calfkit_client, extra_topics=bridge_infra_topics())

                async with worker:
                    # The bridge embeds a foreground (the Discord gateway), so
                    # it owns its own signal handling + the gateway/stop race —
                    # start()/stop() install no signals by design.
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
                        # Stop the DISCORD ingress first so no new events arrive
                        # while the broker drains. The synthesized consumer is a
                        # second ingress; it drains with the broker (safe — the
                        # transcript store closes last, outermost). The broker
                        # itself drains at the ``async with worker`` exit below.
                        for t in (gateway_task, stop_task):
                            if not t.done():
                                t.cancel()
                        await gateway.close()
                # worker.stop() drained the broker HERE — the steps/outbox
                # consumers finished their in-flight hops, still using
                # persona_sender + typing_notifier (both still open). Only NOW
                # is it safe to cancel typing tasks: TypingNotifier.aclose()
                # cancels in-flight tasks, so closing it before the drain (as
                # the pre-0.5.4 code did) could fire a draining steps hop into a
                # cancelled notifier. Closing after the drain eliminates that
                # race; persona/client/store still close after this (outermost).
                await typing_notifier.aclose()


def main() -> None:
    """CLI entry point. Loads config, constructs the registry, runs until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise SystemExit("DISCORD_GUILD_ID is required (global slash sync is too slow for dev)")

    # Bridge no longer reads agents/*.md — agents announce themselves
    # over Kafka via the control plane. Bootstrap with only the
    # locally-built router; the state consumer fills in the rest as
    # agents' startup announcements (and the on_ready discovery ping
    # replies) arrive.
    registry = AgentRegistry([build_router_definition()])

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    asyncio.run(_run(settings, registry, server_urls))


if __name__ == "__main__":
    main()
