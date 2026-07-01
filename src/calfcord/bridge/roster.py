"""Mesh-backed bridge roster — the caller-side view of which agents are online.

Replaces the deleted ``AgentRegistry`` (which projected the ``agent.state``
control-plane topic) with a snapshot of calfkit's public ``client.mesh``
(the ``calf.agents`` plane). The mesh is **online-only** (staleness-filtered),
so the roster is the bridge's source of truth for "which agents can I reach
right now".

R-A2 (fail-fast). The ``@mention`` path treats the mesh as authoritative:

* mesh available, the mentioned agent online → route to it;
* mesh available, none of the mentioned agents online → user-facing
  "no agent online";
* mesh unavailable (reader ``establishing`` / ``open_failed`` / ``reader_dead``)
  → user-facing "roster unavailable" — we do **not** fail open. ``reader_dead``
  is permanent for the ``Client``'s lifetime (there is no ``Mesh.reset``), so a
  dead reader is logged at ERROR and the bridge keeps answering "unavailable"
  until it is restarted.

:meth:`online` returns ``None`` until the first successful :meth:`refresh`
(a cold roster is "unknown", not "empty") and whenever the last refresh hit a
:class:`MeshUnavailableError`, so the (synchronous) ``@mention`` resolution can
distinguish "nobody online" (an empty ``frozenset``) from "can't tell"
(``None`` → fail-fast).
"""

from __future__ import annotations

import logging

from calfkit.client import Client
from calfkit.exceptions import MeshUnavailableError

logger = logging.getLogger(__name__)

# Reasons a refresh can't recover from on the next read (unlike ``establishing``
# / ``open_failed``, which a later refresh may clear) — alert loudly so an
# operator restarts the bridge.
_PERMANENT_REASONS = frozenset({"reader_dead"})


class MeshRoster:
    """A short-lived snapshot of ``client.mesh.get_agents()`` for the sync
    ``@mention`` path.

    :meth:`refresh` is awaited (once per turn, or on a small loop) to keep the
    snapshot current; the resolution that reads it is synchronous.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._online: frozenset[str] | None = None
        self._reason: str | None = None

    async def refresh(self) -> None:
        """Refresh the cached online-agent snapshot from the mesh.

        On success :meth:`online` returns the live name set (possibly empty). On
        :class:`MeshUnavailableError` the snapshot becomes ``None`` and the
        reason is recorded; a permanent reason (``reader_dead``) is logged at
        ERROR so the degraded state is operator-visible.
        """
        try:
            agents = await self._client.mesh.get_agents()
        except MeshUnavailableError as exc:
            self._online = None
            self._reason = exc.reason
            permanent = exc.reason in _PERMANENT_REASONS
            logger.log(
                logging.ERROR if permanent else logging.WARNING,
                "agent roster unavailable (mesh reason=%s); @mentions answered "
                "'roster unavailable' until it recovers%s",
                exc.reason,
                " — reader_dead is permanent for this process; RESTART the bridge"
                if permanent
                else "",
            )
            return
        # ``AgentInfo.name`` is the canonical agent name (== node_id for agents).
        self._online = frozenset(info.name for info in agents.values())
        self._reason = None

    def online(self) -> frozenset[str] | None:
        """The currently-online agent names, or ``None`` when the roster is
        unknown (before the first refresh, or the last refresh found the mesh
        unavailable). ``None`` is the fail-fast signal; an empty ``frozenset``
        means "the mesh is healthy and nobody is online"."""
        return self._online

    def is_online(self, name: str) -> bool:
        """Whether ``name`` is in the last-known online set. ``False`` when the
        roster is unknown/unavailable — the bridge gates on :meth:`online` being
        ``None`` first, so this never has to invent an answer."""
        return self._online is not None and name in self._online

    @property
    def unavailable_reason(self) -> str | None:
        """The :class:`MeshUnavailableError` reason from the last refresh, or
        ``None`` when the roster is available."""
        return self._reason
