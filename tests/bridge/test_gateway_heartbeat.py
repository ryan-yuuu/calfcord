"""Unit tests for the bridge's Discord-connection-aware heartbeat (design §12.1).

The §12.1 health contract is strict: the bridge's heartbeat must mean "connected
to Discord", not merely "process alive". Three properties are pinned here, all
offline (no real Discord connection):

* **First beat on ``on_ready`` BEFORE slash-sync.** A slow/429 ``slash.sync`` must
  never delay or fail the readiness signal, so ``_on_ready`` writes the first
  ``bridge`` beat — to the same ``<home>/state/health/`` the ``calfcord
  _healthcheck bridge`` probe reads — *before* it awaits ``self._slash.sync``.
* **The beat's identity is a display string, never a token** (§12.3): the bot's
  ``str(bot_user) (id)``, with the token nowhere in it.
* **Connection state drives ``connected``.** ``_on_ready`` / ``_on_resumed`` set
  it True; ``_on_disconnect`` sets it False — the predicate the timer-refresher
  gates each write on, so a dropped gateway ages the beat out within the TTL
  instead of lying green.

These mirror ``test_gateway_replies.py``'s harness (a real ``DiscordIngressGateway``
with mocked ingress/registry/client and a stubbed ``slash.sync``); the
``_GatewayClient`` constructor is sync + offline, so no network is touched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import SecretStr

from calfcord.bridge.gateway import DiscordIngressGateway
from calfcord.discord.settings import DiscordSettings
from calfcord.health.heartbeat import is_fresh, read_beat


def _settings() -> DiscordSettings:
    return DiscordSettings(
        bot_token=SecretStr("super-secret-token-value"),
        application_id=1234,
        guild_id=5678,
        owner_user_id=9999,
    )


def _gateway() -> DiscordIngressGateway:
    """A real gateway with mocked collaborators (mirrors test_gateway_replies)."""
    return DiscordIngressGateway(
        settings=_settings(),
        ingress=MagicMock(),
        registry=MagicMock(),
        calfkit_client=MagicMock(),
        transcript_store=MagicMock(),
    )


class _FakeBotUser:
    """A stand-in for ``discord.Client.user``: ``str()`` → name, ``.id`` → id.

    A real class (not a ``SimpleNamespace`` with an instance ``__str__``) because
    Python resolves ``str(obj)`` via ``type(obj).__str__`` — an instance-level
    dunder is ignored — and the bridge formats the beat identity as
    ``f"{bot_user} ({bot_user.id})"``, so ``__str__`` must live on the type.
    """

    def __init__(self, *, name: str = "Calfbot#1234", user_id: int = 42) -> None:
        self.id = user_id
        self._name = name

    def __str__(self) -> str:
        return self._name


def _fake_bot_user(*, name: str = "Calfbot#1234", user_id: int = 42) -> _FakeBotUser:
    """A stand-in for ``discord.Client.user``: ``str()`` → name, ``.id`` → id."""
    return _FakeBotUser(name=name, user_id=user_id)


class TestOnReadyWritesFirstBeat:
    """``_on_ready`` writes the first ``bridge`` heartbeat to the resolved home."""

    async def test_writes_fresh_bridge_beat_under_resolved_home(
        self, tmp_path, monkeypatch
    ) -> None:
        # The beat must land where ``calfcord _healthcheck bridge`` reads it:
        # ``$CALFCORD_HOME/state/health/bridge.json``.
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user()
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()

        beat = read_beat(tmp_path, "bridge")
        assert beat is not None
        assert beat.component == "bridge"
        assert beat.status == "healthy"
        # Stamped with the real clock; assert it reads fresh against the same clock.
        from datetime import UTC, datetime

        assert is_fresh(beat, now=datetime.now(UTC))

    async def test_first_beat_is_written_before_slash_sync_is_awaited(
        self, tmp_path, monkeypatch
    ) -> None:
        # §12.1/§13.3: a slow/429 slash-sync must not gate readiness, so the beat
        # must already exist on disk by the time ``slash.sync`` is awaited.
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user()

        beat_when_synced: list[object] = []

        async def _record_then_return(_guild_id: object) -> None:
            # Capture whether the beat exists at the moment sync runs.
            beat_when_synced.append(read_beat(tmp_path, "bridge"))

        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(side_effect=_record_then_return)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()

        assert len(beat_when_synced) == 1
        assert beat_when_synced[0] is not None, "beat must exist BEFORE slash.sync is awaited"

    async def test_on_ready_survives_a_heartbeat_write_failure(
        self, tmp_path, monkeypatch
    ) -> None:
        # A heartbeat write failure (read-only volume / disk full / EACCES) must
        # NOT crash _on_ready: the bridge still slash-syncs, publishes the
        # discovery ping, and marks itself connected. A failed beat just ages to
        # "not ready" at the probe (correct) — it must never break bridge boot.
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user()
        sync = AsyncMock(return_value=None)
        ping = AsyncMock(return_value=None)
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=sync),
            patch("calfcord.bridge.gateway.publish_discovery_ping", new=ping),
            patch(
                "calfcord.bridge.gateway.write_beat",
                side_effect=OSError("read-only file system"),
            ),
        ):
            await gateway._on_ready()

        sync.assert_awaited_once()
        ping.assert_awaited_once()
        assert gateway.connected is True

    async def test_identity_is_a_display_string_never_the_token(
        self, tmp_path, monkeypatch
    ) -> None:
        # The identity must be a human-readable display string with the numeric id;
        # the bot token must appear NOWHERE in the persisted beat (§12.3).
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user(name="Calfbot#1234", user_id=42)
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()

        beat = read_beat(tmp_path, "bridge")
        assert beat is not None
        assert beat.identity == "Calfbot#1234 (42)"
        assert "super-secret-token-value" not in (beat.identity or "")
        assert "super-secret-token-value" not in beat.model_dump_json()


class TestConnectionStateFlag:
    """``connected`` tracks the live Discord gateway connection (§12.1)."""

    async def test_starts_disconnected_before_on_ready(self) -> None:
        # Before the first handshake the bridge is not yet connected, so a beat
        # written on the timer would be (correctly) skipped.
        gateway = _gateway()
        assert gateway.connected is False

    async def test_on_ready_sets_connected_true(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user()
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()
        assert gateway.connected is True

    async def test_on_disconnect_flips_connected_false(self, tmp_path, monkeypatch) -> None:
        # A dropped gateway must flip the flag so the refresher stops feeding the
        # beat and it ages out within the TTL (the §12.1 contract).
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user()
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()
        assert gateway.connected is True

        await gateway._on_disconnect()
        assert gateway.connected is False

    async def test_on_resumed_flips_connected_true_after_disconnect(self) -> None:
        # A resumed session restores liveness without a full on_ready re-identify.
        gateway = _gateway()
        await gateway._on_disconnect()
        assert gateway.connected is False
        await gateway._on_resumed()
        assert gateway.connected is True


class TestBotIdentityGetter:
    """``bot_identity`` exposes the display string the refresher stamps each beat
    with (resolved once the gateway is ready)."""

    async def test_identity_none_before_ready(self) -> None:
        gateway = _gateway()
        assert gateway.bot_identity is None

    async def test_identity_is_display_string_after_ready(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user(name="Calfbot#1234", user_id=42)
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()
        assert gateway.bot_identity == "Calfbot#1234 (42)"


class TestGatewayClientWiresLifecycleEvents:
    """The ``_GatewayClient`` (discord.Client subclass) must delegate the
    connection-lifecycle events discord.py fires (``on_disconnect`` /
    ``on_resumed``) to the gateway, alongside the existing ``on_ready``."""

    async def test_on_disconnect_delegates_to_gateway(self) -> None:
        gateway = _gateway()
        gateway._on_disconnect = AsyncMock()  # type: ignore[method-assign]
        await gateway._client.on_disconnect()
        gateway._on_disconnect.assert_awaited_once()

    async def test_on_resumed_delegates_to_gateway(self) -> None:
        gateway = _gateway()
        gateway._on_resumed = AsyncMock()  # type: ignore[method-assign]
        await gateway._client.on_resumed()
        gateway._on_resumed.assert_awaited_once()


class TestRefresherPredicateReflectsFlag:
    """The connection flag is exactly the predicate the timer-refresher gates on:
    ``is_healthy=lambda: gateway.connected``. Drive the real ``refresh_once`` with
    that predicate and assert it writes IFF the gateway reports connected."""

    def test_refresh_once_skips_while_disconnected(self, tmp_path) -> None:
        from datetime import UTC, datetime

        from calfcord.health.refresher import refresh_once

        gateway = _gateway()  # connected is False before on_ready
        wrote = refresh_once(
            tmp_path,
            "bridge",
            is_healthy=lambda: gateway.connected,
            identity=lambda: gateway.bot_identity,
            now=datetime.now(UTC),
        )
        assert wrote is False
        assert read_beat(tmp_path, "bridge") is None

    async def test_refresh_once_writes_with_identity_while_connected(
        self, tmp_path, monkeypatch
    ) -> None:
        from datetime import UTC, datetime

        from calfcord.health.refresher import refresh_once

        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        gateway = _gateway()
        fake_user = _fake_bot_user(name="Calfbot#1234", user_id=42)
        with (
            patch.object(type(gateway._client), "user", new=fake_user, create=True),
            patch.object(gateway._slash, "sync", new=AsyncMock(return_value=None)),
            patch(
                "calfcord.bridge.gateway.publish_discovery_ping",
                new=AsyncMock(return_value=None),
            ),
        ):
            await gateway._on_ready()

        wrote = refresh_once(
            tmp_path,
            "bridge",
            is_healthy=lambda: gateway.connected,
            identity=lambda: gateway.bot_identity,
            now=datetime.now(UTC),
        )
        assert wrote is True
        beat = read_beat(tmp_path, "bridge")
        assert beat is not None
        assert beat.identity == "Calfbot#1234 (42)"


class TestRefresherTaskWiringLifecycle:
    """Exercise the exact ``run_refresher`` wiring ``main`` installs (predicate =
    ``gateway.connected``, identity = ``gateway.bot_identity``) driven by an
    injected sleep, so the §12.1 timer behaviour is verified without standing up
    the broker: it writes while connected, stops feeding the beat on disconnect,
    and returns cleanly on cancel."""

    async def test_run_refresher_writes_then_freezes_on_disconnect(
        self, tmp_path
    ) -> None:
        import asyncio
        from datetime import UTC, datetime, timedelta

        from calfcord.health.refresher import run_refresher

        gateway = _gateway()
        gateway._connected = True
        gateway._bot_identity = "Calfbot#1234 (42)"

        # Drop the gateway after the first tick; the second tick must skip the
        # write so last_beat freezes at the first (healthy) tick — the contract
        # that lets the beat age out within the TTL after a disconnect.
        base = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
        times = iter([base, base + timedelta(seconds=2)])
        sleep_count = 0

        async def fake_sleep(_seconds: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count == 1:
                gateway._connected = False  # gateway dropped between ticks
            if sleep_count >= 2:
                raise asyncio.CancelledError

        # Must return cleanly on cancel (run_refresher swallows CancelledError).
        await run_refresher(
            tmp_path,
            "bridge",
            is_healthy=lambda: gateway.connected,
            identity=lambda: gateway.bot_identity,
            clock=lambda: next(times),
            sleep=fake_sleep,
        )

        beat = read_beat(tmp_path, "bridge")
        assert beat is not None
        assert beat.identity == "Calfbot#1234 (42)"
        assert beat.last_beat == base  # frozen at the healthy tick, not advanced
