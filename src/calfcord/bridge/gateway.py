"""Discord ingress gateway daemon and CLI entry point.

Holds the long-lived gateway WebSocket and wires the per-``@mention`` orchestration
together on the calfkit 0.12 **caller surface**. Run via::

    uv run calfkit-bridge

The daemon depends on a running Kafka broker reachable at ``CALF_HOST_URL``
(defaults to ``localhost``) and a Discord bot configured via the ``DISCORD_*``
environment variables (see ``.env.example``).

The bridge is a pure calfkit :class:`~calfkit.client.Client` (no embedded Worker,
no consumers). For each ``@mention`` it builds a :class:`MentionRequest` and runs
:class:`~calfcord.bridge.mention_handler.MentionHandler.handle` as a tracked task:
the handler resolves the target against the live mesh roster, ``start()``s the
agent, drains its run ``stream()`` (live progress + A2A projection), and posts the
terminal reply under the responding agent's persona. Non-``@mention`` ("ambient")
messages go unanswered (C2). The bridge owns SIGINT/SIGTERM for its foreground
(the Discord gateway) and tears down by cancelling in-flight handler tasks then
closing the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import discord
from calfkit.client import Client, MeshViewConfig

from calfcord._provisioning import PROVISIONING
from calfcord.agents.memory import MemoryPromptDeps
from calfcord.bridge.a2a_project import A2AProjector
from calfcord.bridge.egress import A2AChannelResolver
from calfcord.bridge.history import ChannelHistoryFetcher, DiscordHistoryProvider
from calfcord.bridge.mention_handler import MentionHandler, MentionRequest
from calfcord.bridge.normalizer import MessageNormalizer, extract_mention_ids
from calfcord.bridge.overrides import EffortOverrides
from calfcord.bridge.progress import ProgressRenderer
from calfcord.bridge.reply_poster import ReplyPoster
from calfcord.bridge.roster import MeshRoster
from calfcord.bridge.slash import SlashCommandManager
from calfcord.bridge.steps_toggle import StepsToggleView
from calfcord.bridge.transcripts import (
    NullTranscriptStore,
    TranscriptStore,
    TranscriptStoreLike,
)
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.discord.settings import DiscordSettings
from calfcord.discord.typing import TypingNotifier
from calfcord.health.heartbeat import write_beat
from calfcord.health.refresher import run_refresher

logger = logging.getLogger(__name__)

# The component name the bridge writes its heartbeat under and that
# ``disco _healthcheck bridge`` reads back (design §4.2 / §12.1).
_HEALTH_COMPONENT = "bridge"

_SEEN_MESSAGE_IDS_CAPACITY = 1024

# Durable, fixed inbox topic for the bridge's caller surface. A stable name
# (vs an auto-generated per-restart one) avoids leaking orphan topics on a
# no-auto-delete broker (Tansu); only one bridge runs, so there is no contention.
_BRIDGE_INBOX_TOPIC = "discord.bridge.inbox"

# Mesh liveness staleness (R-A6): the calfkit default of 3x30s heartbeats. A
# gracefully-stopped agent tombstones immediately; this only gates the window
# after an ungraceful crash.
_MESH_STALE_AFTER_SECONDS = 90.0

# A2A audit channel/category, moved from the tools service to the bridge (spec §10).
_A2A_CHANNEL_NAME_ENV = "CALFKIT_A2A_CHANNEL_NAME"
_A2A_CHANNEL_CATEGORY_ENV = "CALFKIT_A2A_CHANNEL_CATEGORY"
_A2A_CHANNEL_NAME_DEFAULT = "private-a2a-chats"


def _a2a_channel_name() -> str:
    """The unified A2A audit channel name (``CALFKIT_A2A_CHANNEL_NAME`` or default)."""
    value = os.getenv(_A2A_CHANNEL_NAME_ENV)
    return value.strip() if value and value.strip() else _A2A_CHANNEL_NAME_DEFAULT


def _a2a_category_name() -> str | None:
    """The optional Discord category for the A2A channel, or ``None`` when unset."""
    value = os.getenv(_A2A_CHANNEL_CATEGORY_ENV)
    return value.strip() if value and value.strip() else None


def _resolve_health_home() -> Path:
    """Resolve the install home the heartbeat lands under, matching the reader.

    The ``disco _healthcheck bridge`` probe resolves the beat directory as
    ``_resolve_home() or Path()`` (``cli/main.py``): ``$CALFCORD_HOME`` when the
    shim exported it, else the launch directory. The bridge MUST mirror that
    exact resolution so the beat it writes lands where the probe looks for it; an
    empty ``CALFCORD_HOME=`` counts as unset (same guard as the CLI) rather than
    rooting state at ``/state/health``.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else Path()


@asynccontextmanager
async def _open_transcript_store(settings: DiscordSettings) -> AsyncIterator[TranscriptStoreLike]:
    """Open the transcript store, degrading to a no-op store on failure.

    Constructs the real :class:`TranscriptStore` and connects it. If the open
    fails (bad path, disk error, corrupt DB, …) the bridge MUST NOT abort — a
    crash here would take down all Discord routing, not just transcripts. Instead
    we log a loud ERROR and substitute a :class:`NullTranscriptStore` so the run
    continues with transcripts, tool-call replay, and the expand toggle disabled.
    Yields whichever store is in effect; the real store's connection (if any) is
    closed on exit, and ``NullTranscriptStore.close`` is a harmless no-op.
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

    The bridge is the sole writer and restarts on every deploy, so a startup
    prune bounds the store's growth without a background task. Disabled when
    ``transcript_retention_days <= 0`` (keep forever). Best-effort: a prune
    failure must NEVER abort startup — any exception is logged and swallowed.
    """
    if settings.transcript_retention_days <= 0:
        return
    try:
        cutoff = int(time.time()) - settings.transcript_retention_days * 86400
        pruned = await store.prune_older_than(cutoff)
        if pruned:
            logger.info("pruned %d transcript row(s) older than %d days", pruned, settings.transcript_retention_days)
    except Exception:
        logger.exception("transcript retention prune failed at startup; continuing")


class DiscordIngressGateway:
    """Long-lived gateway daemon. Translates Discord ``@mention``s into agent runs."""

    def __init__(
        self,
        settings: DiscordSettings,
        *,
        calfkit_client: Client,
        persona_sender: DiscordPersonaSender,
        transcript_store: TranscriptStoreLike,
        roster: MeshRoster,
        overrides: EffortOverrides,
        a2a: A2AProjector,
        progress: ProgressRenderer,
        reply: ReplyPoster,
        memory_deps: MemoryPromptDeps,
    ) -> None:
        self._settings = settings
        self._transcript_store = transcript_store
        self._reply = reply
        self._client = _GatewayClient(self)

        # The history fetcher holds the gateway's Discord client; it only calls
        # ``get_channel``/``fetch_channel`` at fetch time (inside ``_on_message``,
        # post-handshake), so constructing it here with the not-yet-connected
        # client is safe. Agent turns are recognized by bot-owned ``webhook_id``
        # (R-A3) via the persona sender's id set.
        fetcher = ChannelHistoryFetcher(self._client, persona_sender.owns_webhook)
        history = DiscordHistoryProvider(fetcher, transcript_store)
        self._handler = MentionHandler(
            client=calfkit_client,
            roster=roster,
            history=history,
            overrides=overrides,
            a2a=a2a,
            progress=progress,
            reply=reply,
            memory_deps=memory_deps,
        )

        # The MessageNormalizer needs bot_user_id, known only at on_ready.
        self._message_normalizer: MessageNormalizer | None = None
        self._bot_user_id: int | None = None

        # Discord-connection liveness (design §12.1): ``_connected`` flips True on
        # on_ready/on_resumed and False on on_disconnect; the timer-refresher gates
        # every beat write on it, so a dropped gateway ages the beat past its TTL.
        self._connected: bool = False
        self._bot_identity: str | None = None

        self._slash = SlashCommandManager(
            client=self._client,
            overrides=overrides,
            owner_user_id=settings.owner_user_id,
            guild_id=settings.guild_id,
        )
        self._slash.register_thinking_effort()
        self._slash.register_clear()

        # In-flight ``handle()`` tasks, tracked so shutdown cancels them before the
        # broker stops. A bounded LRU of Discord message ids dedupes redelivery
        # (discord.py can replay MESSAGE_CREATE on gateway reconnect).
        self._inflight: set[asyncio.Task[None]] = set()
        self._seen_message_ids: OrderedDict[int, None] = OrderedDict()

    @property
    def connected(self) -> bool:
        """Whether the Discord gateway is currently connected (§12.1)."""
        return self._connected

    @property
    def bot_identity(self) -> str | None:
        """The bot's display identity (``name (id)``), or ``None`` before ready (§12.3)."""
        return self._bot_identity

    async def start(self) -> None:
        """Connect to the Discord gateway. Blocks until cancelled or disconnect."""
        logger.info("DiscordIngressGateway starting (guild_id=%s)", self._settings.guild_id)
        await self._client.start(self._settings.bot_token.get_secret_value())

    async def close(self) -> None:
        """Disconnect the Discord gateway cleanly. Idempotent."""
        if not self._client.is_closed():
            await self._client.close()

    async def drain_inflight(self) -> None:
        """Cancel and await any in-flight ``handle()`` tasks (shutdown).

        Called before the calfkit client closes so a parked ``result()`` await is
        cancelled cleanly rather than erroring when the broker stops.
        """
        if not self._inflight:
            return
        tasks = list(self._inflight)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _on_ready(self) -> None:
        bot_user = self._client.user
        assert bot_user is not None, "on_ready fires after authentication completes"
        self._bot_user_id = bot_user.id
        self._message_normalizer = MessageNormalizer(human_owner_id=self._settings.owner_user_id)
        logger.info("gateway ready as %s (id=%s)", bot_user, bot_user.id)

        # Discord is connected as of on_ready — record liveness + identity, then
        # write the FIRST heartbeat BEFORE slash-sync (§12.1): slash-sync can be
        # slow / 429 on a cold tree, and readiness must not hinge on it.
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
            logger.exception("failed to write initial bridge heartbeat; continuing boot")

        await self._slash.sync(self._settings.guild_id)

        # Persistent view for the step-transcript expand toggle: one instance
        # handles every click carrying ``steps:toggle`` on any agent reply,
        # including replies posted before this restart (matched by custom_id).
        self._client.add_view(StepsToggleView(self._transcript_store))

    async def _on_disconnect(self) -> None:
        """Mark the bridge disconnected when the Discord gateway drops (§12.1)."""
        self._connected = False
        logger.warning("discord gateway disconnected; bridge heartbeat will go stale until reconnect")

    async def _on_resumed(self) -> None:
        """Mark the bridge connected again when a dropped session resumes (§12.1)."""
        self._connected = True
        logger.info("discord gateway resumed; bridge heartbeat restored")

    async def _on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if self._settings.guild_id is not None and message.guild.id != self._settings.guild_id:
            return
        if self._message_normalizer is None:
            return  # pre-ready; defensive
        # Skip the bot's own non-webhook posts (e.g. /clear markers, notices).
        # Webhook posts (the bot acting as an agent persona) flow through so the
        # author-stamping / peer-visibility paths see them.
        if self._bot_user_id is not None and message.author.id == self._bot_user_id and message.webhook_id is None:
            return
        if self._already_seen(message.id):
            logger.debug("ignoring redelivered message id=%s", message.id)
            return

        # Ambient (non-@mention) messages go unanswered (C2): skip them before any
        # work. The handler also no-ops on an empty mention list, but skipping here
        # avoids a needless task + mesh read per ambient message.
        mention_ids = extract_mention_ids(message.content)
        if not mention_ids:
            return

        try:
            wire = self._message_normalizer.normalize(message)
        except Exception:
            logger.exception("failed to normalize message id=%s", message.id)
            return

        req = MentionRequest(
            content=wire.content,
            mention_ids=mention_ids,
            author_label=wire.author.display_name,
            message_id=wire.message_id,
            source_channel_id=wire.source_channel_id or wire.channel_id,
            channel_id=wire.channel_id,
            wire=wire,
            reply_target=message,
        )
        self._spawn_handle(req)

    def _spawn_handle(self, req: MentionRequest) -> None:
        """Run one ``@mention`` as a tracked background task.

        Each mention is independent and may be long-running (an agent run + A2A
        consults), so it must not block the Discord event loop. The task is tracked
        in :attr:`_inflight` so shutdown can cancel it, and its result is reaped by
        :meth:`_on_handle_done` (which surfaces an unexpected crash to the user).
        """
        task = asyncio.create_task(self._run_handler(req))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _run_handler(self, req: MentionRequest) -> None:
        """Run the handler for ``req``, surfacing an unexpected crash to the user.

        The handler already posts user-facing notices for *expected* failures
        (roster unavailable, no agent online, fault, drop). An *unexpected* crash
        (a bug) would otherwise leave the user with silence, so post a generic
        best-effort notice. ``CancelledError`` (shutdown) propagates untouched.
        """
        try:
            await self._handler.handle(req)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mention handler crashed for message_id=%s", req.message_id)
            with contextlib.suppress(Exception):
                await self._reply.post_notice(
                    req,
                    "Something went wrong handling that message; please try again. "
                    "If this keeps happening, an operator should check the bridge logs.",
                )

    def _already_seen(self, message_id: int) -> bool:
        """Bounded-LRU dedupe of Discord ``message.id`` (reconnect redelivery)."""
        if message_id in self._seen_message_ids:
            self._seen_message_ids.move_to_end(message_id)
            return True
        self._seen_message_ids[message_id] = None
        if len(self._seen_message_ids) > _SEEN_MESSAGE_IDS_CAPACITY:
            self._seen_message_ids.popitem(last=False)
        return False


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
        await self._gateway._on_disconnect()

    async def on_resumed(self) -> None:
        await self._gateway._on_resumed()

    async def on_message(self, message: discord.Message) -> None:
        await self._gateway._on_message(message)


def main() -> None:
    """CLI entry point. Loads config, constructs the gateway, runs until SIGINT/SIGTERM."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise SystemExit("DISCORD_GUILD_ID is required (global slash sync is too slow for dev)")

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async def _run() -> None:
        # The bridge owns its persona sender (it posts on behalf of every agent).
        # The calfkit Client is a pure caller surface: a durable inbox + the mesh
        # view; no Worker, no consumers. Nested ``async with`` (not combined) keeps
        # each context's rationale attached to it.
        async with DiscordPersonaSender(settings) as persona_sender:  # noqa: SIM117
            async with Client.connect(
                server_urls,
                inbox_topic=_BRIDGE_INBOX_TOPIC,
                provisioning=PROVISIONING,
                mesh_config=MeshViewConfig(stale_after=_MESH_STALE_AFTER_SECONDS),
            ) as calfkit_client:
                # No eager broker pre-start here (D-11 revisited): the first
                # ``client.agent(name).start(...)`` self-ensures the broker BEFORE it
                # publishes — calfkit's ``AgentGateway.start`` awaits
                # ``_ensure_started`` (→ ``broker.start()``) ahead of ``_publish_call``,
                # and with ``provisioning=PROVISIONING`` that provisions the durable
                # inbox and starts its groupless reply subscriber consuming before the
                # request is sent, so a reply can't land on an unprovisioned/unconsumed
                # inbox (calfkit's ``_ensure_started`` docstring calls out exactly the
                # provisioning-enabled case). Nothing between here and the first mention
                # publishes to the broker, and the mesh roster read opens its own
                # independent reader — so there is nothing left to pre-start. (The CLI
                # probes DO keep an ``events()`` pre-start, but for a different reason:
                # there it doubles as the broker-reachability check.)
                async with _open_transcript_store(settings) as transcript_store:
                    await _prune_on_startup(transcript_store, settings)

                    typing_notifier = TypingNotifier(persona_sender.client)
                    overrides = EffortOverrides(transcript_store)
                    await overrides.hydrate()  # restore /thinking-effort overrides across restarts (D-8)
                    # A2A audit projection: the resolver only uses the sender's
                    # ``.client`` (a REST login), which the persona sender provides.
                    resolver = A2AChannelResolver(
                        persona_sender,
                        settings.guild_id,
                        channel_name=_a2a_channel_name(),
                        category_name=_a2a_category_name(),
                    )
                    gateway = DiscordIngressGateway(
                        settings,
                        calfkit_client=calfkit_client,
                        persona_sender=persona_sender,
                        transcript_store=transcript_store,
                        roster=MeshRoster(calfkit_client),
                        overrides=overrides,
                        a2a=A2AProjector(resolver, persona_sender),
                        progress=ProgressRenderer(persona_sender, typing_notifier),
                        reply=ReplyPoster(persona_sender, transcript_store),
                        memory_deps=MemoryPromptDeps(),
                    )
                    try:
                        stop = asyncio.Event()
                        loop = asyncio.get_running_loop()
                        for sig in (signal.SIGINT, signal.SIGTERM):
                            loop.add_signal_handler(sig, stop.set)

                        gateway_task = asyncio.create_task(gateway.start())
                        stop_task = asyncio.create_task(stop.wait())
                        # Refresh the bridge heartbeat on a timer, gated on the live
                        # Discord connection so a dropped gateway ages the beat (§12.1).
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
                            # A fatal gateway crash (not a signal) must surface as a
                            # non-zero exit so the supervisor restarts us; asyncio.wait
                            # does not propagate a task's exception.
                            if gateway_task in done and not gateway_task.cancelled():
                                exc = gateway_task.exception()
                                if exc is not None:
                                    raise exc
                        finally:
                            for t in (gateway_task, stop_task, refresher_task):
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(refresher_task, return_exceptions=True)
                            await gateway.close()
                    finally:
                        # Cancel in-flight handler tasks BEFORE the client context
                        # exits (which closes the broker), then close typing — a
                        # cancelled run can still have fired a typing task.
                        await gateway.drain_inflight()
                        await typing_notifier.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
