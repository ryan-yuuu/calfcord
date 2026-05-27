"""Tests for the agent-side control-plane sink handler."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter

from calfkit_organization.agents.definition import parse_agent_md
from calfkit_organization.control_plane.definition_ref import AgentDefinitionRef
from calfkit_organization.control_plane.schema import (
    AgentControlEnvelope,
    AgentStateEvent,
    DiscoveryPingOp,
    SetThinkingEffortOp,
)
from calfkit_organization.control_plane.sink import make_control_sink_handler


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


def _seed_md(
    path: Path,
    *,
    agent_id: str = "scribe",
    provider: str = "anthropic",
    thinking_effort: str | None = "low",
    body: str = "You are a scribe.",
) -> Path:
    meta: dict[str, str] = {
        "name": agent_id,
        "slash": f"/{agent_id}",
        "display_name": agent_id.capitalize(),
        "description": f"Test {agent_id}.",
        "provider": provider,
    }
    if thinking_effort is not None:
        meta["thinking_effort"] = thinking_effort
    post = frontmatter.Post(body, **meta)
    md_path = path / f"{agent_id}.md"
    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return md_path


def _envelope_with(command: Any) -> AgentControlEnvelope:
    return AgentControlEnvelope(command=command)


async def test_set_thinking_effort_matching_id_applies_and_announces(
    tmp_path: Path,
) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    defn = parse_agent_md(md_path)
    ref = AgentDefinitionRef(current=defn)
    client = _FakeClient()
    handler = make_control_sink_handler(client, ref)  # type: ignore[arg-type]

    envelope = _envelope_with(
        SetThinkingEffortOp(
            agent_id="scribe",
            value="high",
            request_id="req-1",
            issued_by="user-7",
        ),
    )
    await handler(envelope)

    # Disk updated.
    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["thinking_effort"] == "high"

    # Ref updated.
    assert ref.current.thinking_effort == "high"

    # Announce published once on agent.state with cause=command_applied.
    assert len(client._connection.calls) == 1
    call = client._connection.calls[0]
    assert call["topic"] == "agent.state"
    parsed = AgentStateEvent.model_validate(call["payload"])
    assert parsed.cause == "command_applied"
    assert parsed.thinking_effort == "high"
    assert parsed.agent_id == "scribe"


async def test_set_thinking_effort_with_mismatched_agent_id_is_ignored(
    tmp_path: Path,
) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    original = md_path.read_text(encoding="utf-8")
    defn = parse_agent_md(md_path)
    ref = AgentDefinitionRef(current=defn)
    client = _FakeClient()
    handler = make_control_sink_handler(client, ref)  # type: ignore[arg-type]

    envelope = _envelope_with(
        SetThinkingEffortOp(
            agent_id="not-scribe",
            value="high",
            request_id="req-2",
            issued_by="user-7",
        ),
    )
    await handler(envelope)

    # Disk unchanged.
    assert md_path.read_text(encoding="utf-8") == original
    # Ref unchanged.
    assert ref.current is defn
    # No publish.
    assert client._connection.calls == []


async def test_discovery_ping_publishes_state_event(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="medium")
    original = md_path.read_text(encoding="utf-8")
    defn = parse_agent_md(md_path)
    ref = AgentDefinitionRef(current=defn)
    client = _FakeClient()
    handler = make_control_sink_handler(client, ref)  # type: ignore[arg-type]

    envelope = _envelope_with(
        DiscoveryPingOp(
            issued_at=datetime(2026, 5, 25, tzinfo=UTC),
            request_id="ping-1",
        ),
    )
    await handler(envelope)

    # Disk unchanged.
    assert md_path.read_text(encoding="utf-8") == original
    # Ref unchanged.
    assert ref.current is defn
    # One publish on agent.state with cause=discovery_response.
    assert len(client._connection.calls) == 1
    call = client._connection.calls[0]
    assert call["topic"] == "agent.state"
    parsed = AgentStateEvent.model_validate(call["payload"])
    assert parsed.cause == "discovery_response"
    assert parsed.thinking_effort == "medium"


async def test_envelope_with_wrong_schema_version_is_ignored(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    original = md_path.read_text(encoding="utf-8")
    defn = parse_agent_md(md_path)
    ref = AgentDefinitionRef(current=defn)
    client = _FakeClient()
    handler = make_control_sink_handler(client, ref)  # type: ignore[arg-type]

    envelope = AgentControlEnvelope(
        schema_version=999,
        command=SetThinkingEffortOp(
            agent_id="scribe",
            value="high",
            request_id="req-3",
            issued_by="user-7",
        ),
    )
    await handler(envelope)

    assert md_path.read_text(encoding="utf-8") == original
    assert ref.current is defn
    assert client._connection.calls == []


async def test_set_thinking_effort_with_missing_source_file_is_swallowed(
    tmp_path: Path,
) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    defn = parse_agent_md(md_path)
    # Repoint source_path at a nonexistent file (frozen model: use model_copy).
    ghost = tmp_path / "ghost.md"
    defn = defn.model_copy(update={"source_path": ghost})
    ref = AgentDefinitionRef(current=defn)
    client = _FakeClient()
    handler = make_control_sink_handler(client, ref)  # type: ignore[arg-type]

    envelope = _envelope_with(
        SetThinkingEffortOp(
            agent_id="scribe",
            value="high",
            request_id="req-4",
            issued_by="user-7",
        ),
    )
    # Must not raise; logs and returns.
    await handler(envelope)

    assert ref.current is defn
    assert client._connection.calls == []
