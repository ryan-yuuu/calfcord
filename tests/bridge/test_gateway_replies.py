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

from calfkit_organization.bridge.gateway import DiscordIngressGateway
from calfkit_organization.bridge.history import ChannelHistoryFetcher
from calfkit_organization.discord.settings import DiscordSettings


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
    """
    return DiscordIngressGateway(
        settings=_settings(),
        ingress=MagicMock(),
        registry=MagicMock(),
    )


def _fake_message() -> MagicMock:
    """A stand-in for ``discord.Message`` with the surface
    ``_reply_empty_roster`` touches."""
    msg = MagicMock(spec=discord.Message)
    msg.id = 12345
    msg.reply = AsyncMock()
    return msg


class TestReplyEmptyRoster:
    """The gateway's empty-roster reply makes the deployment
    misconfiguration visible to the user via an inline reply, instead
    of silently dropping the ambient message."""

    async def test_reply_called_with_operator_actionable_text(self) -> None:
        gateway = _gateway()
        message = _fake_message()
        await gateway._reply_empty_roster(message)
        message.reply.assert_awaited_once()
        text = message.reply.await_args.args[0]
        # The message must convey three things: the cause (no agents),
        # the action (contact operator), and that this specific
        # message wasn't routed. Pin the substantive phrases rather
        # than the full string so wording can evolve.
        assert "No assistant agents" in text
        assert "contact an operator" in text.lower()

    async def test_logs_info_on_rejection(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO log with the message_id makes the rejection
        correlatable to a user-reported "no reply"."""
        gateway = _gateway()
        message = _fake_message()
        with caplog.at_level(
            logging.INFO, logger="calfkit_organization.bridge.gateway"
        ):
            await gateway._reply_empty_roster(message)
        assert any(
            "rejected ambient publish" in r.message
            and "empty roster" in r.message
            and "12345" in r.message
            for r in caplog.records
        )

    async def test_swallows_http_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If Discord rejects the reply (rate-limit, deleted channel,
        etc.), the gateway logs and continues — matching the
        ``_reply_unknown_mention`` pattern. The caller is already in
        an error-recovery path; bubbling further has nowhere useful
        to go."""
        gateway = _gateway()
        message = _fake_message()
        # Construct an HTTPException without standing up a real
        # aiohttp.ClientResponse — pass MagicMock for response and a
        # short error string. discord.HTTPException's __init__ pulls
        # status/code/text from the response object via getattr.
        fake_response = MagicMock()
        fake_response.status = 429
        fake_response.reason = "Too Many Requests"
        message.reply = AsyncMock(
            side_effect=discord.HTTPException(fake_response, "rate limited")
        )

        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.bridge.gateway"
        ):
            # Must not raise — gateway swallows.
            await gateway._reply_empty_roster(message)
        assert any(
            "failed to send empty-roster reply" in r.message
            for r in caplog.records
        )


class TestOnMessageEmptyRosterWiring:
    """The gateway's ``_on_message`` MUST catch
    :class:`AmbientRosterEmptyError` from ``ingress.handle`` and
    route it to ``_reply_empty_roster``. Without this wiring, the
    user-facing reply would never fire and the documented feature
    ("ambient with no assistants → inline reply") silently regresses
    — every other test would still pass."""

    async def test_on_message_routes_roster_empty_to_reply_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: ingress raises ``AmbientRosterEmptyError``;
        ``_on_message`` must drive ``_reply_empty_roster`` with the
        triggering message."""
        from calfkit_organization.bridge.ingress import (
            AmbientRosterEmptyError,
        )

        gateway = _gateway()

        # ``_on_message`` flows through normalize → ingress.handle.
        # We bypass normalize by stubbing ``_message_normalizer``
        # with a normalizer that returns a fake wire, then make
        # ingress.handle raise our specific exception.
        fake_wire = MagicMock()
        fake_wire.event_id = "evt-test"
        fake_wire.channel_id = 6789

        gateway._message_normalizer = MagicMock()
        gateway._message_normalizer.normalize = MagicMock(return_value=fake_wire)
        gateway._bot_user_id = 0  # unique enough to bypass the self-message filter

        gateway._ingress.handle = AsyncMock(
            side_effect=AmbientRosterEmptyError(
                event_id="evt-test", channel_id=6789
            )
        )

        # Spy on _reply_empty_roster so we don't actually touch
        # Discord — but call the real _on_message control flow.
        reply_spy = AsyncMock()
        monkeypatch.setattr(gateway, "_reply_empty_roster", reply_spy)

        # Build a minimal Discord message stand-in that survives
        # _on_message's filters.
        message = MagicMock(spec=discord.Message)
        message.id = 42
        message.guild = MagicMock()
        message.guild.id = 5678  # matches settings.guild_id
        message.author = MagicMock()
        message.author.id = 99999  # not the bot user
        message.webhook_id = None

        await gateway._on_message(message)

        reply_spy.assert_awaited_once_with(message)

    async def test_on_message_does_not_call_reply_helper_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression positive: a successful ingress.handle must
        NOT trigger the empty-roster reply path."""
        gateway = _gateway()

        fake_wire = MagicMock()
        fake_wire.event_id = "evt-ok"
        fake_wire.channel_id = 6789

        gateway._message_normalizer = MagicMock()
        gateway._message_normalizer.normalize = MagicMock(return_value=fake_wire)
        gateway._bot_user_id = 0

        gateway._ingress.handle = AsyncMock(return_value=None)
        reply_spy = AsyncMock()
        monkeypatch.setattr(gateway, "_reply_empty_roster", reply_spy)

        message = MagicMock(spec=discord.Message)
        message.id = 43
        message.guild = MagicMock()
        message.guild.id = 5678
        message.author = MagicMock()
        message.author.id = 99999
        message.webhook_id = None

        await gateway._on_message(message)

        reply_spy.assert_not_called()


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
        gateway._ingress.handle = AsyncMock(
            side_effect=RuntimeError("kafka broker unreachable")
        )

        reply_spy = AsyncMock()
        monkeypatch.setattr(gateway, "_reply_ingress_failure", reply_spy)

        message = MagicMock(spec=discord.Message)
        message.id = 44
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

    async def test_on_ready_injects_channel_history_fetcher(self) -> None:
        gateway = _gateway()
        # Patch the client.user attribute (populated by Discord after
        # handshake) so _on_ready's assertion holds.
        fake_user = SimpleNamespace(id=42, __str__=lambda self: "bot#1234")
        with patch.object(
            type(gateway._client), "user", new=fake_user, create=True
        ), patch.object(
            gateway._slash, "sync", new=AsyncMock(return_value=None)
        ):
            await gateway._on_ready()

        # The ingress must have received the fetcher.
        gateway._ingress.set_fetcher.assert_called_once()
        injected = gateway._ingress.set_fetcher.call_args.args[0]
        assert isinstance(injected, ChannelHistoryFetcher)

    async def test_on_ready_logs_history_injection(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gateway = _gateway()
        fake_user = SimpleNamespace(id=42, __str__=lambda self: "bot#1234")
        with patch.object(
            type(gateway._client), "user", new=fake_user, create=True
        ), patch.object(
            gateway._slash, "sync", new=AsyncMock(return_value=None)
        ), caplog.at_level(
            logging.INFO, logger="calfkit_organization.bridge.gateway"
        ):
            await gateway._on_ready()
        assert any(
            "history fetcher injected" in r.message for r in caplog.records
        )
