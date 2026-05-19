"""Unit tests for the ``PendingWires`` bounded-LRU map."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.wire import WireAuthor, WireMessage


def _wire(event_id: str = "evt-1") -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind="message",
        slash_target=None,
        message_id=1,
        channel_id=2,
        guild_id=3,
        content="hi",
        author=WireAuthor(
            discord_user_id=4,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
        ),
        created_at=datetime.now(UTC),
    )


class TestBasic:
    def test_put_then_get_returns_wire(self) -> None:
        pw = PendingWires()
        w = _wire()
        pw.put(w.event_id, w)
        assert pw.get(w.event_id) is w

    def test_get_unknown_returns_none(self) -> None:
        assert PendingWires().get("missing") is None

    def test_get_is_non_popping(self) -> None:
        """Multiple agents reply for one correlation_id — get must not consume."""
        pw = PendingWires()
        w = _wire()
        pw.put(w.event_id, w)
        assert pw.get(w.event_id) is w
        assert pw.get(w.event_id) is w  # second reader still sees it.

    def test_pop_removes(self) -> None:
        pw = PendingWires()
        w = _wire()
        pw.put(w.event_id, w)
        assert pw.pop(w.event_id) is w
        assert pw.get(w.event_id) is None

    def test_pop_unknown_returns_none(self) -> None:
        assert PendingWires().pop("missing") is None

    def test_len_tracks_size(self) -> None:
        pw = PendingWires()
        assert len(pw) == 0
        pw.put("a", _wire("a"))
        pw.put("b", _wire("b"))
        assert len(pw) == 2

    def test_invalid_capacity_rejected(self) -> None:
        with pytest.raises(ValueError):
            PendingWires(capacity=0)
        with pytest.raises(ValueError):
            PendingWires(capacity=-1)


class TestLRU:
    def test_eviction_at_capacity(self) -> None:
        pw = PendingWires(capacity=2)
        pw.put("a", _wire("a"))
        pw.put("b", _wire("b"))
        pw.put("c", _wire("c"))  # evicts "a"
        assert pw.get("a") is None
        assert pw.get("b") is not None
        assert pw.get("c") is not None

    def test_get_updates_recency(self) -> None:
        """A recently-read entry should not be the next to evict."""
        pw = PendingWires(capacity=2)
        pw.put("a", _wire("a"))
        pw.put("b", _wire("b"))
        # Touch "a" — now "b" is the oldest.
        pw.get("a")
        pw.put("c", _wire("c"))
        assert pw.get("a") is not None  # survived because we touched it
        assert pw.get("b") is None  # evicted
        assert pw.get("c") is not None

    def test_repeat_put_overwrites_and_refreshes_recency(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A redelivered put replaces the wire (last-writer-wins) and refreshes recency.

        Discord redeliveries can carry edited content under the same
        ``message_id``; binding the consumer's reply to the stale wire
        would misrepresent what the agent was replying to.
        """
        pw = PendingWires(capacity=2)
        first = _wire("a")
        second = _wire("a")  # distinct instance, same id
        pw.put("a", first)
        pw.put("b", _wire("b"))
        with caplog.at_level(logging.INFO):
            pw.put("a", second)
        assert len(pw) == 2
        assert pw.get("a") is second  # last-writer-wins
        assert any(
            "overwriting wire for correlation_id=a" in r.message
            for r in caplog.records
        )
        # Recency: "a" moved to end → next eviction takes "b".
        pw.put("c", _wire("c"))
        assert pw.get("b") is None
        assert pw.get("a") is second
        assert pw.get("c") is not None

    def test_eviction_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        pw = PendingWires(capacity=1)
        pw.put("a", _wire("a"))
        with caplog.at_level(logging.WARNING):
            pw.put("b", _wire("b"))
        assert any(
            "pending_wires evicted" in r.message and "correlation_id=a" in r.message
            for r in caplog.records
        )
