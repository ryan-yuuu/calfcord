"""Unit tests for :class:`AgentRegistry`'s router integration.

The router definition is appended automatically by
:meth:`from_agents_dir` and accessed via the :meth:`router` accessor.
Multi-router lists raise at construction time; zero-router lists
raise lazily at lookup time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.agents.phonebook import phonebook_from_registry
from calfcord.bridge.registry import AgentRegistry
from calfcord.router.definition import ROUTER_AGENT_ID, build_router_definition


def _write_agent(dir_: Path, name: str, **frontmatter_extra: str) -> None:
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


class TestFromAgentsDirAppendsRouter:
    """``from_agents_dir`` always includes the built-in router."""

    def test_empty_dir_yields_router_only(self, tmp_path: Path) -> None:
        registry = AgentRegistry.from_agents_dir(tmp_path)
        all_defs = registry.all()
        assert len(all_defs) == 1
        assert all_defs[0].agent_id == ROUTER_AGENT_ID
        assert all_defs[0].role == "router"

    def test_dir_with_assistants_yields_assistants_plus_router(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scribe")
        _write_agent(tmp_path, "conan")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        ids = sorted(d.agent_id for d in registry.all())
        assert ids == sorted([ROUTER_AGENT_ID, "conan", "scribe"])

    def test_router_lookup_by_id_works(self, tmp_path: Path) -> None:
        registry = AgentRegistry.from_agents_dir(tmp_path)
        looked_up = registry.by_id(ROUTER_AGENT_ID)
        assert looked_up is not None
        assert looked_up.role == "router"


class TestRouterAccessor:
    """:meth:`AgentRegistry.router` returns the singleton router."""

    def test_returns_router_from_disk_loaded_registry(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scribe")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        router = registry.router()
        assert router.agent_id == ROUTER_AGENT_ID
        assert router.role == "router"
        assert router.publish_topic == "routing.decisions"

    def test_returns_router_from_in_memory_registry(self) -> None:
        """Direct construction with a router-included list works too —
        :meth:`router` reads from ``self._all`` and doesn't care how
        the registry was built."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scribe",
                    display_name="Scribe",
                    description="Notes.",
                    system_prompt="x",
                ),
                build_router_definition(),
            ]
        )
        router = registry.router()
        assert router.agent_id == ROUTER_AGENT_ID

    def test_zero_routers_raises(self) -> None:
        """Direct construction without a router IS allowed (in-memory
        test fixtures may not need routing) but the accessor raises
        lazily so a production caller depending on routing sees a
        clear error."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scribe",
                    display_name="Scribe",
                    description="Notes.",
                    system_prompt="x",
                ),
            ]
        )
        with pytest.raises(ValueError, match="zero router"):
            registry.router()


class TestMultiRouterRejection:
    """A registry with two routers is always a wiring bug — only the
    built-in singleton should declare ``role="router"``. The
    constructor rejects this at construction time."""

    def test_two_routers_raises_at_construction(self) -> None:
        router_a = build_router_definition()
        router_b = AgentDefinition(
            agent_id="custom_router",
            display_name="Custom Router",
            description="Another router.",
            role="router",
            publish_topic="custom.topic",
            system_prompt="x",
        )
        with pytest.raises(ValueError, match="multiple router"):
            AgentRegistry([router_a, router_b])

    def test_duplicate_display_name_precedence_over_multi_router(self) -> None:
        """:class:`AgentRegistry`'s constructor docstring documents
        that duplicate-key errors (display_name / agent_id) take
        precedence over the multi-router check, because the
        duplicate-key message is more operator-actionable.
        Indexing runs first; the role check follows. Pin the
        ordering so a future refactor doesn't silently swap them
        and produce the less-helpful 'multiple router' error when
        the real fix is a display_name rename."""
        router_a = build_router_definition()
        # Collide on display_name, not just role.
        router_b = AgentDefinition(
            agent_id="custom_router",
            display_name=router_a.display_name,  # duplicate
            description="Another router.",
            role="router",
            publish_topic="custom.topic",
            system_prompt="x",
        )
        with pytest.raises(ValueError, match="duplicate display_name"):
            AgentRegistry([router_a, router_b])


class TestPhonebookExcludesRouter:
    """The router is **filtered out** of :func:`phonebook_from_registry`
    output. It has no A2A inbox (``agent.{id}.in``), so listing it as
    a peer would mislead assistants' LLMs into calling
    :func:`~calfcord.tools.private_chat.private_chat`
    against a topic with no consumer — silent timeout, wasted tokens.
    The router-side roster (built by
    :func:`calfcord.router.roster.build_router_temp_instructions`)
    is the intentional place for the router-visible agent list."""

    def test_router_not_in_phonebook(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scribe")
        registry = AgentRegistry.from_agents_dir(tmp_path)
        phonebook = phonebook_from_registry(registry)
        ids = {e.agent_id for e in phonebook}
        assert ROUTER_AGENT_ID not in ids
        assert "scribe" in ids


class TestAgentIdAndDisplayNameCollisionWithRouter:
    """User-defined agents that collide with the router's reserved
    ``agent_id`` (``_router``) or ``display_name`` (``Router``) fail at
    construction time via the existing duplicate-detection in
    :meth:`_index`. We pin this here so a future relaxation of those
    checks doesn't silently break the router's reservation.

    The slash command is always ``/<agent_id>``, so agent_id
    uniqueness implicitly reserves the router's slash too — no
    separate slash-collision test is needed."""

    def test_user_agent_with_router_agent_id_collides(self) -> None:
        user_agent = AgentDefinition(
            agent_id=ROUTER_AGENT_ID,  # collides with router's reserved id
            display_name="MyRouter",
            description="x",
            system_prompt="x",
        )
        with pytest.raises(ValueError, match="duplicate agent_id"):
            AgentRegistry([user_agent, build_router_definition()])

    def test_user_agent_with_router_display_name_collides(self) -> None:
        user_agent = AgentDefinition(
            agent_id="my_router",
            display_name="Router",  # collides with router's reserved name
            description="x",
            system_prompt="x",
        )
        with pytest.raises(ValueError, match="duplicate display_name"):
            AgentRegistry([user_agent, build_router_definition()])
