"""Unit tests for the bridge's managed (embedded) Worker lifecycle wiring.

After the Tier-3 migration the bridge folds onto calfkit 0.6.0's managed
``Worker`` lifecycle as the single deliberate *embedded* variant: it keeps
ownership of the Discord foreground + OS signals, so it drives the worker via
``worker.start()`` / ``worker.stop()`` (signals opted OUT) rather than
``Worker.run()`` (which would own the foreground + install a colliding signal
set). Two cohesive concerns are pinned here, both offline (no broker, no
Discord):

* **Blind-spot topic declaration** (``_register_blind_spot_topics``): a single
  ``on_startup`` hook (resource phase, BEFORE ``broker.start()``) declares the
  bridge's blind-spot topics — ``agent.state`` (consumed by the raw state
  consumer) and ``bridge.discovery`` (published at boot by the on_ready
  discovery ping) — into the client's startup ensurer, so calfkit's single
  pre-start provisioning pass creates them before any raw subscriber consumes.
  This mirrors the agents runner's blind-spot hook, on the embedded surface.

* **register-before-serve ordering** (``_run``): every consumer group — the
  two Worker nodes (outbox / steps) AND the raw state consumer —
  must JOIN before the Discord gateway accepts events and before the discovery
  ping is published, or replies arriving in the gap are LOST
  (``auto_offset_reset="latest"``). The embedded ``worker.start()`` joins the
  node groups; the raw state consumer is registered before ``worker.start()``;
  the gateway foreground task is created only after the worker is serving; and
  ``worker.stop()`` runs in the ordered shutdown ``finally`` so in-flight
  replies drain before the broker disconnects.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.worker import Worker
from calfkit.worker.lifecycle import LifecycleContext

from calfcord._provisioning import bridge_infra_topics
from calfcord.bridge.gateway import _register_blind_spot_topics


class _FakeEnsurer:
    """Records every declare() call so the blind-spot hook can be asserted."""

    def __init__(self) -> None:
        self.declared: list[str] = []

    def declare(self, topics: Any, *, framework: bool = False) -> None:
        self.declared.extend(topics)


class _FakeClient:
    def __init__(self) -> None:
        self._startup_ensurer = _FakeEnsurer()


class TestRegisterBlindSpotTopics:
    """``_register_blind_spot_topics`` wires exactly one ``on_startup`` hook that
    declares the bridge's blind-spot topics into the client's startup ensurer —
    the DOMAIN concern (which topics) kept apart from the worker LIFECYCLE that
    schedules it (pre-broker-start)."""

    @staticmethod
    def _wire() -> tuple[Worker, _FakeClient]:
        client = _FakeClient()
        worker = Worker(client)  # type: ignore[arg-type]
        _register_blind_spot_topics(worker, client)  # type: ignore[arg-type]
        return worker, client

    def test_registers_exactly_one_on_startup_hook(self) -> None:
        """One on_startup (declare) hook and nothing else: the bridge owns its
        own serving/shutdown sequencing in the foreground loop, so it registers
        NO after_startup / on_shutdown / after_shutdown worker hooks."""
        worker, _ = self._wire()
        assert len(worker._hooks_for("on_startup")) == 1
        assert worker._hooks_for("after_startup") == []
        assert worker._hooks_for("on_shutdown") == []
        assert worker._hooks_for("after_shutdown") == []

    async def test_on_startup_declares_bridge_blind_spot_topics(self) -> None:
        """The pre-broker-start hook declares the bridge's blind-spot topics
        (agent.state, bridge.discovery) into the client's startup ensurer, so
        calfkit's single provisioning pass creates them before the raw state
        consumer joins or the discovery ping publishes."""
        worker, client = self._wire()

        hook = worker._hooks_for("on_startup")[0]
        await hook(LifecycleContext(worker, worker.resources))

        assert client._startup_ensurer.declared == bridge_infra_topics()
        assert "agent.state" in client._startup_ensurer.declared
        assert "bridge.discovery" in client._startup_ensurer.declared


# ---------------------------------------------------------------------------
# register-before-serve ordering of the embedded boot in ``_run``.
#
# We drive ``main()``'s inner ``_run`` with every external collaborator faked
# (no broker, no Discord, no transcript DB) and assert the load-bearing order:
# state-consumer registered -> worker.start() (node groups join) -> gateway
# foreground task created -> ... -> worker.stop() in the ordered shutdown.
# ---------------------------------------------------------------------------


class _OrderLog:
    """Append-only event log shared by the faked collaborators in ``_run``."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, name: str) -> None:
        self.events.append(name)


class _FakeWorker:
    """A stand-in for ``calfkit.Worker`` recording start/stop into an order log.

    ``on_startup`` is a no-op decorator so ``_register_blind_spot_topics`` (which
    the boot calls) wires cleanly without a real lifecycle engine; the hook's
    *content* is asserted separately in ``TestRegisterBlindSpotTopics``.
    """

    def __init__(self, order: _OrderLog) -> None:
        self._order = order

    def on_startup(self, fn):
        return fn

    async def start(self) -> None:
        self._order.record("worker.start")

    async def stop(self) -> None:
        self._order.record("worker.stop")


class _FakeGateway:
    """A stand-in for ``DiscordIngressGateway`` recording start/close.

    ``start()`` blocks (the Discord foreground) until cancelled, so the
    boot's ``asyncio.wait`` is resolved by the stop event, not the gateway.
    """

    def __init__(self, order: _OrderLog) -> None:
        self._order = order
        self._slash = MagicMock()
        self.closed = False

    async def start(self) -> None:
        self._order.record("gateway.start")
        # Block like the real Discord websocket until the task is cancelled.
        import asyncio

        await asyncio.Event().wait()

    async def close(self) -> None:
        self._order.record("gateway.close")
        self.closed = True


@pytest.fixture
def _patched_run(monkeypatch):
    """Patch every external collaborator ``_run`` touches and return the log.

    Returns a tuple ``(order_log, handles)`` where ``handles`` exposes the
    faked worker / gateway / register_state_consumer mock / typing notifier so
    individual tests can assert finer-grained ordering.
    """
    import asyncio

    order = _OrderLog()

    # Persona sender + calfkit client + transcript store: trivial async CMs.
    @asynccontextmanager
    async def _fake_persona(_settings):
        client = MagicMock()
        client.client = MagicMock()
        yield client

    connect_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def _fake_connect(*_a, **_k):
        connect_calls.append(_k)
        yield MagicMock()

    @asynccontextmanager
    async def _fake_transcript(_settings):
        yield MagicMock()

    fake_worker = _FakeWorker(order)
    fake_gateway = _FakeGateway(order)

    def _make_gateway(*_a, **_k):
        return fake_gateway

    def _make_worker(*_a, **_k):
        return fake_worker

    register_state_consumer = MagicMock(side_effect=lambda *a, **k: order.record("register_state_consumer"))

    typing_notifier = MagicMock()
    typing_notifier.aclose = AsyncMock(side_effect=lambda: order.record("typing.aclose"))

    async def _fake_refresher(*_a, **_k):
        # Mirror the real ``run_refresher``: run until cancelled, then return
        # cleanly (swallow CancelledError) so the awaiting ``finally`` proceeds
        # to typing.aclose() + gateway.close() instead of re-raising.
        order.record("refresher.start")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    monkeypatch.setattr("calfcord.bridge.gateway.DiscordPersonaSender", _fake_persona)
    monkeypatch.setattr("calfcord.bridge.gateway.Client", MagicMock(connect=_fake_connect))
    monkeypatch.setattr("calfcord.bridge.gateway._open_transcript_store", _fake_transcript)
    monkeypatch.setattr("calfcord.bridge.gateway._prune_on_startup", AsyncMock())
    monkeypatch.setattr("calfcord.bridge.gateway.BridgeIngress", MagicMock())
    monkeypatch.setattr("calfcord.bridge.gateway.PendingWires", MagicMock())
    monkeypatch.setattr("calfcord.bridge.gateway.TypingNotifier", lambda *_a, **_k: typing_notifier)
    monkeypatch.setattr("calfcord.bridge.gateway.DiscordIngressGateway", _make_gateway)
    monkeypatch.setattr("calfcord.bridge.gateway.Worker", _make_worker)
    monkeypatch.setattr("calfcord.bridge.gateway.build_outbox_consumer", MagicMock())
    monkeypatch.setattr("calfcord.bridge.gateway.build_steps_consumer", MagicMock())
    monkeypatch.setattr("calfcord.bridge.gateway.StepsState", MagicMock())
    monkeypatch.setattr("calfcord.bridge.gateway.run_refresher", _fake_refresher)
    monkeypatch.setattr(
        "calfcord.control_plane.state_consumer.register_state_consumer",
        register_state_consumer,
    )

    handles = MagicMock()
    handles.worker = fake_worker
    handles.gateway = fake_gateway
    handles.register_state_consumer = register_state_consumer
    handles.typing_notifier = typing_notifier
    handles.connect_calls = connect_calls
    return order, handles


def _run_settings():
    from pydantic import SecretStr

    from calfcord.discord.settings import DiscordSettings

    return DiscordSettings(
        bot_token=SecretStr("test-bot-token"),
        application_id=1234,
        guild_id=5678,
        owner_user_id=9999,
    )


def _extract_run_coro(gw):
    """Return the inner ``_run()`` coroutine ``main`` would pass to asyncio.run.

    ``main`` does config validation then ``asyncio.run(_run())``. We monkeypatch
    ``asyncio.run`` to capture the coroutine rather than execute it, so a test
    can drive it under its own event loop with explicit cancellation.
    """
    captured: dict[str, Any] = {}

    def _capture(coro):
        captured["coro"] = coro

    real_run = gw.asyncio.run
    gw.asyncio.run = _capture  # type: ignore[assignment]
    try:
        gw.main()
    finally:
        gw.asyncio.run = real_run  # type: ignore[assignment]
    return captured["coro"]


async def _boot_to_serving_then_sigint(gw, order: _OrderLog, monkeypatch) -> _OrderLog:
    """Drive ``_run`` to the serving state, then simulate SIGINT, returning the log.

    Runs ``_run`` as a task until the (faked, blocking) Discord gateway has
    started — i.e. boot reached the foreground ``asyncio.wait`` — then cancels
    the task to stand in for a SIGINT/SIGTERM, letting the ordered shutdown
    ``finally`` chain run to completion. The returned :class:`_OrderLog` records
    the load-bearing boot/shutdown sequence.
    """
    import asyncio
    import contextlib

    monkeypatch.setattr(gw, "DiscordSettings", lambda *a, **k: _run_settings())
    monkeypatch.setenv("CALF_HOST_URL", "localhost")

    task = asyncio.create_task(_extract_run_coro(gw))
    for _ in range(50):
        await asyncio.sleep(0)
        if "gateway.start" in order.events:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return order


class TestEmbeddedBootOrdering:
    """The embedded boot preserves register-before-serve and ordered shutdown.

    Drives ``main()``'s inner ``_run`` with every collaborator faked (no broker,
    no Discord, no transcript DB) and asserts the load-bearing order: the raw
    state consumer registers, then ``worker.start()`` joins the node groups,
    then the Discord foreground starts; on shutdown the broker drains LAST.
    """

    async def test_state_consumer_registered_before_worker_start(self, _patched_run, monkeypatch) -> None:
        from calfcord.bridge import gateway as gw

        order, _ = _patched_run
        await _boot_to_serving_then_sigint(gw, order, monkeypatch)

        assert order.events.index("register_state_consumer") < order.events.index("worker.start"), (
            f"the raw state consumer must register before worker.start so its "
            f"group joins before the broker serves; got {order.events}"
        )

    async def test_bridge_client_does_not_claim_outbox_as_reply_inbox(self, _patched_run, monkeypatch) -> None:
        """Regression guard for the calfkit 0.10.0 send-API migration.

        The bridge ``Client`` must NOT name ``discord.outbox`` as its own
        ``reply_topic`` — it takes a private auto-generated inbox. This is the
        load-bearing precondition for the ingress/retry sites calling
        ``send(reply_to="discord.outbox")``: calfkit's ``send`` raises
        ``ValueError`` if ``reply_to`` equals the client's own reply inbox. If
        someone re-adds ``reply_topic=DISCORD_OUTBOX_TOPIC`` to the bridge
        connect, every mocked unit test stays green but the bridge breaks at
        runtime on the first agent reply — so pin it here.
        """
        from calfcord.bridge import gateway as gw

        order, handles = _patched_run
        await _boot_to_serving_then_sigint(gw, order, monkeypatch)

        assert handles.connect_calls, "the bridge never called Client.connect"
        for kwargs in handles.connect_calls:
            assert kwargs.get("reply_topic") != "discord.outbox", (
                "the bridge Client must not name discord.outbox as its own reply "
                "inbox — send(reply_to='discord.outbox') would raise on the guard"
            )

    async def test_worker_start_before_gateway_start(self, _patched_run, monkeypatch) -> None:
        from calfcord.bridge import gateway as gw

        order, _ = _patched_run
        await _boot_to_serving_then_sigint(gw, order, monkeypatch)

        assert order.events.index("worker.start") < order.events.index("gateway.start"), (
            f"node consumer groups must join (worker.start) before the gateway "
            f"accepts Discord events; got {order.events}"
        )

    async def test_typing_notifier_closes_after_broker_drain(self, _patched_run, monkeypatch) -> None:
        from calfcord.bridge import gateway as gw

        order, _ = _patched_run
        await _boot_to_serving_then_sigint(gw, order, monkeypatch)

        # Ordered shutdown: stop the Discord ingress (gateway.close), drain the
        # broker (worker.stop — so in-flight discord.outbox replies post before
        # disconnect), THEN close the typing notifier. A steps-consumer hop
        # draining at shutdown can still fire typing, and aclose only cancels the
        # tasks live at that instant (fire has no closed guard) — so closing it
        # before the drain would leave a drain-fired typing task dangling at loop
        # shutdown. Closing after the drain accounts for every fired task.
        assert order.events.index("gateway.close") < order.events.index("worker.stop"), (
            f"worker.stop (broker drain) must run after gateway.close so in-flight "
            f"replies drain before the broker disconnects; got {order.events}"
        )
        assert order.events.index("worker.stop") < order.events.index("typing.aclose"), (
            f"typing_notifier.aclose() must run AFTER the broker drain so a draining "
            f"steps hop never fires a cancelled notifier; got {order.events}"
        )

    async def test_fatal_gateway_crash_propagates_and_still_tears_down(self, _patched_run, monkeypatch) -> None:
        from calfcord.bridge import gateway as gw

        order, handles = _patched_run

        # A FATAL gateway failure (not a shutdown signal): gateway.start() raises
        # instead of blocking. asyncio.wait does NOT propagate a task's exception,
        # so without an explicit re-raise the bridge would exit 0 — a crash
        # masquerading as a clean stop. _run must re-raise it (non-zero exit ⇒
        # supervisor restart) while still running the ordered teardown.
        async def _crashing_start() -> None:
            order.record("gateway.start")
            raise RuntimeError("gateway websocket died")

        monkeypatch.setattr(handles.gateway, "start", _crashing_start)
        monkeypatch.setattr(gw, "DiscordSettings", lambda *a, **k: _run_settings())
        monkeypatch.setenv("CALF_HOST_URL", "localhost")

        coro = _extract_run_coro(gw)
        with pytest.raises(RuntimeError, match="gateway websocket died"):
            await coro

        # Teardown still ran: ingress closed, broker drained, notifier closed.
        assert "gateway.close" in order.events
        assert "worker.stop" in order.events
        assert "typing.aclose" in order.events

    async def test_typing_notifier_closes_even_when_broker_drain_raises(self, _patched_run, monkeypatch) -> None:
        from calfcord.bridge import gateway as gw

        order, handles = _patched_run

        # The drain (worker.stop) raises. The guarded ``try: worker.stop() finally:
        # typing_notifier.aclose()`` must STILL close the notifier (no leaked typing
        # tasks on a flaky drain), and the drain error must propagate (a failed drain
        # is a real fault, not swallowed). Gateway returns cleanly so the foreground
        # race ends on its own — no signal/cancellation needed to reach teardown.
        async def _clean_start() -> None:
            order.record("gateway.start")

        async def _failing_stop() -> None:
            order.record("worker.stop")
            raise RuntimeError("drain failed")

        monkeypatch.setattr(handles.gateway, "start", _clean_start)
        monkeypatch.setattr(handles.worker, "stop", _failing_stop)
        monkeypatch.setattr(gw, "DiscordSettings", lambda *a, **k: _run_settings())
        monkeypatch.setenv("CALF_HOST_URL", "localhost")

        coro = _extract_run_coro(gw)
        with pytest.raises(RuntimeError, match="drain failed"):
            await coro

        assert "typing.aclose" in order.events, "aclose must run even when the drain raises"
        assert order.events.index("worker.stop") < order.events.index("typing.aclose")
