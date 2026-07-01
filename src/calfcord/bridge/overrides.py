"""Per-agent thinking-effort overrides for the bridge (C11 / R-A1 / D-8).

The bridge applies a per-agent ``ModelSettings`` override (set via the
``/thinking-effort`` slash command) on each call. After the registry deletion the
bridge is **provider-blind** — it cannot know an agent's model provider — so it
stores only the effort *tier* and emits a provider-blind **union**
``model_settings`` via
:func:`~calfcord.agents.thinking.build_model_settings_union` (R-A1).

This class is the override store: an in-memory ``{agent → tier}`` map kept in sync
with the persisted SQLite ``agent_overrides`` table (D-8). The map is hydrated from
the table at startup (overrides survive a bridge restart) and is the hot-path read
the :class:`~calfcord.bridge.mention_handler.MentionHandler` consults per turn; the
``/thinking-effort`` command writes through both the table and the map.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from calfcord.bridge.transcripts import TranscriptStoreLike

if TYPE_CHECKING:
    from calfcord.agents.definition import ThinkingEffort

logger = logging.getLogger(__name__)


class EffortOverrides:
    """In-memory ``{agent_id → effort tier}`` map backed by the SQLite store."""

    def __init__(self, store: TranscriptStoreLike) -> None:
        self._store = store
        self._map: dict[str, str] = {}

    async def hydrate(self) -> None:
        """Load all persisted overrides into the in-memory map (call at startup).

        Idempotent: replaces the in-memory map with the table's current contents, so
        overrides set before the last restart are honored on the first turn after it.

        Best-effort: a store read failure must NOT crash bridge boot (which would
        take down all Discord routing) just to restore non-essential thinking-effort
        overrides — mirroring the sibling startup step ``_prune_on_startup``. On
        failure the map degrades to empty (no overrides) and the error is logged.
        """
        try:
            self._map = await self._store.all_agent_overrides()
        except Exception:
            logger.exception("failed to hydrate thinking-effort overrides at startup; continuing with none")
            self._map = {}

    def effort_for(self, agent_id: str) -> str | None:
        """Return the override tier for ``agent_id``, or ``None`` if unset.

        Hot-path read off the in-memory map (no DB hit). The returned tier is fed to
        :func:`~calfcord.agents.thinking.build_model_settings_union`, which tolerates
        an unknown tier by degrading to no override.
        """
        return self._map.get(agent_id)

    async def set(self, agent_id: str, effort: ThinkingEffort) -> None:
        """Persist and cache an override for ``agent_id`` (``/thinking-effort`` write).

        Strict in (a validated :data:`ThinkingEffort` tier — the slash command
        validates before calling), tolerant out (:meth:`effort_for` returns the
        opaque ``str`` the store holds, which a hand-edited DB could dirty)."""
        await self._store.set_agent_override(agent_id, effort)
        self._map[agent_id] = effort

    async def clear(self, agent_id: str) -> None:
        """Remove ``agent_id``'s override from the table and the map (idempotent)."""
        await self._store.clear_agent_override(agent_id)
        self._map.pop(agent_id, None)
