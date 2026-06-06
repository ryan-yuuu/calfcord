"""Tests for the control-plane roster probe.

``reduce_live_roster`` is the pure replay that turns control-plane messages
(collected from ``agent.state``) into the current live roster — the same dispatch
the bridge's state consumer does, as a batch reduction, so any host can
reconstruct "who's alive" without reading the bridge's in-memory registry.
"""

from __future__ import annotations

from datetime import UTC, datetime

from calfcord.control_plane.probe import reduce_live_roster
from calfcord.control_plane.schema import AgentDepartureEvent, AgentStateEvent


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
