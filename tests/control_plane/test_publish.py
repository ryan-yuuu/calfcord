"""Tests for control-plane publish helpers.

Strategy: stand up a fake client whose ``_connection.publish`` records the
(topic, payload) pairs. Each publish helper should produce exactly one record
on the expected topic with a JSON payload that round-trips back into the
expected pydantic type.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from calfkit_organization.control_plane.publish import (
    publish_control_command,
    publish_departure,
    publish_discovery_ping,
    publish_state_event,
)
from calfkit_organization.control_plane.schema import (
    AgentControlEnvelope,
    AgentDepartureEvent,
    AgentStateEvent,
    DiscoveryPingOp,
    SetThinkingEffortOp,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(
        self, payload: str, *, topic: str, key: bytes | None = None
    ) -> None:
        self.calls.append({"topic": topic, "payload": payload, "key": key})


class _FakeClient:
    def __init__(self) -> None:
        self._connection = _FakeConnection()


async def test_publish_control_command_targets_agent_topic() -> None:
    client = _FakeClient()
    command = SetThinkingEffortOp(
        agent_id="scribe",
        value="high",
        request_id="req-1",
        issued_by="user-42",
    )

    await publish_control_command(client, "scribe", command)  # type: ignore[arg-type]

    assert len(client._connection.calls) == 1
    call = client._connection.calls[0]
    assert call["topic"] == "agent.scribe.control.in"
    # H1/M2: per-agent partition key keeps ordered command delivery on
    # multi-partition control topics.
    assert call["key"] == b"scribe"

    parsed = AgentControlEnvelope.model_validate(call["payload"])
    assert isinstance(parsed.command, SetThinkingEffortOp)
    assert parsed.command.agent_id == "scribe"
    assert parsed.command.value == "high"
    assert parsed.command.request_id == "req-1"
    assert parsed.command.issued_by == "user-42"


async def test_publish_discovery_ping_targets_broadcast_topic() -> None:
    client = _FakeClient()
    await publish_discovery_ping(client)  # type: ignore[arg-type]

    assert len(client._connection.calls) == 1
    call = client._connection.calls[0]
    assert call["topic"] == "bridge.discovery"
    # Discovery is broadcast: not keyed so each agent (sole member of its
    # own consumer group) reads every partition.
    assert call["key"] is None

    parsed = AgentControlEnvelope.model_validate(call["payload"])
    assert isinstance(parsed.command, DiscoveryPingOp)
    assert parsed.command.request_id


async def test_publish_state_event_targets_state_topic() -> None:
    from datetime import datetime

    client = _FakeClient()
    event = AgentStateEvent(
        agent_id="scribe",
        slash="/scribe",
        display_name="Scribe",
        description="Takes notes.",
        role="assistant",
        history_turns=30,
        thinking_effort="high",
        provider="anthropic",
        emitted_at=datetime(2026, 5, 25, 12, 0, tzinfo=UTC),
        cause="startup",
    )
    await publish_state_event(client, event)  # type: ignore[arg-type]

    assert len(client._connection.calls) == 1
    call = client._connection.calls[0]
    assert call["topic"] == "agent.state"
    # H1: per-agent partition key so state events and the eventual
    # departure event share a partition and stay ordered for the bridge.
    assert call["key"] == b"scribe"

    parsed = AgentStateEvent.model_validate(call["payload"])
    assert parsed == event


async def test_publish_departure_targets_state_topic_with_departure_kind() -> None:
    client = _FakeClient()
    await publish_departure(client, "scribe")  # type: ignore[arg-type]

    assert len(client._connection.calls) == 1
    call = client._connection.calls[0]
    assert call["topic"] == "agent.state"
    # H1: shares the partition key of this agent's state events so the
    # bridge sees the departure AFTER any prior state events.
    assert call["key"] == b"scribe"

    # Verify ``kind="departure"`` is on the wire (discriminator field).
    raw = call["payload"]
    assert raw["kind"] == "departure"

    parsed = AgentDepartureEvent.model_validate(call["payload"])
    assert parsed.agent_id == "scribe"
    assert parsed.reason == "shutdown"
