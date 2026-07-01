"""Unit tests for the gateway's ``_on_message`` intake and handler-task lifecycle.

Post-0.12 the gateway is a pure caller surface: for each ``@mention`` it builds a
:class:`MentionRequest` and runs :meth:`MentionHandler.handle` as a tracked
asyncio task. There is no ingress/outbox/Worker anymore. These tests pin the
intake seam and the task machinery, all offline (no Discord, no broker):

* **Filtering** — DMs, wrong-guild, pre-ready, the bot's own non-webhook posts
  (e.g. ``/clear`` markers, notices), and ambient (non-``@mention``) messages are
  dropped before a handler task is ever spawned (C2). A webhook post carrying the
  bot's user id (an agent persona) is NOT self-filtered.
* **Dedupe** — a redelivered ``MESSAGE_CREATE`` (same ``message.id``) spawns the
  handler only once.
* **Spawn** — a real ``@mention`` reaches ``handler.handle`` with a correctly
  populated :class:`MentionRequest` (mention ids, author label, channel flattening,
  reply target, the serialized wire).
* **Crash isolation** — an *unexpected* handler exception posts a generic notice
  via the reply poster; ``CancelledError`` (shutdown) propagates untouched.
* **Drain** — ``drain_inflight`` cancels in-flight handler tasks at shutdown.

The gateway is built with mocked collaborators; ``_handler`` is swapped for a
recording/failing fake so a mention only has to REACH ``handler.handle``. The
``_GatewayClient`` constructor is sync + offline, so no network is touched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from calfcord.bridge.gateway import DiscordIngressGateway
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.normalizer import MessageNormalizer
from calfcord.bridge.steps_toggle import StepsToggleView
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.settings import DiscordSettings

_GUILD_ID = 5678
_BOT_USER_ID = 555
_OWNER_USER_ID = 9999


def _settings() -> DiscordSettings:
    return DiscordSettings(
        bot_token=SecretStr("test-bot-token"),
        application_id=1234,
        guild_id=_GUILD_ID,
        owner_user_id=_OWNER_USER_ID,
    )


def _gateway() -> DiscordIngressGateway:
    """A real gateway with mocked collaborators and a stubbed ``add_view``."""
    gateway = DiscordIngressGateway(
        _settings(),
        calfkit_client=MagicMock(),
        persona_sender=MagicMock(),
        transcript_store=MagicMock(),
        roster=MagicMock(),
        overrides=MagicMock(),
        a2a=MagicMock(),
        progress=MagicMock(),
        reply=MagicMock(),
        memory_deps=MagicMock(),
    )
    gateway._client.add_view = MagicMock()  # type: ignore[method-assign]
    return gateway


def _ready(gateway: DiscordIngressGateway) -> None:
    """Put the gateway into its post-``on_ready`` state without a live handshake.

    ``_on_message`` no-ops until the normalizer + bot user id are set on ready;
    setting them directly is how we exercise intake in isolation (no client.user).
    """
    gateway._message_normalizer = MessageNormalizer(_OWNER_USER_ID)
    gateway._bot_user_id = _BOT_USER_ID


class _RecordingHandler:
    """A ``MentionHandler`` stand-in that records the requests handed to it."""

    def __init__(self) -> None:
        self.calls: list[MentionRequest] = []

    async def handle(self, req: MentionRequest) -> None:
        self.calls.append(req)


async def _settle(gateway: DiscordIngressGateway) -> None:
    """Await any spawned handler tasks so their effects are observable."""
    tasks = list(gateway._inflight)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _req() -> MentionRequest:
    """A minimal request for driving ``_run_handler`` / ``_spawn_handle`` directly."""
    return MentionRequest(
        content="@scribe hi",
        mention_ids=("scribe",),
        author_label="alice",
        message_id=1,
        source_channel_id=10,
        channel_id=10,
        wire=WireMessage(
            event_id="e1",
            kind="message",
            message_id=1,
            channel_id=10,
            source_channel_id=10,
            guild_id=1,
            content="@scribe hi",
            author=WireAuthor(discord_user_id=1, display_name="alice", is_bot=False, is_webhook=False),
            created_at=datetime.now(UTC),
        ),
        reply_target=object(),
    )


class _FakeBotUser:
    """A ``discord.Client.user`` stand-in: ``str()`` → name, ``.id`` → id."""

    def __init__(self, *, name: str = "Calfbot#1234", user_id: int = 42) -> None:
        self.id = user_id
        self._name = name

    def __str__(self) -> str:
        return self._name


class TestOnMessageSpawnsHandler:
    """A real ``@mention`` reaches ``handler.handle`` with a correct request."""

    async def test_mention_spawns_handler_with_populated_request(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(
            message_id=42,
            channel_id=200,
            guild_id=_GUILD_ID,
            author_display_name="Alice",
            content="@scribe help me",
        )
        await gateway._on_message(msg)
        await _settle(gateway)

        assert len(handler.calls) == 1
        req = handler.calls[0]
        assert req.mention_ids == ("scribe",)
        assert req.content == "@scribe help me"
        assert req.author_label == "Alice"
        assert req.message_id == 42
        assert req.channel_id == 200
        assert req.source_channel_id == 200
        assert req.reply_target is msg
        # The typed WireMessage rides along (the handler serializes it into
        # ``deps["discord"]``; the reply poster reads its typed fields).
        assert req.wire.content == "@scribe help me"
        assert req.wire.slash_target == "scribe"

    async def test_thread_message_flattens_parent_but_keeps_thread_source(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(channel_id=500, thread_parent_id=200, guild_id=_GUILD_ID, content="@scribe hi")
        await gateway._on_message(msg)
        await _settle(gateway)

        req = handler.calls[0]
        assert req.channel_id == 200, "parent channel hosts the persona webhook"
        assert req.source_channel_id == 500, "thread id drives history fetching"

    async def test_first_of_multiple_mentions_carried_in_order(self, fake_message) -> None:
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(guild_id=_GUILD_ID, content="@scribe loop in @echo")
        await gateway._on_message(msg)
        await _settle(gateway)

        assert handler.calls[0].mention_ids == ("scribe", "echo")


class TestOnMessageFilters:
    """Messages that must never spawn a handler task."""

    async def _dropped(self, gateway: DiscordIngressGateway, msg: Any) -> _RecordingHandler:
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        await gateway._on_message(msg)
        await _settle(gateway)
        return handler

    async def test_ambient_message_without_mention_is_ignored(self, fake_message) -> None:
        gateway = _gateway()
        _ready(gateway)
        handler = await self._dropped(gateway, fake_message(guild_id=_GUILD_ID, content="just chatting"))
        assert handler.calls == []

    async def test_own_non_webhook_message_is_ignored(self, fake_message) -> None:
        # The /clear marker and operator notices are the bot's own non-webhook
        # posts; re-ingesting them would fan the bot's own text back out to agents.
        gateway = _gateway()
        _ready(gateway)
        msg = fake_message(author_id=_BOT_USER_ID, webhook_id=None, guild_id=_GUILD_ID, content="@scribe hi")
        handler = await self._dropped(gateway, msg)
        assert handler.calls == []

    async def test_webhook_post_with_bot_id_passes_through(self, fake_message) -> None:
        # A webhook post (an agent persona) is NOT self-filtered even though it
        # carries the bot's user id — which is exactly why /clear posts its marker
        # as a plain, non-webhook message so the seam above can drop it.
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)
        msg = fake_message(author_id=_BOT_USER_ID, webhook_id=777, guild_id=_GUILD_ID, content="@scribe hi")
        await gateway._on_message(msg)
        await _settle(gateway)
        assert len(handler.calls) == 1

    async def test_dm_is_ignored(self, fake_message) -> None:
        gateway = _gateway()
        _ready(gateway)
        handler = await self._dropped(gateway, fake_message(guild_id=None, content="@scribe hi"))
        assert handler.calls == []

    async def test_wrong_guild_is_ignored(self, fake_message) -> None:
        gateway = _gateway()
        _ready(gateway)
        handler = await self._dropped(gateway, fake_message(guild_id=_GUILD_ID + 1, content="@scribe hi"))
        assert handler.calls == []

    async def test_pre_ready_message_is_ignored(self, fake_message) -> None:
        # Before on_ready there is no normalizer; intake must no-op defensively.
        gateway = _gateway()
        gateway._message_normalizer = None
        gateway._bot_user_id = None
        handler = await self._dropped(gateway, fake_message(guild_id=_GUILD_ID, content="@scribe hi"))
        assert handler.calls == []


class TestOnMessageDedup:
    async def test_redelivered_message_spawns_handler_once(self, fake_message) -> None:
        # discord.py can replay MESSAGE_CREATE on gateway reconnect; the bounded
        # LRU of message ids must collapse the duplicate.
        gateway = _gateway()
        handler = _RecordingHandler()
        gateway._handler = handler  # type: ignore[assignment]
        _ready(gateway)

        msg = fake_message(message_id=42, guild_id=_GUILD_ID, content="@scribe hi")
        await gateway._on_message(msg)
        await gateway._on_message(msg)  # redelivery of the SAME id
        await _settle(gateway)

        assert len(handler.calls) == 1


class TestRunHandlerErrorHandling:
    """``_run_handler`` isolates handler crashes from the Discord event loop."""

    async def test_unexpected_crash_posts_generic_notice(self) -> None:
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]

        class _Crash:
            async def handle(self, req: MentionRequest) -> None:
                raise RuntimeError("boom")

        gateway._handler = _Crash()  # type: ignore[assignment]
        req = _req()
        await gateway._run_handler(req)

        gateway._reply.post_notice.assert_awaited_once()
        posted_req, text = gateway._reply.post_notice.await_args.args
        assert posted_req is req
        assert "Something went wrong" in text
        # The notice must not leak internal detail.
        assert "RuntimeError" not in text

    async def test_cancelled_error_propagates_without_notice(self) -> None:
        # Shutdown cancellation must propagate so drain sees the task as cancelled;
        # it is NOT an "unexpected crash", so no user-facing notice is posted.
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock()  # type: ignore[method-assign]

        class _Cancels:
            async def handle(self, req: MentionRequest) -> None:
                raise asyncio.CancelledError

        gateway._handler = _Cancels()  # type: ignore[assignment]
        with pytest.raises(asyncio.CancelledError):
            await gateway._run_handler(_req())
        gateway._reply.post_notice.assert_not_awaited()

    async def test_notice_failure_is_swallowed(self) -> None:
        # Even the best-effort notice can fail (Discord down); that must not escape.
        gateway = _gateway()
        gateway._reply.post_notice = AsyncMock(side_effect=RuntimeError("discord down"))  # type: ignore[method-assign]

        class _Crash:
            async def handle(self, req: MentionRequest) -> None:
                raise RuntimeError("boom")

        gateway._handler = _Crash()  # type: ignore[assignment]
        await gateway._run_handler(_req())  # must not raise


class TestDrainInflight:
    async def test_drain_cancels_running_handler_tasks(self) -> None:
        gateway = _gateway()
        started = asyncio.Event()

        class _Blocks:
            async def handle(self, req: MentionRequest) -> None:
                started.set()
                await asyncio.Event().wait()  # block until cancelled

        gateway._handler = _Blocks()  # type: ignore[assignment]
        gateway._spawn_handle(_req())
        await started.wait()
        assert len(gateway._inflight) == 1
        task = next(iter(gateway._inflight))

        await gateway.drain_inflight()
        assert task.cancelled()

    async def test_drain_is_a_noop_when_idle(self) -> None:
        gateway = _gateway()
        await gateway.drain_inflight()  # must not raise


class TestOnReadyRegistersStepsToggleView:
    async def test_on_ready_adds_persistent_steps_toggle_view(self, tmp_path, monkeypatch) -> None:
        # _on_ready writes the first heartbeat too (§12.1); contain it in a tmp home.
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()  # add_view is stubbed to a MagicMock
        with (
            patch.object(type(gateway._client), "user", new=_FakeBotUser(), create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
        ):
            await gateway._on_ready()

        gateway._client.add_view.assert_called_once()
        view = gateway._client.add_view.call_args.args[0]
        assert isinstance(view, StepsToggleView)
