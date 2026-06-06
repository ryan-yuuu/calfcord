"""Lifecycle tests for the bridge's ``_run`` boot path (calfkit 0.5.4).

The bridge embeds a foreground (the Discord gateway WebSocket owns the loop),
so unlike the four standalone runners it does not hand the whole lifecycle to
``Worker.run()``. Instead it drives a managed ``async with worker:`` that wraps
its own signal handling + the gateway/stop race. These tests pin the boot
contract the migration to 0.5.4 must preserve:

* the raw state consumer registers BEFORE the worker starts (its consumer group
  must join with the worker's own node subscribers in one ``broker.start()``);
* ``provision_infra`` (the #180 reply-topic + the bridge's blind-spot control
  topics) runs BEFORE the worker starts;
* the broker drains (``worker.stop()``) on exit of the ``async with worker:``
  block;
* TEARDOWN ORDER (the correction from review, doc §6): the Discord ingress stops
  first (``gateway.close()`` in the inner ``finally``), the broker drains while
  ``persona_sender`` + ``typing_notifier`` are still open, and only THEN does
  ``typing_notifier.aclose()`` run — so a steps hop draining at SIGTERM never
  fires into a cancelled ``TypingNotifier`` (``aclose`` cancels in-flight tasks).

The full real ``_run`` needs a live broker + a Discord connection; here we stub
the heavy collaborators and pin the wiring + ordering, mirroring the standalone
runners' ``_amain`` tests (``tests/router/test_runner.py``,
``tests/agents/test_runner.py``).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from calfcord._provisioning import bridge_infra_topics
from calfcord.bridge import gateway as gateway_mod
from calfcord.bridge.registry import AgentRegistry
from calfcord.discord.settings import DiscordSettings


def _settings() -> DiscordSettings:
    return DiscordSettings(
        bot_token=SecretStr("test-bot-token"),
        application_id=1234,
        guild_id=5678,
        owner_user_id=9999,
    )


class _RecordingWorker:
    """Stand-in :class:`calfkit.Worker` exercised through ``async with worker:``.

    ``__aenter__`` records "start" (the real ``start()`` registers handlers,
    provisions node topics, and joins consumer groups) and ``__aexit__`` records
    "drain" (the real ``stop()`` drains in-flight hops before disconnecting). On
    drain it invokes an optional ``on_drain`` callback so a test can simulate a
    steps hop still in flight at shutdown.
    """

    def __init__(self, order: list[str], *, on_drain: Any = None) -> None:
        self._order = order
        self._on_drain = on_drain
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _RecordingWorker:
        self._order.append("start")
        self.entered = True
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._order.append("drain")
        self.exited = True
        if self._on_drain is not None:
            await self._on_drain()


def _patch_boot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    order: list[str],
    client: object,
    worker: _RecordingWorker,
    typing_notifier: Any = None,
    gateway: Any = None,
) -> dict[str, Any]:
    """Stub every heavy ``_run`` collaborator; return the spy handles.

    The Discord gateway, persona sender, transcript store, and broker are all
    replaced so ``_run`` exercises pure wiring. ``gateway.start()`` resolves
    immediately so the gateway/stop race ends and the inner ``finally`` runs;
    the test then asserts the teardown sequence.
    """
    persona_sender = MagicMock()
    persona_sender.client = MagicMock()

    @asynccontextmanager
    async def _fake_persona(*_a: Any, **_k: Any) -> Any:
        yield persona_sender

    @asynccontextmanager
    async def _fake_connect(*_a: Any, **_k: Any) -> Any:
        yield client

    @asynccontextmanager
    async def _fake_store(*_a: Any, **_k: Any) -> Any:
        yield MagicMock()

    monkeypatch.setattr(gateway_mod, "DiscordPersonaSender", lambda *a, **k: _fake_persona())
    monkeypatch.setattr(gateway_mod.Client, "connect", lambda *a, **k: _fake_connect(*a, **k))
    monkeypatch.setattr(gateway_mod, "_open_transcript_store", lambda *a, **k: _fake_store())
    monkeypatch.setattr(gateway_mod, "BridgeIngress", MagicMock())
    monkeypatch.setattr(gateway_mod, "PendingWires", MagicMock())
    monkeypatch.setattr(gateway_mod, "_prune_on_startup", AsyncMock())

    notifier = typing_notifier if typing_notifier is not None else MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(gateway_mod, "TypingNotifier", lambda *a, **k: notifier)

    if gateway is None:
        gateway = MagicMock()
        gateway.start = AsyncMock()
        gateway.close = AsyncMock(side_effect=lambda: order.append("gateway.close"))
        gateway._slash = MagicMock()
    monkeypatch.setattr(gateway_mod, "DiscordIngressGateway", lambda *a, **k: gateway)

    monkeypatch.setattr(gateway_mod, "build_outbox_consumer", lambda *a, **k: MagicMock())
    monkeypatch.setattr(gateway_mod, "build_synthesized_consumer", lambda *a, **k: MagicMock())
    monkeypatch.setattr(gateway_mod, "build_steps_consumer", lambda *a, **k: MagicMock())
    monkeypatch.setattr(gateway_mod, "StepsState", MagicMock())

    monkeypatch.setattr(gateway_mod, "Worker", lambda *a, **k: worker)

    state_consumer = MagicMock(side_effect=lambda *a, **k: order.append("register_state_consumer"))
    monkeypatch.setattr(
        "calfcord.control_plane.state_consumer.register_state_consumer", state_consumer
    )

    provision = AsyncMock(side_effect=lambda *a, **k: order.append("provision"))
    monkeypatch.setattr(gateway_mod, "provision_infra", provision)

    return {
        "gateway": gateway,
        "notifier": notifier,
        "register_state_consumer": state_consumer,
        "provision_infra": provision,
        "persona_sender": persona_sender,
    }


class TestRunBootOrdering:
    """The bridge's managed boot must register the raw state consumer and
    provision the blind-spot topics BEFORE the worker starts, then drain the
    broker on exit. Getting any of these out of order silently drops agent
    replies on a no-auto-create broker or strands consumers at shutdown."""

    async def test_state_consumer_and_provision_run_before_worker_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        client = MagicMock()
        worker = _RecordingWorker(order)
        spies = _patch_boot(monkeypatch, order=order, client=client, worker=worker)

        await gateway_mod._run(_settings(), AgentRegistry([]), "localhost")

        # Raw subscriber registered, then blind-spot topics provisioned, then
        # (and only then) the worker starts — its broker.start joins every
        # subscriber together.
        assert order.index("register_state_consumer") < order.index("start")
        assert order.index("provision") < order.index("start")
        assert spies["register_state_consumer"].call_count == 1
        spies["provision_infra"].assert_awaited_once_with(
            client, extra_topics=bridge_infra_topics()
        )

    async def test_broker_drains_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``async with worker:`` must drain the broker (``worker.stop()``) on
        exit — the bridge no longer hand-rolls broker.start/stop."""
        order: list[str] = []
        worker = _RecordingWorker(order)
        _patch_boot(monkeypatch, order=order, client=MagicMock(), worker=worker)

        await gateway_mod._run(_settings(), AgentRegistry([]), "localhost")

        assert worker.entered and worker.exited
        assert "drain" in order


class TestRunTeardownOrder:
    """Teardown order (doc §6, risk 3): ingress stops first, broker drains while
    the typing notifier is still open, then ``typing_notifier.aclose()`` runs.
    A steps hop draining at SIGTERM must never call a cancelled TypingNotifier."""

    async def test_gateway_closes_before_drain_and_notifier_closes_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        notifier = MagicMock()
        notifier.aclose = AsyncMock(side_effect=lambda: order.append("notifier.aclose"))
        worker = _RecordingWorker(order)
        _patch_boot(
            monkeypatch, order=order, client=MagicMock(), worker=worker, typing_notifier=notifier
        )

        await gateway_mod._run(_settings(), AgentRegistry([]), "localhost")

        # Ingress (Discord) stops first, then the broker drains, then the
        # typing notifier is closed — strictly in that order.
        assert order.index("gateway.close") < order.index("drain")
        assert order.index("drain") < order.index("notifier.aclose")

    async def test_in_flight_steps_hop_at_drain_never_hits_closed_notifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real race the new order fixes: a steps hop still draining when the
        worker stops must be able to ``fire()`` the typing notifier. Today's code
        closed the notifier BEFORE the drain, so an in-flight hop fired into a
        cancelled notifier. Here the worker's drain simulates that hop firing the
        notifier; ``aclose`` must not have run yet."""
        order: list[str] = []
        notifier = MagicMock()
        notifier.fire = MagicMock(side_effect=lambda _cid: order.append("steps.fire"))
        notifier.aclose = AsyncMock(side_effect=lambda: order.append("notifier.aclose"))

        # Simulate an in-flight steps hop that fires typing during the drain.
        async def _drain_fires_typing() -> None:
            notifier.fire(123)

        worker = _RecordingWorker(order, on_drain=_drain_fires_typing)
        _patch_boot(
            monkeypatch, order=order, client=MagicMock(), worker=worker, typing_notifier=notifier
        )

        await gateway_mod._run(_settings(), AgentRegistry([]), "localhost")

        # The hop fired during the drain, BEFORE aclose — the notifier was live.
        assert order.index("steps.fire") < order.index("notifier.aclose")
        # And aclose ran exactly once, after the drain completed.
        notifier.aclose.assert_awaited_once()


class TestRunSignalRace:
    """The bridge keeps its OWN signal handling (start()/stop() install none by
    design). A SIGINT/SIGTERM must end the gateway/stop race and unwind the
    managed worker cleanly — the gateway foreground is what the bridge owns."""

    async def test_stop_signal_unwinds_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        order: list[str] = []

        # A gateway whose start() blocks forever, so only a stop signal can end
        # the race. The signal handler the bridge installs sets the stop event.
        stop_started = asyncio.Event()
        gateway = MagicMock()
        gateway._slash = MagicMock()
        gateway.close = AsyncMock(side_effect=lambda: order.append("gateway.close"))

        async def _never_returns() -> None:
            stop_started.set()
            await asyncio.Event().wait()

        gateway.start = _never_returns

        worker = _RecordingWorker(order)
        _patch_boot(
            monkeypatch, order=order, client=MagicMock(), worker=worker, gateway=gateway
        )

        run_task = asyncio.create_task(
            gateway_mod._run(_settings(), AgentRegistry([]), "localhost")
        )
        await stop_started.wait()
        # Drive the stop event the bridge's own signal handler would set on
        # SIGTERM. We can't deliver a real signal under pytest portably, so we
        # cancel the run task — the inner finally must still close the gateway
        # and drain the worker.
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

        # Even on a cancellation the inner finally + async-with-worker teardown
        # must have stopped the ingress and drained the broker.
        assert "gateway.close" in order
        assert "drain" in order


class TestMainDelegatesToRun:
    """``main`` stays the thin CLI seam: it builds settings + registry and
    delegates to the module-level ``_run`` via ``asyncio.run``. Extracting
    ``_run`` to module scope is what makes the boot contract testable."""

    def test_main_invokes_run_with_settings_registry_and_urls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALF_HOST_URL", "broker-host:9092")
        monkeypatch.setattr(
            gateway_mod, "DiscordSettings", lambda *a, **k: _settings()
        )
        captured: dict[str, Any] = {}

        async def _fake_run(settings: Any, registry: Any, server_urls: str) -> None:
            captured["settings"] = settings
            captured["registry"] = registry
            captured["server_urls"] = server_urls

        monkeypatch.setattr(gateway_mod, "_run", _fake_run)

        gateway_mod.main()

        assert isinstance(captured["registry"], AgentRegistry)
        assert captured["server_urls"] == "broker-host:9092"
        assert captured["settings"].guild_id == 5678
