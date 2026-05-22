"""Unit tests for the phonebook wire format."""

from __future__ import annotations

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.phonebook import (
    PhonebookEntry,
    phonebook_from_deps,
    phonebook_from_registry,
    phonebook_to_deps,
)
from calfkit_organization.bridge.registry import AgentRegistry


def _agent(
    agent_id: str,
    *,
    tools: tuple[str, ...] = (),
    avatar_url: str | None = "https://example.com/a.png",
    description: str = "desc",
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        slash=f"/{agent_id}",
        display_name=agent_id.title(),
        description=description,
        avatar_url=avatar_url,
        tools=tools,
        system_prompt="x",
    )


class TestFromRegistry:
    def test_projects_each_relevant_field(self) -> None:
        registry = AgentRegistry(
            [_agent("alice", tools=("private_chat",), description="Sched.")]
        )
        result = phonebook_from_registry(registry)
        assert len(result) == 1
        entry = result[0]
        assert entry.agent_id == "alice"
        assert entry.display_name == "Alice"
        assert entry.avatar_url == "https://example.com/a.png"
        assert entry.description == "Sched."
        assert entry.tools == ("private_chat",)

    def test_empty_registry_yields_empty_phonebook(self) -> None:
        registry = AgentRegistry([])
        assert phonebook_from_registry(registry) == []

    def test_preserves_order_of_registry(self) -> None:
        """Phonebook iteration order is observable in roster output. Pin
        it to registry order so the bridge can choose ordering once."""
        registry = AgentRegistry([_agent("zeta"), _agent("alpha"), _agent("mu")])
        result = phonebook_from_registry(registry)
        assert [e.agent_id for e in result] == ["zeta", "alpha", "mu"]

    def test_filters_out_router(self) -> None:
        """The built-in router has no A2A inbox; listing it as a peer
        would invite assistants' LLMs to call private_chat against a
        topic with no consumer. ``phonebook_from_registry`` filters
        ``role=router`` defensively."""
        router = AgentDefinition(
            agent_id="_router",
            slash="/_router",
            display_name="Router",
            description="Internal routing agent",
            role="router",
            publish_topic="routing.decisions",
            system_prompt="route",
        )
        registry = AgentRegistry([_agent("scribe"), router])
        result = phonebook_from_registry(registry)
        ids = {e.agent_id for e in result}
        assert "_router" not in ids
        assert "scribe" in ids


class TestRoundtripThroughDeps:
    def test_to_deps_returns_json_friendly_dicts(self) -> None:
        """Used inside ``deps={"phonebook": phonebook_to_deps(...)}`` —
        must be JSON-serializable (calfkit serializes deps over Kafka)."""
        entry = PhonebookEntry(
            agent_id="alice",
            display_name="Alice",
            description="x",
            tools=("private_chat",),
        )
        result = phonebook_to_deps([entry])
        assert isinstance(result, list)
        assert isinstance(result[0], dict)
        # Verify it actually round-trips through json.
        import json
        json.dumps(result)

    def test_from_deps_validates_and_reconstructs(self) -> None:
        raw = [
            {
                "agent_id": "alice",
                "display_name": "Alice",
                "avatar_url": None,
                "description": "x",
                "tools": ["private_chat"],
            }
        ]
        result = phonebook_from_deps(raw)
        assert len(result) == 1
        assert isinstance(result[0], PhonebookEntry)
        assert result[0].tools == ("private_chat",)

    def test_from_deps_rejects_non_list(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            phonebook_from_deps({"agent_id": "alice"})

    def test_round_trip_preserves_equality(self) -> None:
        original = [
            PhonebookEntry(
                agent_id="alice",
                display_name="Alice",
                description="x",
                tools=("private_chat",),
            ),
            PhonebookEntry(
                agent_id="bob",
                display_name="Bob",
                avatar_url="https://example.com/b.png",
                description="y",
            ),
        ]
        round_tripped = phonebook_from_deps(phonebook_to_deps(original))
        assert round_tripped == original

    def test_from_deps_normalizes_invalid_entries_to_validation_error(self) -> None:
        """Entries that fail schema validation raise pydantic's
        ``ValidationError`` (a ``ValueError`` subclass). Callers that
        want a single exception type can catch ``ValueError`` to cover
        both list-shape and per-entry failures. Pinned so the
        ``ValidationError <: ValueError`` relationship is part of the
        documented contract."""
        with pytest.raises(ValueError):
            phonebook_from_deps([{"agent_id": "alice"}])  # missing required fields


class TestEntryIsFrozen:
    """``PhonebookEntry`` is the bridge's snapshot of the registry. The
    bridge serializes it into deps and downstream consumers expect a
    stable view — accidental mutation by a tool process must fail
    loudly, not corrupt the source snapshot."""

    def test_cannot_reassign_field(self) -> None:
        entry = PhonebookEntry(
            agent_id="alice",
            display_name="Alice",
            description="x",
        )
        with pytest.raises(ValueError):  # ValidationError <: ValueError
            entry.agent_id = "bob"  # type: ignore[misc]


class TestEntryValidators:
    """Wire-format constraints must not be looser than the source schema —
    a misbehaving bridge that emits an invalid id or an over-long
    description should be rejected at deserialization, not by Discord
    far downstream where the cause is harder to trace."""

    def test_rejects_invalid_agent_id_pattern(self) -> None:
        with pytest.raises(ValueError, match=r"agent_id must match"):
            PhonebookEntry(
                agent_id="HasCaps",  # uppercase fails [a-z0-9_-]
                display_name="X",
                description="x",
            )

    def test_rejects_empty_agent_id(self) -> None:
        with pytest.raises(ValueError, match=r"agent_id must match"):
            PhonebookEntry(agent_id="", display_name="X", description="x")

    def test_rejects_overlong_agent_id(self) -> None:
        with pytest.raises(ValueError, match=r"agent_id must match"):
            PhonebookEntry(
                agent_id="a" * 33,  # max 32
                display_name="X",
                description="x",
            )

    def test_rejects_empty_description(self) -> None:
        with pytest.raises(ValueError, match=r"description must be 1-100"):
            PhonebookEntry(agent_id="alice", display_name="X", description="")

    def test_rejects_overlong_description(self) -> None:
        with pytest.raises(ValueError, match=r"description must be 1-100"):
            PhonebookEntry(
                agent_id="alice",
                display_name="X",
                description="x" * 101,
            )

    def test_accepts_max_length_description(self) -> None:
        """Boundary: exactly 100 chars must work."""
        entry = PhonebookEntry(
            agent_id="alice",
            display_name="X",
            description="x" * 100,
        )
        assert len(entry.description) == 100
