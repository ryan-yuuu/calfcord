"""Tests for the AgentDefinition <-> AgentStateEvent projection helpers."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.control_plane.builders import (
    build_state_event,
    state_event_to_definition,
)
from calfkit_organization.control_plane.schema import AgentStateEvent


def _make_definition(**overrides: object) -> AgentDefinition:
    base: dict[str, object] = {
        "agent_id": "scribe",
        "display_name": "Scribe",
        "description": "Takes notes.",
        "avatar_url": "https://example.com/a.png",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "tools": ("calendar",),
        "thinking_effort": "high",
        "role": "assistant",
        "history_turns": 20,
        "system_prompt": "You are the scribe.",
        "source_path": Path("/tmp/scribe.md"),
    }
    base.update(overrides)
    return AgentDefinition(**base)  # type: ignore[arg-type]


def test_build_state_event_projects_all_visible_fields() -> None:
    defn = _make_definition()
    event = build_state_event(defn, "startup")

    assert event.kind == "state"
    assert event.agent_id == "scribe"
    assert event.display_name == "Scribe"
    assert event.description == "Takes notes."
    assert event.avatar_url == "https://example.com/a.png"
    assert event.role == "assistant"
    assert event.history_turns == 20
    assert event.thinking_effort == "high"
    assert event.provider == "anthropic"
    assert event.memory is False
    assert event.cause == "startup"
    assert event.emitted_at is not None


def test_build_state_event_excludes_internal_fields() -> None:
    """Verify wire form omits agent-internal fields."""
    field_names = set(AgentStateEvent.model_fields.keys())
    assert "system_prompt" not in field_names
    assert "tools" not in field_names
    assert "model" not in field_names
    assert "publish_topic" not in field_names
    assert "source_path" not in field_names


def test_build_state_event_with_each_cause() -> None:
    defn = _make_definition()
    for cause in ("startup", "command_applied", "discovery_response"):
        event = build_state_event(defn, cause)  # type: ignore[arg-type]
        assert event.cause == cause


def test_state_event_to_definition_produces_valid_definition() -> None:
    from datetime import datetime

    event = AgentStateEvent(
        agent_id="scribe",
        display_name="Scribe",
        description="Takes notes.",
        avatar_url=None,
        role="assistant",
        history_turns=25,
        thinking_effort="medium",
        provider="openai",
        emitted_at=datetime(2026, 5, 25, tzinfo=UTC),
        cause="startup",
    )

    defn = state_event_to_definition(event)
    assert defn.agent_id == "scribe"
    assert defn.display_name == "Scribe"
    assert defn.description == "Takes notes."
    assert defn.role == "assistant"
    assert defn.history_turns == 25
    assert defn.thinking_effort == "medium"
    assert defn.provider == "openai"
    # Stubbed internals.
    assert defn.system_prompt  # non-empty
    assert defn.tools == ()
    assert defn.model is None
    assert defn.publish_topic is None
    assert defn.source_path is None


def test_round_trip_definition_to_event_to_definition() -> None:
    original = _make_definition()
    event = build_state_event(original, "startup")
    rebuilt = state_event_to_definition(event)

    assert rebuilt.agent_id == original.agent_id
    assert rebuilt.display_name == original.display_name
    assert rebuilt.description == original.description
    assert rebuilt.role == original.role
    assert rebuilt.avatar_url == original.avatar_url
    assert rebuilt.history_turns == original.history_turns
    assert rebuilt.thinking_effort == original.thinking_effort
    assert rebuilt.provider == original.provider
    assert rebuilt.memory == original.memory


def test_round_trip_preserves_memory_opt_in() -> None:
    """Regression: a ``memory: true`` agent must still read as memory-enabled
    after the definition -> event -> definition control-plane round trip.

    The bridge gates shipping the memory-prompt template in ``deps`` on
    ``any(spec.memory ...)`` over its registry, and that registry is rebuilt
    from these state events. If ``memory`` is dropped on either projection,
    the bridge never ships the template and memory silently no-ops for every
    agent — the bug this test guards against.
    """
    original = _make_definition(memory=True)
    event = build_state_event(original, "startup")
    assert event.memory is True

    rebuilt = state_event_to_definition(event)
    assert rebuilt.memory is True


def test_state_event_to_definition_defaults_memory_off_when_absent() -> None:
    """An event from an agent predating the ``memory`` field (it simply omits
    the key on the wire) must deserialize as memory-off rather than erroring —
    the backward-compatibility the defaulted field buys us without a
    schema_version bump."""
    from datetime import datetime

    event = AgentStateEvent.model_validate(
        {
            "agent_id": "scribe",
            "display_name": "Scribe",
            "description": "d",
            "role": "assistant",
            "history_turns": 10,
            "emitted_at": datetime(2026, 5, 25, tzinfo=UTC),
            "cause": "startup",
        }
    )
    assert event.memory is False
    assert state_event_to_definition(event).memory is False


def test_state_event_to_definition_assistant_has_no_publish_topic() -> None:
    """Assistant role mandates publish_topic=None; the rebuilt definition must
    satisfy that validator without surprise."""
    from datetime import datetime

    event = AgentStateEvent(
        agent_id="scribe",
        display_name="Scribe",
        description="d",
        role="assistant",
        history_turns=10,
        emitted_at=datetime(2026, 5, 25, tzinfo=UTC),
        cause="startup",
    )
    rebuilt = state_event_to_definition(event)
    assert rebuilt.publish_topic is None
    assert rebuilt.role == "assistant"
