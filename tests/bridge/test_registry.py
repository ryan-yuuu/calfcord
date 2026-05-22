"""Unit tests for AgentRegistry duplicate detection and the from_agents_dir loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.registry import AgentRegistry


def _make_definition(**overrides) -> AgentDefinition:
    defaults = dict(
        agent_id="scheduler",
        slash="/scheduler",
        display_name="Aksel (Scheduler)",
        description="Calendar mechanics.",
        system_prompt="Test scheduler agent.",
    )
    return AgentDefinition(**(defaults | overrides))


class TestAgentRegistryDuplicates:
    def test_duplicate_agent_id_rejected(self) -> None:
        a = _make_definition()
        b = _make_definition(slash="/other", display_name="Other")
        with pytest.raises(ValueError, match="duplicate agent_id"):
            AgentRegistry([a, b])

    def test_duplicate_slash_rejected(self) -> None:
        a = _make_definition()
        b = _make_definition(agent_id="other", display_name="Other")
        with pytest.raises(ValueError, match="duplicate slash"):
            AgentRegistry([a, b])

    def test_duplicate_display_name_rejected(self) -> None:
        a = _make_definition()
        b = _make_definition(agent_id="other", slash="/other")
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
                    slash="/finance",
                    display_name="Finn (Finance)",
                    description="Bookkeeping.",
                ),
            ]
        )

    def test_by_id(self, registry: AgentRegistry) -> None:
        assert registry.by_id("scheduler").agent_id == "scheduler"
        assert registry.by_id("missing") is None

    def test_by_slash(self, registry: AgentRegistry) -> None:
        assert registry.by_slash("/finance").agent_id == "finance"
        assert registry.by_slash("/nope") is None

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
            "slash": f"/{name}",
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

    def test_empty_directory_returns_router_only_registry(self, tmp_path: Path) -> None:
        """An empty agents dir still produces a registry containing the
        built-in router (appended unconditionally by
        :meth:`from_agents_dir`). The router-count invariant requires
        it, and the loader's "no agents" case maps to "router only"."""
        registry = AgentRegistry.from_agents_dir(tmp_path)
        all_defs = registry.all()
        assert len(all_defs) == 1
        assert all_defs[0].role == "router"

    def test_duplicate_slash_in_dir_rejected(self, tmp_path: Path) -> None:
        # Two agents both claim slash /shared — registry catches this.
        self._write_agent(tmp_path, "alice", slash="/shared")
        self._write_agent(tmp_path, "bob", slash="/shared")
        with pytest.raises(ValueError, match="duplicate slash"):
            AgentRegistry.from_agents_dir(tmp_path)


class TestSetThinkingEffort:
    """Coverage for the runtime mutator that rewrites .md + swaps the index."""

    @staticmethod
    def _write_agent(dir_: Path, name: str, **frontmatter_extra) -> None:
        fields: dict[str, str] = {
            "name": name,
            "slash": f"/{name}",
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

    async def test_rewrites_md_and_swaps_in_memory(self, tmp_path: Path) -> None:
        self._write_agent(tmp_path, "scheduler", provider="anthropic")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        assert registry.by_id("scheduler").thinking_effort is None

        new_def = await registry.set_thinking_effort("scheduler", "high")
        assert new_def.thinking_effort == "high"
        assert registry.by_id("scheduler").thinking_effort == "high"

        import frontmatter

        reloaded = frontmatter.load(tmp_path / "scheduler.md")
        assert reloaded.metadata["thinking_effort"] == "high"

    async def test_other_index_maps_stay_consistent(self, tmp_path: Path) -> None:
        self._write_agent(tmp_path, "scheduler")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        await registry.set_thinking_effort("scheduler", "high")

        # by_slash / by_display_name return the freshly-swapped entry.
        assert registry.by_slash("/scheduler").thinking_effort == "high"
        assert registry.by_display_name("Scheduler").thinking_effort == "high"
        # all() exposes the swapped scheduler entry exactly once (the
        # built-in router is also in the registry, but it has a
        # different agent_id and isn't affected by this mutation).
        all_defs = registry.all()
        scheduler_defs = [d for d in all_defs if d.agent_id == "scheduler"]
        assert len(scheduler_defs) == 1
        assert scheduler_defs[0].thinking_effort == "high"

    async def test_unknown_agent_raises(self, tmp_path: Path) -> None:
        self._write_agent(tmp_path, "scheduler")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        with pytest.raises(KeyError):
            await registry.set_thinking_effort("ghost", "high")

    async def test_no_source_path_raises(self) -> None:
        """An AgentDefinition built in memory (no .md) cannot be rewritten."""
        d = AgentDefinition(
            agent_id="scheduler",
            slash="/scheduler",
            display_name="Aksel (Scheduler)",
            description="Calendar.",
            system_prompt="Test scheduler.",
        )
        registry = AgentRegistry([d])
        with pytest.raises(ValueError, match="source_path"):
            await registry.set_thinking_effort("scheduler", "high")

    async def test_missing_md_file_raises_filenotfounderror(self, tmp_path: Path) -> None:
        """File disappeared between registry construction and the mutation."""
        self._write_agent(tmp_path, "scheduler")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        (tmp_path / "scheduler.md").unlink()

        with pytest.raises(FileNotFoundError):
            await registry.set_thinking_effort("scheduler", "high")

    async def test_concurrent_writes_produce_consistent_final_state(
        self, tmp_path: Path
    ) -> None:
        """Concurrent set_thinking_effort calls produce a consistent final
        disk + in-memory state.

        Today's writer is fully synchronous, so on a single-threaded
        asyncio event loop these calls run end-to-end serially even
        without the registry's lock — this test does NOT exercise the
        lock's mutual-exclusion contract. The lock is forward-compat
        for an async writer; if a future change adds awaits inside the
        critical section, expand this test to inject an
        ``asyncio.Event``-driven interleaving probe to actually catch
        a missing lock.

        What this DOES verify: end-to-end consistency. Whichever effort
        landed in the final ``os.replace`` is the value present in both
        the in-memory registry and the on-disk frontmatter.
        """
        import asyncio

        import frontmatter

        self._write_agent(tmp_path, "scheduler", provider="anthropic")
        registry = AgentRegistry.from_agents_dir(tmp_path)

        await asyncio.gather(
            registry.set_thinking_effort("scheduler", "low"),
            registry.set_thinking_effort("scheduler", "high"),
        )

        final_in_memory = registry.by_id("scheduler").thinking_effort
        final_on_disk = frontmatter.load(tmp_path / "scheduler.md").metadata.get(
            "thinking_effort"
        )
        assert final_in_memory == final_on_disk
        assert final_in_memory in ("low", "high")
