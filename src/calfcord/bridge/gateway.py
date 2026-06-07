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
The bridge is calfcord's single deliberate **embedded** Worker variant: it
co-runs the Discord gateway (a foreground WebSocket) and owns SIGINT/SIGTERM,
so it drives the worker via :meth:`Worker.start` / :meth:`Worker.stop`
(signals opted OUT) rather than :meth:`Worker.run` (which would own the
foreground and install a colliding signal set) — see :func:`main`.
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
from datetime import UTC, datetime
from pathlib import Path

import discord
from calfkit.client import Client
from calfkit.worker import Worker
from calfkit.worker.lifecycle import LifecycleContext

from calfcord._provisioning import PROVISIONING, bridge_infra_topics
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
from calfcord.health.heartbeat import write_beat
from calfcord.health.refresher import run_refresher
from calfcord.router.definition import build_router_definition
from calfcord.topics import DISCORD_OUTBOX_TOPIC

logger = logging.getLogger(__name__)

_REPLY_TOPIC = DISCORD_OUTBOX_TOPIC

# The component name the bridge writes its heartbeat under and that
# ``calfcord _healthcheck bridge`` reads back (design §4.2 / §12.1).
_HEALTH_COMPONENT = "bridge"

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


def _resolve_health_home() -> Path:
    """Resolve the install home the heartbeat lands under, matching the reader.

    The ``calfcord _healthcheck bridge`` probe resolves the beat directory as
    ``_resolve_home() or Path()`` (``cli/main.py``): ``$CALFCORD_HOME`` when the
    shim exported it, else the launch directory. The bridge MUST mirror that
    exact resolution so the beat it writes lands where the probe looks for it; an
    empty ``CALFCORD_HOME=`` counts as unset (same guard as the CLI) rather than
    rooting state at ``/state/health``.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else Path()


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

        # Discord-connection liveness (design §12.1): the bridge heartbeat must
        # mean "connected to Discord", not merely "process up". ``_connected``
        # flips True on on_ready / on_resumed and False on on_disconnect; the
        # timer-refresher in ``main`` gates every beat write on it, so a dropped
        # gateway ages the beat past its TTL instead of staying falsely green.
        # ``_bot_identity`` is the display string (name + numeric id, never a
        # token — §12.3) the refresher stamps each beat with once we are ready.
        self._connected: bool = False
        self._bot_identity: str | None = None
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

    @property
    def connected(self) -> bool:
        """Whether the Discord gateway is currently connected (§12.1).

        This is the predicate the heartbeat refresher gates each write on, so it
        reflects the *live* websocket state: True between on_ready/on_resumed and
        the next on_disconnect. Read-only by design — only the lifecycle handlers
        mutate it.
        """
        return self._connected

    @property
    def bot_identity(self) -> str | None:
        """The bot's display identity (``name (id)``), or ``None`` before ready.

        Always a display string — never a token (§12.3). The refresher passes
        this through to each beat's ``identity`` field so ``status`` / ``doctor``
        can show *which* bot is connected.
        """
        return self._bot_identity

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

        # Discord is connected as of on_ready — record liveness and the display
        # identity, then write the FIRST heartbeat BEFORE slash-sync (§12.1 /
        # §13.3). Slash-sync can be slow or 429 on a cold tree; gating readiness
        # on it would let a transient Discord-side hiccup fail or delay the
        # "bridge healthy" signal even though the gateway is fully connected. The
        # beat lands in the same ``<home>/state/health/`` the ``calfcord
        # _healthcheck bridge`` probe reads. ``identity`` is a display string
        # (name + numeric id), never the token (§12.3).
        self._connected = True
        self._bot_identity = f"{bot_user} ({bot_user.id})"
        try:
            write_beat(
                _resolve_health_home(),
                _HEALTH_COMPONENT,
                status="healthy",
                identity=self._bot_identity,
                now=datetime.now(UTC),
            )
        except Exception:
            # A heartbeat write failure (read-only volume / disk full / EACCES)
            # must NOT break bridge boot: skip the beat (it ages to "not ready" at
            # the probe, which is correct) and continue to slash-sync + discovery.
            # The timer-refresher retries the write on its next tick.
            logger.exception("failed to write initial bridge heartbeat; continuing boot")

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

    async def _on_disconnect(self) -> None:
        """Mark the bridge disconnected when the Discord gateway drops (§12.1).

        discord.py fires ``on_disconnect`` whenever the websocket connection is
        lost — a revoked token, a network blip, or a normal session expiry. While
        disconnected the bot cannot post replies, so the heartbeat MUST go stale:
        flipping the flag stops the refresher feeding the beat, which ages past
        its TTL and turns the silent failure into a "not ready" probe verdict
        instead of a green light that lies. discord.py auto-reconnects, so this is
        often transient; on_resumed / on_ready restore the flag.
        """
        self._connected = False
        logger.warning("discord gateway disconnected; bridge heartbeat will go stale until reconnect")

    async def _on_resumed(self) -> None:
        """Mark the bridge connected again when a dropped session resumes (§12.1).

        discord.py fires ``on_resumed`` when it transparently resumes a session
        after a disconnect (no full re-identify, so on_ready does NOT fire). The
        bot can post again, so restore liveness here too — otherwise the beat
        would stay stale after every routine resume.
        """
        self._connected = True
        logger.info("discord gateway resumed; bridge heartbeat restored")

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

    async def on_disconnect(self) -> None:
        # discord.py fires this on every websocket drop (transient or terminal).
        # Delegate so the gateway can mark the heartbeat stale (§12.1).
        await self._gateway._on_disconnect()

    async def on_resumed(self) -> None:
        # discord.py fires this when a dropped session resumes WITHOUT a full
        # re-identify (so on_ready won't fire); delegate to restore liveness.
        await self._gateway._on_resumed()

    async def on_message(self, message: discord.Message) -> None:
        await self._gateway._on_message(message)


def _register_blind_spot_topics(worker: Worker, client: Client) -> None:
    """Declare the bridge's blind-spot topics into the managed pre-start pass.

    The bridge's three nodes (outbox / synthesized / steps) have their topics —
    plus the framework return inboxes and the client reply topic — auto-declared
    by the managed ``Worker.start()`` lifecycle and the connect-time pre-start
    hook. Two control-plane topics the bridge touches RAW are invisible to that
    node-walk and must still exist before their first use on a no-auto-create
    broker (Tansu):

    * ``agent.state`` — consumed by the raw state-consumer subscriber (not a
      Worker node), registered before ``worker.start()``.
    * ``bridge.discovery`` — *published* at boot by the on_ready discovery ping,
      possibly before any agent is up to create it.

    Both are declared here as a single ``on_startup`` hook (resource phase, which
    runs BEFORE ``broker.start()``), so calfkit's single pre-start provisioning
    pass creates them alongside the node topics — before the state consumer's
    group joins or the discovery ping publishes. This keeps the WHICH-topics
    DOMAIN concern in one named place, separate from the worker LIFECYCLE that
    schedules it; it mirrors the agents runner's blind-spot hook on the embedded
    surface. The standalone ``provision_extra_topics`` is the alternative but
    opens a second admin connection rather than reusing the broker's.
    """

    @worker.on_startup
    async def _declare(ctx: LifecycleContext[Worker]) -> None:
        client._startup_ensurer.declare(bridge_infra_topics())


def main() -> None:
    """CLI entry point. Loads config, constructs the gateway, runs until SIGINT/SIGTERM."""
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

    async def _run() -> None:
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
                # the consumer setup and the gateway run loop.
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

                    bridge_nodes = [consumer_node, synthesized_node, steps_node]
                    worker = Worker(calfkit_client, bridge_nodes)
                    # Declare the bridge's blind-spot topics (agent.state,
                    # bridge.discovery) into the client's startup ensurer via a
                    # pre-broker-start hook, so calfkit's single provisioning pass
                    # creates them alongside the node topics + reply topic before
                    # any subscriber consumes — required on a no-auto-create
                    # broker (Tansu). See :func:`_register_blind_spot_topics`.
                    _register_blind_spot_topics(worker, calfkit_client)

                    # Register the raw state-event projection subscriber on the
                    # broker BEFORE ``worker.start()`` (which starts the broker).
                    # register-before-serve is load-bearing: this consumer group
                    # — and the three node groups ``worker.start()`` joins — must
                    # join before the gateway accepts Discord events AND before
                    # the on_ready discovery ping publishes, or a state event /
                    # reply arriving in the gap is LOST (auto_offset_reset=latest).
                    # State events from already-running agents arrive in reply to
                    # that ping, so the subscriber must be live by then.
                    # ``schedule_resync`` is a bound method whose signature
                    # matches the consumer callback shape (``(agent_id) -> None``),
                    # so pass it directly.
                    #
                    # Imported here (not at module top) to avoid a circular
                    # import: ``state_consumer`` imports ``AgentRegistry``,
                    # which re-exports through ``bridge.__init__``, which
                    # imports this module.
                    from calfcord.control_plane.state_consumer import (
                        register_state_consumer,
                    )

                    register_state_consumer(
                        calfkit_client,
                        registry,
                        on_first_seen=gateway._slash.schedule_resync,
                        on_departed=gateway._slash.schedule_resync,
                    )

                    try:
                        # Embedded managed lifecycle: ``worker.start()`` runs the
                        # on_startup hooks (register the node handlers + declare
                        # the node/blind-spot topics) → ``broker.start()``
                        # (calfkit's pre-start hook provisions everything declared,
                        # then EVERY registered consumer group — the three nodes
                        # AND the raw state consumer above — joins) →
                        # after_startup. It does NOT install signal handlers (that
                        # is the ``Worker.run()`` surface), so the bridge keeps
                        # SIGINT/SIGTERM ownership for its own foreground (the
                        # Discord gateway). Inside the ``try`` so the ``finally``'s
                        # ``stop()`` always runs — a no-op if the worker never
                        # started, idempotent otherwise — so neither a failed boot
                        # nor a clean run ever leaks the broker connection.
                        await worker.start()

                        stop = asyncio.Event()
                        loop = asyncio.get_running_loop()
                        for sig in (signal.SIGINT, signal.SIGTERM):
                            loop.add_signal_handler(sig, stop.set)

                        gateway_task = asyncio.create_task(gateway.start())
                        stop_task = asyncio.create_task(stop.wait())

                        # Keep the bridge heartbeat fresh on a timer for the
                        # whole run (design §12.1). ``_on_ready`` writes the first
                        # beat synchronously (before slash-sync); this task
                        # refreshes it every few seconds, but gated on
                        # ``gateway.connected`` — so a dropped Discord gateway
                        # stops the writes and the beat ages past its TTL rather
                        # than lying green. Identity is the bot display string the
                        # getter resolves once ready (never a token, §12.3).
                        # Started AFTER the gateway task so the loop is only alive
                        # while the gateway is; cancelled in the finally below
                        # (``run_refresher`` swallows CancelledError cleanly).
                        refresher_task = asyncio.create_task(
                            run_refresher(
                                _resolve_health_home(),
                                _HEALTH_COMPONENT,
                                is_healthy=lambda: gateway.connected,
                                identity=lambda: gateway.bot_identity,
                            )
                        )
                        try:
                            done, _ = await asyncio.wait(
                                {gateway_task, stop_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            # A FATAL gateway crash (not a shutdown signal) must
                            # surface as a non-zero exit so the supervisor restarts
                            # us — ``asyncio.wait`` does NOT propagate a task's
                            # exception, so without this the bridge would exit 0, a
                            # crash masquerading as a clean stop. On the signal path
                            # stop_task wins the race and gateway_task is still
                            # running (not in ``done``), so it is cancelled in the
                            # finally instead.
                            if gateway_task in done and not gateway_task.cancelled():
                                exc = gateway_task.exception()
                                if exc is not None:
                                    raise exc
                        finally:
                            for t in (gateway_task, stop_task, refresher_task):
                                if not t.done():
                                    t.cancel()
                            # Await the cancelled refresher so its task is
                            # retrieved here (no "Task was destroyed but it is
                            # pending" warning). ``run_refresher`` catches
                            # CancelledError and returns cleanly, so this await
                            # does NOT re-raise.
                            await refresher_task
                            await gateway.close()
                    finally:
                        # Ordered shutdown: the gateway ingress is already closed
                        # (inner finally above); drain the broker, THEN close the
                        # typing notifier.
                        #
                        # Drain (``worker.stop``) runs after ``gateway.close`` so any
                        # in-flight agent reply on ``discord.outbox`` posts before the
                        # broker disconnects. ``stop()`` runs the worker's on_shutdown
                        # → broker.stop (drain) → after_shutdown and is a no-op if the
                        # worker never started, so this is safe on a failed boot.
                        #
                        # ``typing_notifier.aclose()`` runs AFTER the drain: a
                        # steps-consumer hop draining here can still ``fire()`` typing.
                        # ``aclose`` cancels + awaits only the typing tasks live at
                        # that instant; it does NOT disable the notifier (``fire`` has
                        # no closed guard). So if it ran BEFORE the drain, a hop firing
                        # during the drain would spawn a fresh typing task ``aclose``
                        # can no longer track or cancel — left dangling at loop
                        # shutdown. Running it after the drain means every fired task
                        # is accounted for. The inner ``try/finally`` keeps the close
                        # unconditional even if the drain raises. All bracketed INSIDE
                        # the persona / connection / transcript ``async with``
                        # contexts, so the notifier's underlying client is still open.
                        try:
                            await worker.stop()
                        finally:
                            await typing_notifier.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
