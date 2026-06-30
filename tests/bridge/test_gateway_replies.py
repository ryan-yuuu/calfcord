"""Unit tests for the gateway's user-facing error replies and lifecycle.

The gateway runs Discord I/O so a true end-to-end test would need a
mock discord.py connection. These tests focus on the reply-helper
contract: given a triggering condition, the right text is sent via
``message.reply`` and Discord HTTPException is logged + swallowed
(matching the existing ``_reply_unknown_mention`` shape).

Also covers the ``_on_ready`` lifecycle: it must inject a
:class:`ChannelHistoryFetcher` into the ingress so the slash + ambient
paths can fetch history. A regression that drops the injection would
leave the bridge running in the pre-ready degradation mode forever
(empty history fleet-wide) — silent quality loss with no operator
signal beyond DEBUG logs. The injection test is a regression alarm.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from pydantic import SecretStr

from calfcord.bridge.gateway import DiscordIngressGateway
from calfcord.bridge.history import CLEAR_MARKER_TEXT, ChannelHistoryFetcher
from calfcord.discord.settings import DiscordSettings


def _settings() -> DiscordSettings:
    return DiscordSettings(
        bot_token=SecretStr("test-bot-token"),
        application_id=1234,
        guild_id=5678,
        owner_user_id=9999,
    )


def _gateway() -> DiscordIngressGateway:
    """Construct a gateway with mocked ingress + registry.

    Note: ``DiscordIngressGateway.__init__`` instantiates a
    ``_GatewayClient`` (discord.Client subclass). The discord.Client
    constructor is sync and offline, so this is safe without
    network. We do not call ``.start()``.

    The calfkit client is mocked — its only use in non-``_on_ready``
    paths is being passed to the :class:`SlashCommandManager` (which
    stores it but doesn't invoke it during constructor wiring).

    The gateway does not fire typing indicators — that lives entirely in
    the steps consumer — so there is no typing notifier to inject here.
    """
    return DiscordIngressGateway(
        settings=_settings(),
        ingress=MagicMock(),
        registry=MagicMock(),
        calfkit_client=MagicMock(),
        transcript_store=MagicMock(),
    )


def _fake_message() -> MagicMock:
    """A stand-in for ``discord.Message`` with the surface the
    reply helpers touch."""
    msg = MagicMock(spec=discord.Message)
    msg.id = 12345
    msg.reply = AsyncMock()
    return msg


class TestOnMessageIngressFailureWiring:
    """The broad ``except Exception`` in ``_on_message`` MUST drive
    a generic user-facing reply so the silence after an unexpected
    ingress failure (broker hiccup, registry mid-write, etc.) isn't
    unexplained. Without this wiring the user has no signal at all."""

    async def test_on_message_routes_unexpected_failure_to_reply_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gateway = _gateway()

        fake_wire = MagicMock()
        fake_wire.event_id = "evt-broker-fail"
        fake_wire.channel_id = 6789

        gateway._message_normalizer = MagicMock()
        gateway._message_normalizer.normalize = MagicMock(return_value=fake_wire)
        gateway._bot_user_id = 0

        # Simulate a broker-level failure that isn't
        # AmbientRosterEmptyError.
        gateway._ingress.handle = AsyncMock(side_effect=RuntimeError("kafka broker unreachable"))

        reply_spy = AsyncMock()
        monkeypatch.setattr(gateway, "_reply_ingress_failure", reply_spy)

        message = MagicMock(spec=discord.Message)
        message.id = 44
        message.content = "hello there"
        message.guild = MagicMock()
        message.guild.id = 5678
        message.author = MagicMock()
        message.author.id = 99999
        message.webhook_id = None

        await gateway._on_message(message)

        reply_spy.assert_awaited_once_with(message)

    async def test_reply_ingress_failure_text_is_generic_not_internal(
        self,
    ) -> None:
        """The user-facing text must not leak internal detail
        (stack-trace fragments, internal class names) — the
        operator-actionable signal is the ingress's ERROR log."""
        gateway = _gateway()
        message = _fake_message()
        await gateway._reply_ingress_failure(message)
        message.reply.assert_awaited_once()
        text = message.reply.await_args.args[0]
        # The reply should be generic enough that an internal
        # rename doesn't leak.
        assert "Something went wrong" in text
        # Sanity: no Python type names or implementation references.
        assert "RuntimeError" not in text
        assert "ingress" not in text.lower() or "operator" in text.lower()


class TestOnMessageFiltersClearMarker:
    """The ``/clear`` marker is the bot's own non-webhook message, so the
    gateway self-message filter in ``_on_message`` must drop it. Without
    this, the bridge would re-ingest its own marker and fan it out to
    agents on every ``/clear`` — a token-burning loop. The marker depends
    on this seam; these tests pin that dependency."""

    def _ready_gateway(self) -> DiscordIngressGateway:
        gateway = _gateway()
        gateway._bot_user_id = 555
        gateway._message_normalizer = MagicMock()
        gateway._message_normalizer.normalize = MagicMock(return_value=MagicMock())
        gateway._ingress.handle = AsyncMock()
        return gateway

    @staticmethod
    def _message(*, author_id: int, webhook_id: int | None) -> MagicMock:
        message = MagicMock(spec=discord.Message)
        message.id = 1
        message.guild = MagicMock()
        message.guild.id = 5678  # matches settings.guild_id
        message.author = MagicMock()
        message.author.id = author_id
        message.webhook_id = webhook_id
        message.content = CLEAR_MARKER_TEXT
        return message

    async def test_own_non_webhook_marker_is_not_ingested(self) -> None:
        gateway = self._ready_gateway()
        message = self._message(author_id=555, webhook_id=None)  # the bot itself

        await gateway._on_message(message)

        gateway._message_normalizer.normalize.assert_not_called()
        gateway._ingress.handle.assert_not_awaited()

    async def test_webhook_message_with_bot_id_passes_through(self) -> None:
        """A webhook post (e.g. an agent persona) is NOT self-filtered —
        which is exactly why ``/clear`` posts the marker as a plain
        (non-webhook) message so this seam can drop it."""
        gateway = self._ready_gateway()
        message = self._message(author_id=555, webhook_id=777)

        await gateway._on_message(message)

        gateway._message_normalizer.normalize.assert_called_once()


class TestOnReadyInjectsFetcher:
    """The ``_on_ready`` hook must construct a
    :class:`ChannelHistoryFetcher` and inject it into the ingress via
    :meth:`BridgeIngress.set_fetcher`. Without this, the bridge would
    run in pre-ready degradation mode forever — every slash and
    ambient invocation would skip history fetching and produce
    silently-lower-quality replies.

    The test stubs out the slash command sync (which requires a live
    Discord connection) and just asserts the injection happens.
    """

    async def test_on_ready_injects_channel_history_fetcher(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        # _on_ready now also writes the first bridge heartbeat (§12.1); point
        # CALFCORD_HOME at a tmp dir so that beat lands there instead of
        # littering the repo's working directory.
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        # Patch the client.user attribute (populated by Discord after
        # handshake) so _on_ready's assertion holds.
        fake_user = SimpleNamespace(id=42, __str__=lambda self: "bot#1234")
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()

        # The ingress must have received the fetcher.
        gateway._ingress.set_fetcher.assert_called_once()
        injected = gateway._ingress.set_fetcher.call_args.args[0]
        assert isinstance(injected, ChannelHistoryFetcher)

    async def test_on_ready_logs_history_injection(
        self, caplog: pytest.LogCaptureFixture, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _on_ready now writes the first bridge heartbeat (§12.1); contain it in
        # a tmp CALFCORD_HOME so the test stays hermetic.
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = SimpleNamespace(id=42, __str__=lambda self: "bot#1234")
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
            caplog.at_level(logging.INFO, logger="calfcord.bridge.gateway"),
        ):
            await gateway._on_ready()
        assert any("history fetcher injected" in r.message for r in caplog.records)
