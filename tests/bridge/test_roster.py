"""Unit tests for the mesh-backed bridge roster (R-A2 fail-fast).

The real ``client.mesh`` opens its own ktables reader (needs real Kafka), so
these tests drive :class:`MeshRoster` through a ``FakeMesh`` — the only unit
path (real roster I/O is integration-tested on Tansu).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from calfkit import AgentInfo, MeshUnavailableError

from calfcord.bridge.roster import MeshRoster


class _FakeMesh:
    def __init__(self, *, online: dict[str, str | None] | None = None, error: Exception | None = None) -> None:
        self._error = error
        self._agents = {
            name: AgentInfo(name=name, description=desc, last_seen=datetime.now(UTC))
            for name, desc in (online or {}).items()
        }

    async def get_agents(self) -> Any:
        if self._error is not None:
            raise self._error
        return MappingProxyType(self._agents)


class _FakeClient:
    def __init__(self, mesh: _FakeMesh) -> None:
        self.mesh = mesh


def _roster(*, online: dict[str, str | None] | None = None, error: Exception | None = None) -> MeshRoster:
    return MeshRoster(_FakeClient(_FakeMesh(online=online, error=error)))


class TestMeshRoster:
    async def test_refresh_populates_online_names(self) -> None:
        roster = _roster(online={"scribe": "Notes.", "conan": "Research."})
        await roster.refresh()
        assert roster.online() == frozenset({"scribe", "conan"})
        assert roster.is_online("scribe") is True
        assert roster.is_online("ghost") is False

    async def test_empty_roster_is_empty_set_not_none(self) -> None:
        """An online-but-empty mesh is distinct from an unavailable one: empty
        set (route nothing) vs None (fail-fast 'roster unavailable')."""
        roster = _roster(online={})
        await roster.refresh()
        assert roster.online() == frozenset()
        assert roster.is_online("scribe") is False

    async def test_unavailable_mesh_yields_none_and_records_reason(self) -> None:
        roster = _roster(error=MeshUnavailableError("calf.agents not present yet", reason="open_failed"))
        await roster.refresh()
        assert roster.online() is None
        assert roster.unavailable_reason == "open_failed"
        # is_online is False (the bridge fail-fasts on online() is None, but a
        # stray is_online call must not crash on the unavailable snapshot).
        assert roster.is_online("scribe") is False

    async def test_reader_dead_is_recorded_as_unavailable(self) -> None:
        roster = _roster(error=MeshUnavailableError("reader died", reason="reader_dead"))
        await roster.refresh()
        assert roster.online() is None
        assert roster.unavailable_reason == "reader_dead"

    async def test_refresh_recovers_after_transient_unavailability(self) -> None:
        """A later successful refresh clears a prior unavailable state."""
        roster = _roster(online={"scribe": "Notes."})
        await roster.refresh()
        assert roster.online() == frozenset({"scribe"})
        # Swap to an unavailable mesh and refresh again.
        roster._client.mesh = _FakeMesh(error=MeshUnavailableError("x", reason="establishing"))  # type: ignore[attr-defined]
        await roster.refresh()
        assert roster.online() is None
        assert roster.unavailable_reason == "establishing"

    async def test_online_is_none_before_first_refresh(self) -> None:
        """Until the first refresh runs, the roster is 'unknown' (None), so the
        bridge fail-fasts rather than treating a cold roster as 'nobody online'."""
        roster = _roster(online={"scribe": "Notes."})
        assert roster.online() is None
