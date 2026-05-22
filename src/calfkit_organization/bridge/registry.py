"""Static agent roster, indexed for the bridge's lookups.

The registry is the single source of truth for which agents exist, what
slash command each one owns, and what display name to attribute a
persona-webhook message to. It is loaded once at daemon startup; new
agents require a restart.

The agent definitions themselves live in :mod:`calfkit_organization.agents`
(parsed from ``agents/*.md`` files). This module owns the *index* the
bridge uses (O(1) lookups by id, slash, and display name, plus rejection
of duplicate ``slash`` or ``display_name`` across agents) and the
in-process mutator for the one frontmatter field that operators can edit
at runtime: ``thinking_effort``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Self

from calfkit_organization.agents.definition import AgentDefinition, ThinkingEffort
from calfkit_organization.agents.loader import load_agents_dir
from calfkit_organization.agents.md_writer import update_thinking_effort
from calfkit_organization.router.definition import build_router_definition


class AgentRegistry:
    """In-memory index of :class:`AgentDefinition`s with O(1) lookups.

    Routers (``role == "router"``) are first-class index members but are
    treated specially: there must be exactly one router in the registry
    (the built-in singleton). Zero or more-than-one routers indicate a
    boot-time wiring bug and are rejected here so the failure surfaces
    immediately rather than at first ambient invocation.
    """

    def __init__(self, definitions: Sequence[AgentDefinition]) -> None:
        self._by_id: dict[str, AgentDefinition] = {}
        self._by_slash: dict[str, AgentDefinition] = {}
        self._by_display_name: dict[str, AgentDefinition] = {}
        self._all: list[AgentDefinition] = list(definitions)
        # Serializes concurrent set_thinking_effort calls. Today's writer
        # (md_writer.update_thinking_effort) is fully synchronous, so on
        # a single-threaded asyncio event loop two concurrent callers
        # would already run end-to-end serially without this lock — it's
        # forward-compat for a future async writer (aiofiles, threaded
        # fsync). Keep it cheap; reintroduce a real interleaving test if
        # the writer ever gains an ``await``.
        self._write_lock = asyncio.Lock()

        for d in self._all:
            self._index(d)

        # Multi-router detection runs after indexing so duplicate-id
        # /slash/display_name errors (which also fire if two routers
        # share those fields) take precedence — operators see the more
        # actionable "duplicate slash" message before the role error.
        # Zero-router is NOT rejected here: the "exactly one router"
        # invariant applies to the production load path
        # (:meth:`from_agents_dir` appends the built-in router
        # unconditionally; :meth:`router` raises at lookup time on a
        # zero-router registry), but in-memory test fixtures that
        # don't exercise routing should be allowed to omit it. A
        # multi-router list, by contrast, is always a wiring bug
        # (only the built-in singleton should declare role="router";
        # a user-defined ``agents/*.md`` accidentally setting it would
        # land here too).
        routers = [d for d in self._all if d.role == "router"]
        if len(routers) > 1:
            ids = [r.agent_id for r in routers]
            raise ValueError(
                f"AgentRegistry has multiple router agents {ids!r}; "
                f"exactly one router is allowed (only the built-in "
                f"router declared via build_router_definition() should "
                f"set role='router' — check user-authored agents/*.md "
                f"for an accidental role: router frontmatter field)"
            )

    def _index(self, definition: AgentDefinition) -> None:
        """Insert ``definition`` into all three indexes, rejecting duplicates."""
        if definition.agent_id in self._by_id:
            raise ValueError(f"duplicate agent_id: {definition.agent_id!r}")
        if definition.slash in self._by_slash:
            raise ValueError(f"duplicate slash: {definition.slash!r}")
        if definition.display_name in self._by_display_name:
            raise ValueError(f"duplicate display_name: {definition.display_name!r}")
        self._by_id[definition.agent_id] = definition
        self._by_slash[definition.slash] = definition
        self._by_display_name[definition.display_name] = definition

    def _replace(self, old: AgentDefinition, new: AgentDefinition) -> None:
        """Swap an existing entry. Keys must match; only mutable fields change.

        The asserts guard an internal invariant rather than user input —
        today's only caller is ``set_thinking_effort`` and the writer
        beneath it can't change ``agent_id``, ``slash``, or
        ``display_name``. If you add another caller, audit whether its
        write path can mutate any of those keys before reusing
        ``_replace`` — under ``python -O`` these asserts vanish.
        """
        assert old.agent_id == new.agent_id, "agent_id is immutable"
        assert old.slash == new.slash, "slash is immutable"
        assert old.display_name == new.display_name, "display_name is immutable"
        self._by_id[new.agent_id] = new
        self._by_slash[new.slash] = new
        self._by_display_name[new.display_name] = new
        idx = self._all.index(old)
        self._all[idx] = new

    @classmethod
    def from_agents_dir(cls, path: Path) -> Self:
        """Load an :class:`AgentRegistry` from a directory of agent ``.md`` files.

        Delegates parsing to
        :func:`calfkit_organization.agents.loader.load_agents_dir` and adds
        the cross-agent duplicate-detection of :meth:`__init__`.

        The built-in router definition is appended automatically — every
        registry instance loaded from disk includes the singleton router
        without any user-side opt-in. A user-defined ``agents/_router.md``
        file would collide with the built-in's ``agent_id`` (or its
        reserved ``slash``/``display_name``) and the duplicate-detection
        in :meth:`_index` would raise at construction time.
        """
        definitions = list(load_agents_dir(path))
        definitions.append(build_router_definition())
        return cls(definitions)

    def router(self) -> AgentDefinition:
        """Return the singleton router :class:`AgentDefinition`.

        :meth:`from_agents_dir` appends the built-in router on every
        load, so any registry loaded from disk in production carries
        exactly one. Test fixtures that build the registry directly
        without a router will get a :class:`ValueError` from this
        accessor — the failure is intentional and indicates the
        registry was constructed without the router that production
        always has.

        The multi-router case can't be reached here: it raises in
        :meth:`__init__`'s indexing-time validation. The zero-router
        case CAN be reached (the constructor permits it for test
        fixtures); it raises lazily here, on first lookup.

        Raises:
            ValueError: if the registry has no router agent. Operators
                running production paths see this only on a wiring
                regression (e.g., a refactor of
                :meth:`from_agents_dir` that drops the router append).
        """
        for d in self._all:
            if d.role == "router":
                return d
        raise ValueError(
            "AgentRegistry has zero router agents; the registry was "
            "constructed without one (production paths go through "
            "AgentRegistry.from_agents_dir which appends "
            "build_router_definition() automatically)"
        )

    def by_id(self, agent_id: str) -> AgentDefinition | None:
        return self._by_id.get(agent_id)

    def by_slash(self, slash: str) -> AgentDefinition | None:
        return self._by_slash.get(slash)

    def by_display_name(self, name: str) -> AgentDefinition | None:
        return self._by_display_name.get(name)

    def all(self) -> Sequence[AgentDefinition]:
        return tuple(self._all)

    async def set_thinking_effort(
        self, agent_id: str, value: ThinkingEffort
    ) -> AgentDefinition:
        """Rewrite ``thinking_effort`` in the agent's ``.md`` and swap the in-memory copy.

        The returned :class:`AgentDefinition` is the freshly-parsed
        post-write entry. Returned (rather than ``None``) so a caller
        holding the old reference can swap atomically without a second
        ``by_id`` lookup. The same instance is now in all three indexes.

        Raises:
            KeyError: ``agent_id`` is not in the registry.
            ValueError: the registered definition has no ``source_path``
                (in-memory construction without a real file), or the
                existing ``.md`` fails validation, or the rewrite would
                produce an invalid definition.
            FileNotFoundError: the ``.md`` file is missing on disk.
            OSError: a filesystem error during the tmp write or atomic
                rename. Post-rename parent-dir fsync failures are
                swallowed and logged at warning level — see
                :mod:`calfkit_organization.agents.md_writer`.
        """
        async with self._write_lock:
            existing = self._by_id.get(agent_id)
            if existing is None:
                raise KeyError(agent_id)
            if existing.source_path is None:
                raise ValueError(
                    f"agent {agent_id!r} has no source_path; cannot rewrite frontmatter"
                )
            new_definition = update_thinking_effort(existing.source_path, value)
            self._replace(existing, new_definition)
            return new_definition
