"""Tests for the control-plane roster probe.

``reduce_live_roster`` is the pure replay that turns control-plane messages
(collected from ``agent.state``) into the current live roster — the same dispatch
the bridge's state consumer does, as a batch reduction, so any host can
reconstruct "who's alive" without reading the bridge's in-memory registry.

``probe_live_roster`` is the live, broker-backed orchestrator around that replay.
Its offline tests (mirroring ``tests/control_plane/test_first_reply.py``) inject a
fake :class:`~calfkit.client.Client` that captures the raw subscriber's
topic/group_id/offset-reset, monkeypatch the module-level
``provision_and_start_broker`` and ``publish_discovery_ping`` seams, feed synthetic
``AgentState`` messages through the captured collector, and assert the reduced
roster + the isolation (unique non-bridge ``latest``-offset group) + that the
discovery ping is RE-PUBLISHED across the window (the join-gap fix) — all without
a broker, FastStream, or an LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from calfcord.control_plane import probe as probe_mod
from calfcord.control_plane.probe import probe_live_roster, reduce_live_roster
from calfcord.control_plane.schema import AgentDepartureEvent, AgentStateEvent
from calfcord.control_plane.topics import AGENT_STATE_TOPIC


def _state_event(agent_id: str = "scribe", **overrides: object) -> AgentStateEvent:
    base: dict[str, object] = {
        "agent_id": agent_id,
        "display_name": agent_id.capitalize(),
        "description": "Takes notes.",
        "role": "assistant",
        "history_turns": 20,
        "thinking_effort": "high",
        "provider": "anthropic",
        "emitted_at": datetime(2026, 5, 25, tzinfo=UTC),
        "cause": "discovery_response",
    }
    base.update(overrides)
    return AgentStateEvent(**base)  # type: ignore[arg-type]


def _departure(agent_id: str = "scribe") -> AgentDepartureEvent:
    return AgentDepartureEvent(
        agent_id=agent_id,
        departed_at=datetime(2026, 5, 25, tzinfo=UTC),
    )


def test_empty_input_yields_empty_roster() -> None:
    assert reduce_live_roster([]) == []


def test_single_state_event_is_in_roster() -> None:
    event = _state_event("scribe")
    assert reduce_live_roster([event]) == [event]


def test_later_event_replaces_earlier_for_same_agent() -> None:
    first = _state_event("scribe", cause="startup")
    second = _state_event("scribe", cause="discovery_response")
    assert reduce_live_roster([first, second]) == [second]


def test_departure_removes_agent() -> None:
    event = _state_event("scribe")
    departure = _departure("scribe")
    assert reduce_live_roster([event, departure]) == []


def test_reannounce_after_departure_readds_agent() -> None:
    startup = _state_event("scribe", cause="startup")
    departure = _departure("scribe")
    reannounce = _state_event("scribe", cause="discovery_response")
    assert reduce_live_roster([startup, departure, reannounce]) == [reannounce]


def test_wrong_schema_version_event_is_ignored() -> None:
    event = _state_event("scribe").model_copy(update={"schema_version": 999})
    assert reduce_live_roster([event]) == []


def test_wrong_schema_version_departure_does_not_remove() -> None:
    event = _state_event("scribe")
    stale_departure = _departure("scribe").model_copy(update={"schema_version": 999})
    assert reduce_live_roster([event, stale_departure]) == [event]


def test_multiple_agents_are_sorted_by_agent_id() -> None:
    zelda = _state_event("zelda")
    apollo = _state_event("apollo")
    assert reduce_live_roster([zelda, apollo]) == [apollo, zelda]


# --------------------------------------------------------------------------- #
# probe_live_roster: the live orchestrator, driven OFFLINE
# --------------------------------------------------------------------------- #


class _Broker:
    """Minimal FastStream-broker stand-in for the fake client's ``broker`` prop.

    ``provision_and_start_broker`` is monkeypatched out in these tests, so the
    broker is never actually started; this only exists so attribute access on the
    injected client doesn't blow up.
    """

    running = False


class _FakeConnection:
    """Captures the raw ``broker.subscriber(...)`` call the probe makes.

    The probe registers its ``agent.state`` collector via
    ``client._connection.subscriber(topic, group_id=..., auto_offset_reset=...)``
    and then calls the returned decorator with its ``_collect`` coroutine. This
    records the subscriber kwargs (for the isolation assertions) and stashes the
    collector so the test can feed it synthetic state messages — exactly the seam
    a real aiokafka consumer would push records through.
    """

    def __init__(self) -> None:
        self.sub_topic: str | None = None
        self.sub_group_id: str | None = None
        self.sub_offset_reset: str | None = None
        self.collector: Any = None

    def subscriber(self, topic: str, *, group_id: str, auto_offset_reset: str) -> Any:
        self.sub_topic = topic
        self.sub_group_id = group_id
        self.sub_offset_reset = auto_offset_reset

        def _register(fn: Any) -> Any:
            self.collector = fn
            return fn

        return _register


class _FakeClient:
    """Injected transient client: an async context manager (the probe uses
    ``async with Client.connect(...) as client``) exposing only the surfaces the
    probe touches — ``_connection`` (raw subscriber) and ``broker``."""

    def __init__(self) -> None:
        self._connection = _FakeConnection()
        self.broker = _Broker()

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _install_fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Patch ``Client.connect`` (referenced as ``probe.Client``) to return our
    fake, and return the fake so the test can read what the probe captured."""
    fake = _FakeClient()

    def _connect(server_urls: str, *, provisioning: object) -> _FakeClient:
        return fake

    monkeypatch.setattr(probe_mod.Client, "connect", staticmethod(_connect))
    return fake


async def test_probe_reduces_collected_state_into_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (offline): the probe subscribes a collector, the discovery ping
    publish drives synthetic agent replies into it, and the result is the reduced,
    sorted, ``AgentDefinition``-projected live roster."""
    fake = _install_fake_client(monkeypatch)

    async def _no_broker(client: object, server_urls: object, topics: object) -> None:
        # The real helper provisions blind-spot topics and bare-starts the broker;
        # offline there is no broker, so this is a no-op.
        return None

    monkeypatch.setattr(probe_mod, "provision_and_start_broker", _no_broker)

    async def _ping_delivers_replies(client: object) -> None:
        # Each discovery ping makes running agents re-announce on agent.state; model
        # that by pushing two agents' state events through the captured collector.
        # Idempotent across re-rounds (reduce_live_roster dedupes by agent_id).
        assert fake._connection.collector is not None
        await fake._connection.collector(_state_event("zelda"))
        await fake._connection.collector(_state_event("apollo"))

    monkeypatch.setattr(probe_mod, "publish_discovery_ping", _ping_delivers_replies)

    roster = await probe_live_roster("localhost:9092", timeout_s=0.01)

    # Reduced + sorted by agent_id, projected to AgentDefinition.
    assert [d.agent_id for d in roster] == ["apollo", "zelda"]


async def test_probe_subscriber_is_isolated_latest_offset_unique_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw collector must read ``agent.state`` at ``latest`` offset in a UNIQUE
    consumer group that is NOT the bridge's — so the probe sees only replies to its
    own pings and never disturbs the bridge's state-consumer group."""
    fake = _install_fake_client(monkeypatch)

    async def _no_broker(client: object, server_urls: object, topics: object) -> None:
        return None

    async def _noop_ping(client: object) -> None:
        return None

    monkeypatch.setattr(probe_mod, "provision_and_start_broker", _no_broker)
    monkeypatch.setattr(probe_mod, "publish_discovery_ping", _noop_ping)

    await probe_live_roster("localhost:9092", timeout_s=0.01)

    conn = fake._connection
    assert conn.sub_topic == AGENT_STATE_TOPIC
    assert conn.sub_offset_reset == "latest"
    # A per-probe group: unique-ish and never the bridge's own state-consumer group.
    assert conn.sub_group_id is not None
    assert conn.sub_group_id.startswith("calfcord-probe-")
    assert "bridge" not in conn.sub_group_id


async def test_probe_republishes_ping_across_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery ping must be RE-PUBLISHED across the window, not sent once.

    aiokafka resolves group partition assignment asynchronously after start(), so
    a single ping at t=0 races the join: replies in the join gap are dropped at
    ``latest`` offset, under-reporting live agents (worst for remote agents). The
    fix re-sends the ping every ~0.5s until ``timeout_s`` elapses so a re-announce
    lands after the group joins. With a window comfortably larger than the
    republish interval, the probe must publish MORE THAN ONCE."""
    _install_fake_client(monkeypatch)

    async def _no_broker(client: object, server_urls: object, topics: object) -> None:
        return None

    monkeypatch.setattr(probe_mod, "provision_and_start_broker", _no_broker)

    pings = 0

    async def _count_pings(client: object) -> None:
        nonlocal pings
        pings += 1

    monkeypatch.setattr(probe_mod, "publish_discovery_ping", _count_pings)

    # A window several republish-intervals wide; bounded so the test stays fast.
    await probe_live_roster("localhost:9092", timeout_s=1.2)

    assert pings > 1, f"expected the ping to be re-published across the window, got {pings}"


async def test_probe_window_is_bounded_by_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-publishing must NOT inflate latency: total wall time stays bounded by
    ``timeout_s`` (the bounded-window contract the deep-probe / agent ps rely on)."""
    _install_fake_client(monkeypatch)

    async def _no_broker(client: object, server_urls: object, topics: object) -> None:
        return None

    async def _noop_ping(client: object) -> None:
        return None

    monkeypatch.setattr(probe_mod, "provision_and_start_broker", _no_broker)
    monkeypatch.setattr(probe_mod, "publish_discovery_ping", _noop_ping)

    import time

    start = time.monotonic()
    await probe_live_roster("localhost:9092", timeout_s=0.5)
    elapsed = time.monotonic() - start

    # Allow generous scheduler slack but assert it did not run, e.g., timeout_s per
    # ping (which a naive "sleep timeout_s after every ping" loop would do).
    assert elapsed < 1.5, f"probe window overran its bound: {elapsed:.3f}s for a 0.5s timeout"
