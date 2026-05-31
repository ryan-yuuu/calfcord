"""Unit tests for the wire schema (WireMessage, WireAuthor)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from calfkit_organization.bridge.wire import WireAuthor, WireMessage


def _make_author(**overrides) -> WireAuthor:
    defaults = dict(
        discord_user_id=1,
        display_name="Alice",
        is_bot=False,
        is_webhook=False,
    )
    return WireAuthor(**(defaults | overrides))


def _make_message(**overrides) -> WireMessage:
    defaults = dict(
        event_id="abc123",
        kind="message",
        message_id=100,
        channel_id=200,
        guild_id=300,
        content="hi",
        author=_make_author(),
        created_at=datetime.now(UTC),
    )
    return WireMessage(**(defaults | overrides))


class TestRoundTrip:
    def test_message_kind_round_trip(self) -> None:
        original = _make_message()
        dumped = original.model_dump(mode="json")
        restored = WireMessage.model_validate(dumped)
        assert restored == original

    def test_slash_kind_round_trip(self) -> None:
        original = _make_message(kind="slash", slash_target="scheduler")
        dumped = original.model_dump(mode="json")
        restored = WireMessage.model_validate(dumped)
        assert restored == original

    def test_author_round_trip(self) -> None:
        author = _make_author(
            is_webhook=True,
            webhook_id=999,
            agent_id="scheduler",
            display_name="Aksel (Scheduler)",
        )
        dumped = author.model_dump(mode="json")
        restored = WireAuthor.model_validate(dumped)
        assert restored == author


class TestValidators:
    def test_slash_kind_requires_slash_target(self) -> None:
        with pytest.raises(ValidationError, match="slash_target is required"):
            _make_message(kind="slash", slash_target=None)

    def test_message_kind_forbids_slash_target(self) -> None:
        with pytest.raises(ValidationError, match="slash_target must be None"):
            _make_message(kind="message", slash_target="scheduler")

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_message(kind="reaction")  # type: ignore[arg-type]


class TestFrozen:
    def test_wire_message_is_frozen(self) -> None:
        msg = _make_message()
        with pytest.raises(ValidationError):
            msg.content = "mutated"  # type: ignore[misc]

    def test_wire_author_is_frozen(self) -> None:
        author = _make_author()
        with pytest.raises(ValidationError):
            author.display_name = "mutated"  # type: ignore[misc]


class TestThreadId:
    """``thread_id`` resolves the originating thread, or None for top-level."""

    def test_none_when_no_source_channel(self) -> None:
        assert _make_message().thread_id is None

    def test_none_when_source_equals_channel(self) -> None:
        # A top-level message: the normalizer sets source == channel.
        msg = _make_message(channel_id=200, source_channel_id=200)
        assert msg.thread_id is None

    def test_returns_source_when_it_differs_from_channel(self) -> None:
        # A thread message: parent (channel_id) and thread (source) differ.
        msg = _make_message(channel_id=200, source_channel_id=555)
        assert msg.thread_id == 555
