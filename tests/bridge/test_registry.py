"""Unit tests for AgentRegistry duplicate detection, the from_agents_dir loader,
and the state-event-driven mutators (upsert_from_state_event, remove,
apply_local_thinking_effort_override)."""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.bridge.registry import AgentRegistry


def _make_definition(**overrides) -> AgentDefinition:
    defaults = dict(
        agent_id="scheduler",
        display_name="Aksel (Scheduler)",
        description="Calendar mechanics.",
        system_prompt="Test scheduler agent.",
    )
    return AgentDefinition(**(defaults | overrides))


class TestAgentRegistryDuplicates:
    def test_duplicate_agent_id_rejected(self) -> None:
        a = _make_definition()
        b = _make_definition(display_name="Other")
        with pytest.raises(ValueError, match="duplicate agent_id"):
            AgentRegistry([a, b])

    def test_duplicate_display_name_rejected(self) -> None:
        a = _make_definition()
        b = _make_definition(agent_id="other")
        with pytest.raises(ValueError, match="duplicate display_name"):
            AgentRegistry([a, b])


class TestAgentRegistryLookups:
    @pytest.fixture
    def registry(self) -> AgentRegistry:
        return AgentRegistry(
            [
                _make_definition(),
                _make_definition(
                    agent_id="finance",
                    display_name="Finn (Finance)",
                    description="Bookkeeping.",
                ),
            ]
        )

    def test_by_id(self, registry: AgentRegistry) -> None:
        assert registry.by_id("scheduler").agent_id == "scheduler"
        assert registry.by_id("missing") is None

    def test_by_display_name(self, registry: AgentRegistry) -> None:
        assert registry.by_display_name("Aksel (Scheduler)").agent_id == "scheduler"
        assert registry.by_display_name("Unknown") is None

    def test_all_returns_definitions_in_order(self, registry: AgentRegistry) -> None:
        all_defs = registry.all()
        assert [d.agent_id for d in all_defs] == ["scheduler", "finance"]


class TestFromAgentsDir:
    """``AgentRegistry.from_agents_dir`` delegates to the loader; this tests the integration."""

    def _write_agent(self, dir_: Path, name: str, **frontmatter_extra) -> None:
        fields = {
            "name": name,
            "display_name": name.title(),
            "description": f"Test agent {name}.",
        }
        fields.update(frontmatter_extra)
        lines = ["---"]
        for k, v in fields.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append(f"You are {name}.")
        (dir_ / f"{name}.md").write_text("\n".join(lines))

    def test_loads_valid_directory(self, tmp_path: Path) -> None:
        self._write_agent(tmp_path, "scheduler")
        self._write_agent(tmp_path, "finance")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        assert registry.by_id("scheduler") is not None
        assert registry.by_id("finance") is not None

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            AgentRegistry.from_agents_dir(tmp_path / "nonexistent")

    def test_duplicate_display_name_in_dir_rejected(self, tmp_path: Path) -> None:
        # Two agents both claim display_name "Shared" — registry catches this.
        self._write_agent(tmp_path, "alice", display_name="Shared")
        self._write_agent(tmp_path, "bob", display_name="Shared")
        with pytest.raises(ValueError, match="duplicate display_name"):
            AgentRegistry.from_agents_dir(tmp_path)


class TestUpsertFromStateEvent:
    """Coverage for the state-event projection mutator."""

    def test_first_seen_returns_true_and_inserts(self) -> None:
        registry = AgentRegistry([])
        definition = _make_definition()
        first_seen = registry.upsert_from_state_event(definition)
        assert first_seen is True
        assert registry.by_id("scheduler") is definition
        assert registry.by_display_name("Aksel (Scheduler)") is definition

    def test_re_announce_returns_false_and_updates_fields(self) -> None:
        registry = AgentRegistry([])
        original = _make_definition(thinking_effort=None)
        registry.upsert_from_state_event(original)
        updated = _make_definition(thinking_effort="high")
        re_seen = registry.upsert_from_state_event(updated)
        assert re_seen is False
        assert registry.by_id("scheduler").thinking_effort == "high"
        # Both indexes point at the same updated instance.
        assert registry.by_display_name("Aksel (Scheduler)").thinking_effort == "high"

    def test_key_field_change_handles_rename(self) -> None:
        """An agent re-announcing with a changed display_name updates all
        indexes via remove-then-insert (bypassing the immutability
        asserts in ``_replace``)."""
        registry = AgentRegistry([])
        original = _make_definition()
        registry.upsert_from_state_event(original)
        renamed = _make_definition(display_name="Aksel v2")
        re_seen = registry.upsert_from_state_event(renamed)
        assert re_seen is False
        # Old display lookup now misses; new one hits.
        assert registry.by_display_name("Aksel (Scheduler)") is None
        assert registry.by_display_name("Aksel v2") is renamed
        # by_id still resolves to the renamed entry.
        assert registry.by_id("scheduler") is renamed

    def test_duplicate_display_name_from_different_agent_raises(self) -> None:
        """If a key-field change would collide with a different
        agent's display_name, restore the old indexes and propagate the
        ValueError so the state consumer can log and skip."""
        registry = AgentRegistry([])
        alice = _make_definition(agent_id="alice", display_name="Alice")
        bob = _make_definition(agent_id="bob", display_name="Bob")
        registry.upsert_from_state_event(alice)
        registry.upsert_from_state_event(bob)
        # Now try to rename Alice to use "Bob" — should raise and leave
        # Alice intact.
        colliding = _make_definition(agent_id="alice", display_name="Bob")
        with pytest.raises(ValueError, match="duplicate display_name"):
            registry.upsert_from_state_event(colliding)
        # Alice's indexes are restored to their pre-attempt state.
        assert registry.by_id("alice") is alice
        assert registry.by_display_name("Alice") is alice
        # Bob is unchanged.
        assert registry.by_id("bob") is bob
        assert registry.by_display_name("Bob") is bob


class TestRemove:
    """Coverage for the state-event departure mutator."""

    def test_remove_existing_agent_returns_true(self) -> None:
        registry = AgentRegistry([])
        registry.upsert_from_state_event(_make_definition())
        removed = registry.remove("scheduler")
        assert removed is True
        assert registry.by_id("scheduler") is None
        assert registry.by_display_name("Aksel (Scheduler)") is None

    def test_remove_unknown_returns_false(self) -> None:
        registry = AgentRegistry([])
        assert registry.remove("ghost") is False

    def test_remove_idempotent(self) -> None:
        registry = AgentRegistry([])
        registry.upsert_from_state_event(_make_definition())
        assert registry.remove("scheduler") is True
        assert registry.remove("scheduler") is False


class TestApplyLocalThinkingEffortOverride:
    """Coverage for the optimistic in-memory ``/thinking-effort`` update."""

    def test_returns_new_definition_with_updated_effort(self) -> None:
        registry = AgentRegistry([])
        registry.upsert_from_state_event(_make_definition(thinking_effort=None))
        new_def = registry.apply_local_thinking_effort_override("scheduler", "high")
        assert new_def is not None
        assert new_def.thinking_effort == "high"
        assert registry.by_id("scheduler").thinking_effort == "high"
        assert registry.by_display_name("Aksel (Scheduler)").thinking_effort == "high"

    def test_unknown_agent_returns_none(self) -> None:
        registry = AgentRegistry([])
        result = registry.apply_local_thinking_effort_override("ghost", "high")
        assert result is None
        assert registry.all() == ()
