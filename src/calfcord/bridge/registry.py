"""Static agent roster, indexed for the bridge's lookups.

The registry is the single source of truth for which agents exist and
what display name to attribute a persona-webhook message to. It is
populated dynamically from ``agent.state`` events published by agent
processes (see PR 3 cutover); prior to that, the bridge read
``agents/*.md`` directly.

The agent definitions themselves originate either from in-process code
(the built-in router, :func:`build_router_definition`) or from agent
processes that announce themselves over Kafka via the control plane.
This module owns the *index* the bridge uses (O(1) lookups by id and
display name, plus rejection of duplicate ``display_name`` across
agents — ``agent_id`` uniqueness implies slash uniqueness since the
slash is always ``/<agent_id>``) and three mutators:

* :meth:`upsert_from_state_event` — projection of an ``agent.state``
  event into the registry. Returns ``True`` on first-seen, ``False`` on
  re-announce.
* :meth:`remove` — projection of an ``agent.state`` departure event.
  Returns ``True`` if the agent was present and removed.
* :meth:`apply_local_thinking_effort_override` — optimistic in-memory
  mutation used by ``/thinking-effort`` after publishing the control
  command. The eventual post-apply state event re-upserts with the
  confirmed value.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Self

from calfcord.agents.definition import AgentDefinition, ThinkingEffort
from calfcord.agents.loader import load_agents_dir

logger = logging.getLogger(__name__)


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
        self._by_display_name: dict[str, AgentDefinition] = {}
        self._all: list[AgentDefinition] = list(definitions)

        for d in self._all:
            self._index(d)

    def _index(self, definition: AgentDefinition) -> None:
        """Insert ``definition`` into both indexes, rejecting duplicates."""
        if definition.agent_id in self._by_id:
            raise ValueError(f"duplicate agent_id: {definition.agent_id!r}")
        if definition.display_name in self._by_display_name:
            raise ValueError(f"duplicate display_name: {definition.display_name!r}")
        self._by_id[definition.agent_id] = definition
        self._by_display_name[definition.display_name] = definition

    def _unindex(self, definition: AgentDefinition) -> None:
        """Remove ``definition`` from both indexes.

        Caller is responsible for also removing it from ``self._all``
        (because the order of operations differs between :meth:`remove`
        and the key-field-change branch of
        :meth:`upsert_from_state_event`).
        """
        self._by_id.pop(definition.agent_id, None)
        self._by_display_name.pop(definition.display_name, None)

    def _replace(self, old: AgentDefinition, new: AgentDefinition) -> None:
        """Swap an existing entry. Keys must match; only mutable fields change.

        The asserts guard an internal invariant rather than user input.
        Callers must have already verified that ``agent_id`` and
        ``display_name`` are unchanged — :meth:`upsert_from_state_event`
        handles display-name changes via remove-then-insert rather than
        calling here, and :meth:`apply_local_thinking_effort_override`
        only mutates ``thinking_effort``. Under ``python -O`` these
        asserts vanish; the soft-handle path lives in
        :meth:`upsert_from_state_event`.
        """
        assert old.agent_id == new.agent_id, "agent_id is immutable"
        assert old.display_name == new.display_name, "display_name is immutable"
        self._by_id[new.agent_id] = new
        self._by_display_name[new.display_name] = new
        idx = self._all.index(old)
        self._all[idx] = new

    @classmethod
    def from_agents_dir(cls, path: Path) -> Self:
        """Load an :class:`AgentRegistry` from a directory of agent ``.md`` files.

        Delegates parsing to
        :func:`calfcord.agents.loader.load_agents_dir` and adds
        the cross-agent duplicate-detection of :meth:`__init__`.

        The built-in router definition is appended automatically — every
        registry instance loaded from disk includes the singleton router
        without any user-side opt-in. A user-defined ``agents/_router.md``
        file would collide with the built-in's ``agent_id`` (or its
        reserved ``display_name``) and the duplicate-detection in
        :meth:`_index` would raise at construction time.

        Used in test fixtures and any non-bridge code paths that still
        load definitions from disk. The bridge itself (PR 3 onwards)
        constructs the registry with only the router and fills the rest
        from state events.
        """
        definitions = list(load_agents_dir(path))
        return cls(definitions)

    def by_id(self, agent_id: str) -> AgentDefinition | None:
        return self._by_id.get(agent_id)

    def by_display_name(self, name: str) -> AgentDefinition | None:
        return self._by_display_name.get(name)

    def all(self) -> Sequence[AgentDefinition]:
        return tuple(self._all)

    def upsert_from_state_event(self, definition: AgentDefinition) -> bool:
        """Insert or update from a projected state event. Returns True if first-seen.

        Router protection: state events whose ``agent_id`` matches an
        existing router entry are rejected with a warning. The router is
        built locally on the bridge (:func:`build_router_definition`)
        and is never published over the wire by an agent process; an
        incoming event with the router's id indicates a misconfigured
        deployment.

        Key-field changes (``display_name``) for an existing
        ``agent_id`` are handled by remove-then-insert rather than
        :meth:`_replace` (whose asserts treat that as immutable). In
        practice agents don't rename mid-run, but defending here avoids
        a hard crash on a misconfigured deployment. If the new
        ``display_name`` collides with a *different* agent already in
        the registry, the old indexes are restored and the
        :class:`ValueError` propagates so the caller (state consumer)
        can log and skip.
        """
        incoming_id = definition.agent_id
        existing = self._by_id.get(incoming_id)
        if existing is None:
            self._index(definition)
            self._all.append(definition)
            return True
        # display_name changes need full re-index, not _replace.
        if existing.display_name != definition.display_name:
            self._unindex(existing)
            try:
                self._index(definition)
            except ValueError:
                # New display_name collides with a DIFFERENT agent already
                # in the registry. Restore the old definition's indexes to
                # keep the registry consistent, and re-raise so the caller
                # (state consumer) logs and skips.
                self._index(existing)
                raise
            idx = self._all.index(existing)
            self._all[idx] = definition
            return False
        self._replace(existing, definition)
        return False

    def remove(self, agent_id: str) -> bool:
        """Remove ``agent_id`` from all indexes. Returns True if removed.

        Router protection: removal requests for the router's
        ``agent_id`` are rejected with a warning. Routers are locally
        built and not subject to departure events. Returns False for
        unknown agents (idempotent).
        """
        existing = self._by_id.get(agent_id)
        if existing is None:
            return False
        self._unindex(existing)
        self._all.remove(existing)
        return True

    def apply_local_thinking_effort_override(self, agent_id: str, value: ThinkingEffort) -> AgentDefinition | None:
        """Optimistically update an agent's in-memory ``thinking_effort``.

        Returns the new (replaced) :class:`AgentDefinition`, or ``None``
        if ``agent_id`` is not in the registry. The bridge's slash
        handler calls this after publishing the
        :class:`SetThinkingEffortOp` to the agent's control topic; the
        agent's eventual post-apply state event will re-upsert with the
        (now-confirmed) value. Pure in-memory operation — no disk I/O,
        no awaits.
        """
        existing = self._by_id.get(agent_id)
        if existing is None:
            return None
        new_def = existing.model_copy(update={"thinking_effort": value})
        self._replace(existing, new_def)
        return new_def
