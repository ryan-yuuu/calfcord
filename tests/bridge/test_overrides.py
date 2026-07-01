"""Unit tests for the per-agent thinking-effort override store (C11/R-A1/D-8)."""

from __future__ import annotations

import pytest

from calfcord.bridge.overrides import EffortOverrides


class _FakeStore:
    """Records writes and serves an in-memory ``agent_overrides`` table."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})
        self.set_calls: list[tuple[str, str]] = []
        self.clear_calls: list[str] = []

    async def all_agent_overrides(self) -> dict[str, str]:
        return dict(self.data)

    async def set_agent_override(self, agent_id: str, effort: str) -> None:
        self.data[agent_id] = effort
        self.set_calls.append((agent_id, effort))

    async def clear_agent_override(self, agent_id: str) -> None:
        self.data.pop(agent_id, None)
        self.clear_calls.append(agent_id)


async def test_hydrate_loads_persisted_overrides() -> None:
    store = _FakeStore({"scribe": "high", "conan": "low"})
    overrides = EffortOverrides(store)  # type: ignore[arg-type]
    assert overrides.effort_for("scribe") is None  # not yet hydrated
    await overrides.hydrate()
    assert overrides.effort_for("scribe") == "high"
    assert overrides.effort_for("conan") == "low"


async def test_effort_for_unset_is_none() -> None:
    overrides = EffortOverrides(_FakeStore())  # type: ignore[arg-type]
    await overrides.hydrate()
    assert overrides.effort_for("ghost") is None


async def test_set_writes_store_and_map() -> None:
    store = _FakeStore()
    overrides = EffortOverrides(store)  # type: ignore[arg-type]
    await overrides.hydrate()
    await overrides.set("scribe", "max")
    assert store.set_calls == [("scribe", "max")]
    assert store.data["scribe"] == "max"
    assert overrides.effort_for("scribe") == "max"  # reflected immediately, no re-hydrate


async def test_clear_removes_from_store_and_map() -> None:
    store = _FakeStore({"scribe": "high"})
    overrides = EffortOverrides(store)  # type: ignore[arg-type]
    await overrides.hydrate()
    await overrides.clear("scribe")
    assert store.clear_calls == ["scribe"]
    assert "scribe" not in store.data
    assert overrides.effort_for("scribe") is None


async def test_hydrate_replaces_map_with_current_table() -> None:
    store = _FakeStore({"scribe": "high"})
    overrides = EffortOverrides(store)  # type: ignore[arg-type]
    await overrides.hydrate()
    store.data = {"conan": "low"}  # table changed out-of-band
    await overrides.hydrate()
    assert overrides.effort_for("scribe") is None
    assert overrides.effort_for("conan") == "low"


async def test_hydrate_degrades_to_empty_on_store_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A store read failure at boot must not raise (that would crash all Discord
    routing to restore non-essential overrides); it degrades to an empty map."""

    class _BoomStore(_FakeStore):
        async def all_agent_overrides(self) -> dict[str, str]:
            raise RuntimeError("db locked")

    overrides = EffortOverrides(_BoomStore())  # type: ignore[arg-type]
    with caplog.at_level("ERROR"):
        await overrides.hydrate()  # must not raise
    assert overrides.effort_for("anyone") is None
    assert any("hydrate" in r.message for r in caplog.records)
