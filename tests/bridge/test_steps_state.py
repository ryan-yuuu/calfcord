"""Unit tests for ``StepsState`` (bounded LRU) and ``StepsEntry``
(per-correlation transcript cursor + thread cache)."""

from __future__ import annotations

import logging

import pytest

from calfkit_organization.bridge.steps_state import StepsEntry, StepsState
from calfkit_organization.discord.persona import Persona


def _entry(thread_id: int | None = None, history_cursor: int = 0) -> StepsEntry:
    return StepsEntry(
        parent_channel_id=10,
        parent_message_id=20,
        persona=Persona(name="agent", avatar_url=None),
        thread_id=thread_id,
        history_cursor=history_cursor,
    )


class TestBasic:
    def test_put_then_get_returns_entry(self) -> None:
        s = StepsState()
        e = _entry()
        s.put("c1", e)
        assert s.get("c1") is e

    def test_get_unknown_returns_none(self) -> None:
        assert StepsState().get("missing") is None

    def test_get_is_non_popping(self) -> None:
        s = StepsState()
        e = _entry()
        s.put("c1", e)
        assert s.get("c1") is e
        assert s.get("c1") is e

    def test_pop_and_mark_completed_removes(self) -> None:
        s = StepsState()
        e = _entry()
        s.put("c1", e)
        assert s.pop_and_mark_completed("c1") is e
        assert s.get("c1") is None

    def test_pop_and_mark_completed_marks_completion(self) -> None:
        """Subsequent hops for the same correlation_id are reported completed."""
        s = StepsState()
        s.put("c1", _entry())
        assert not s.is_completed("c1")
        s.pop_and_mark_completed("c1")
        assert s.is_completed("c1")

    def test_pop_and_mark_completed_marks_even_without_entry(self) -> None:
        """A terminal hop on a correlation that never had an active entry
        (pure-text reply with no intermediates) still records completion
        so a retry of that correlation doesn't seed a fresh transcript."""
        s = StepsState()
        assert s.pop_and_mark_completed("never-seen") is None
        assert s.is_completed("never-seen")

    def test_is_completed_unknown_returns_false(self) -> None:
        assert not StepsState().is_completed("never-seen")

    def test_len_tracks_size(self) -> None:
        s = StepsState()
        assert len(s) == 0
        s.put("a", _entry())
        s.put("b", _entry())
        assert len(s) == 2

    def test_invalid_capacity_rejected(self) -> None:
        with pytest.raises(ValueError):
            StepsState(capacity=0)
        with pytest.raises(ValueError):
            StepsState(capacity=-1)
        with pytest.raises(ValueError):
            StepsState(completed_capacity=0)


class TestCompletedSet:
    def test_completed_capacity_evicts_oldest(self) -> None:
        """Completion records are bounded by ``completed_capacity`` and
        evict oldest-first."""
        s = StepsState(completed_capacity=2)
        s.pop_and_mark_completed("a")
        s.pop_and_mark_completed("b")
        s.pop_and_mark_completed("c")  # evicts "a"
        assert not s.is_completed("a")
        assert s.is_completed("b")
        assert s.is_completed("c")

    def test_is_completed_updates_recency(self) -> None:
        """Touching a completion record refreshes its recency so it
        doesn't get pushed out by unrelated terminal hops."""
        s = StepsState(completed_capacity=2)
        s.pop_and_mark_completed("a")
        s.pop_and_mark_completed("b")
        s.is_completed("a")  # touch
        s.pop_and_mark_completed("c")
        assert s.is_completed("a")
        assert not s.is_completed("b")
        assert s.is_completed("c")


class TestLRU:
    def test_eviction_at_capacity(self) -> None:
        s = StepsState(capacity=2)
        s.put("a", _entry())
        s.put("b", _entry())
        s.put("c", _entry())  # evicts "a"
        assert s.get("a") is None
        assert s.get("b") is not None
        assert s.get("c") is not None

    def test_get_updates_recency(self) -> None:
        s = StepsState(capacity=2)
        s.put("a", _entry())
        s.put("b", _entry())
        s.get("a")  # touch
        s.put("c", _entry())
        assert s.get("a") is not None
        assert s.get("b") is None
        assert s.get("c") is not None

    def test_put_overwrites_and_refreshes_recency(self) -> None:
        s = StepsState(capacity=2)
        first = _entry()
        second = _entry()
        s.put("a", first)
        s.put("b", _entry())
        s.put("a", second)  # overwrite
        assert s.get("a") is second
        # Recency: "a" moved to end → next eviction takes "b".
        s.put("c", _entry())
        assert s.get("b") is None
        assert s.get("a") is second

    def test_eviction_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        s = StepsState(capacity=1)
        s.put("a", _entry())
        with caplog.at_level(logging.WARNING):
            s.put("b", _entry())
        assert any(
            "steps_state evicted" in r.message and "correlation_id=a" in r.message
            for r in caplog.records
        )


class TestEntry:
    def test_default_thread_id_and_cursor(self) -> None:
        e = StepsEntry(
            parent_channel_id=1,
            parent_message_id=2,
            persona=Persona(name="x", avatar_url=None),
        )
        assert e.thread_id is None
        assert e.history_cursor == 0

    def test_entry_is_mutable(self) -> None:
        """thread_id and history_cursor advance across hops."""
        e = _entry()
        e.thread_id = 999
        e.history_cursor = 5
        assert e.thread_id == 999
        assert e.history_cursor == 5
