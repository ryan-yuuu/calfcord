"""Unit tests for ``PendingWires`` (bounded LRU) and ``PendingEntry``
(value type carrying wire + retry context)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from calfkit_organization.bridge.pending_wires import (
    PendingEntry,
    PendingWires,
    make_pending_entry,
)
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


def _entry(event_id: str = "evt-1") -> PendingEntry:
    """Build a minimal PendingEntry (retry_attempt=0, no history)."""
    return make_pending_entry(_wire(event_id))


class TestBasic:
    def test_put_then_get_returns_entry(self) -> None:
        pw = PendingWires()
        e = _entry()
        pw.put(e.wire.event_id, e)
        assert pw.get(e.wire.event_id) is e

    def test_get_unknown_returns_none(self) -> None:
        assert PendingWires().get("missing") is None

    def test_get_is_non_popping(self) -> None:
        """Multiple agents reply for one correlation_id — get must not consume."""
        pw = PendingWires()
        e = _entry()
        pw.put(e.wire.event_id, e)
        assert pw.get(e.wire.event_id) is e
        assert pw.get(e.wire.event_id) is e  # second reader still sees it.

    def test_pop_removes(self) -> None:
        pw = PendingWires()
        e = _entry()
        pw.put(e.wire.event_id, e)
        assert pw.pop(e.wire.event_id) is e
        assert pw.get(e.wire.event_id) is None

    def test_pop_unknown_returns_none(self) -> None:
        assert PendingWires().pop("missing") is None

    def test_len_tracks_size(self) -> None:
        pw = PendingWires()
        assert len(pw) == 0
        pw.put("a", _entry("a"))
        pw.put("b", _entry("b"))
        assert len(pw) == 2

    def test_invalid_capacity_rejected(self) -> None:
        with pytest.raises(ValueError):
            PendingWires(capacity=0)
        with pytest.raises(ValueError):
            PendingWires(capacity=-1)


class TestLRU:
    def test_eviction_at_capacity(self) -> None:
        pw = PendingWires(capacity=2)
        pw.put("a", _entry("a"))
        pw.put("b", _entry("b"))
        pw.put("c", _entry("c"))  # evicts "a"
        assert pw.get("a") is None
        assert pw.get("b") is not None
        assert pw.get("c") is not None

    def test_get_updates_recency(self) -> None:
        """A recently-read entry should not be the next to evict."""
        pw = PendingWires(capacity=2)
        pw.put("a", _entry("a"))
        pw.put("b", _entry("b"))
        # Touch "a" — now "b" is the oldest.
        pw.get("a")
        pw.put("c", _entry("c"))
        assert pw.get("a") is not None  # survived because we touched it
        assert pw.get("b") is None  # evicted
        assert pw.get("c") is not None

    def test_repeat_put_overwrites_and_refreshes_recency(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A redelivered put replaces the entry (last-writer-wins) and refreshes recency.

        Discord redeliveries can carry edited content under the same
        ``message_id``; binding the consumer's reply to the stale wire
        would misrepresent what the agent was replying to.
        """
        pw = PendingWires(capacity=2)
        first = _entry("a")
        second = _entry("a")  # distinct instance, same id
        pw.put("a", first)
        pw.put("b", _entry("b"))
        with caplog.at_level(logging.INFO):
            pw.put("a", second)
        assert len(pw) == 2
        assert pw.get("a") is second  # last-writer-wins
        assert any(
            "overwriting entry for correlation_id=a" in r.message
            for r in caplog.records
        )
        # Recency: "a" moved to end → next eviction takes "b".
        pw.put("c", _entry("c"))
        assert pw.get("b") is None
        assert pw.get("a") is second
        assert pw.get("c") is not None

    def test_eviction_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        pw = PendingWires(capacity=1)
        pw.put("a", _entry("a"))
        with caplog.at_level(logging.WARNING):
            pw.put("b", _entry("b"))
        assert any(
            "pending_wires evicted" in r.message and "correlation_id=a" in r.message
            for r in caplog.records
        )


class TestPendingEntry:
    """``PendingEntry`` is a frozen snapshot of original-invocation
    context. The retry counter lives in :class:`PendingWires`'s
    side-table (``_retry_counts``); see :class:`TestRetryCounter`
    below for that surface."""

    def test_defaults_are_empty_and_none(self) -> None:
        e = make_pending_entry(_wire())
        assert e.message_history == ()
        assert e.temp_instructions is None
        assert e.model_settings is None

    def test_fields_are_set_at_construction(self) -> None:
        history = (
            ModelRequest(parts=[UserPromptPart(content="hi")]),
            ModelResponse(parts=[TextPart(content="hey")]),
        )
        model_settings = {"thinking_effort": "high"}
        e = PendingEntry(
            wire=_wire(),
            message_history=history,
            temp_instructions="peer roster here",
            model_settings=model_settings,
        )
        assert e.message_history is history
        assert e.temp_instructions == "peer roster here"
        assert e.model_settings is model_settings

    def test_is_frozen(self) -> None:
        """``PendingEntry`` is ``frozen=True``; attribute assignment raises."""
        from dataclasses import FrozenInstanceError

        e = make_pending_entry(_wire())
        with pytest.raises(FrozenInstanceError):
            e.temp_instructions = "mutated"  # type: ignore[misc]

    def test_make_pending_entry_helper(self) -> None:
        history = (ModelRequest(parts=[UserPromptPart(content="x")]),)
        e = make_pending_entry(
            _wire(),
            message_history=history,
            temp_instructions="ti",
            model_settings={"k": "v"},
        )
        assert e.message_history is history
        assert e.temp_instructions == "ti"
        assert e.model_settings == {"k": "v"}


class TestRetryCounter:
    """``increment_retry`` is the outbox's atomic claim mechanism for
    triggering a retry attempt. The counter lives on the
    :class:`PendingWires` side-table (not on the frozen
    :class:`PendingEntry`)."""

    def test_initial_count_is_zero(self) -> None:
        pw = PendingWires()
        pw.put("a", _entry("a"))
        assert pw.get_retry_count("a") == 0

    def test_unknown_count_is_zero(self) -> None:
        """A missing key reads as 0 so callers can ask 'is this a first
        attempt?' without distinguishing 'evicted' from 'no retries yet'."""
        assert PendingWires().get_retry_count("missing") == 0

    def test_increment_returns_new_count(self) -> None:
        pw = PendingWires()
        pw.put("a", _entry("a"))
        assert pw.increment_retry("a") == 1
        assert pw.increment_retry("a") == 2
        assert pw.increment_retry("a") == 3

    def test_get_retry_count_reflects_increments(self) -> None:
        pw = PendingWires()
        pw.put("a", _entry("a"))
        pw.increment_retry("a")
        pw.increment_retry("a")
        assert pw.get_retry_count("a") == 2

    def test_increment_unknown_returns_none(self) -> None:
        pw = PendingWires()
        assert pw.increment_retry("missing") is None

    def test_increment_after_pop_returns_none(self) -> None:
        pw = PendingWires()
        pw.put("a", _entry("a"))
        pw.pop("a")
        assert pw.increment_retry("a") is None

    def test_pop_clears_retry_count(self) -> None:
        """Popping an entry must also clear its retry counter to keep
        the side-table in sync with ``_entries``."""
        pw = PendingWires()
        pw.put("a", _entry("a"))
        pw.increment_retry("a")
        pw.pop("a")
        assert pw.get_retry_count("a") == 0

    def test_eviction_clears_retry_count(self) -> None:
        """An LRU eviction also clears the counter side-table."""
        pw = PendingWires(capacity=1)
        pw.put("a", _entry("a"))
        pw.increment_retry("a")
        assert pw.get_retry_count("a") == 1
        pw.put("b", _entry("b"))  # evicts "a"
        assert pw.get_retry_count("a") == 0

    def test_redelivery_resets_retry_count(self) -> None:
        """``put`` over an existing key resets the counter — a redelivery
        is a fresh invocation, not a continuation of the prior wire."""
        pw = PendingWires()
        pw.put("a", _entry("a"))
        pw.increment_retry("a")
        pw.increment_retry("a")
        pw.put("a", _entry("a"))  # redelivery
        assert pw.get_retry_count("a") == 0

    def test_increment_after_eviction_returns_none(self) -> None:
        pw = PendingWires(capacity=1)
        pw.put("a", _entry("a"))
        pw.put("b", _entry("b"))  # evicts "a"
        assert pw.increment_retry("a") is None

    def test_increment_refreshes_recency(self) -> None:
        """An in-progress retry sequence shouldn't be evicted mid-flight."""
        pw = PendingWires(capacity=2)
        pw.put("a", _entry("a"))
        pw.put("b", _entry("b"))
        # Touch "a" via increment — now "b" is the oldest.
        pw.increment_retry("a")
        pw.put("c", _entry("c"))
        assert pw.get("a") is not None
        assert pw.get("b") is None  # evicted
