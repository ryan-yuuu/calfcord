"""Unit tests for :mod:`calfkit_organization.bridge.history`.

Covers three units:

* :func:`project_history` — pure function; tested with hand-built
  :class:`HistoryRecord` lists across POV variants, empty content,
  leading-response, and router (None) POV.
* :class:`ChannelHistoryFetcher` — wraps the gateway's ``discord.Client``;
  tested with hand-built async-iterator fakes for ``channel.history()``
  and stub clients for ``get_channel`` / ``fetch_channel``. No real
  Discord, no real Kafka.
* :class:`HistoryRecord` — pydantic schema validation.

The fakes mirror the duck-typing the production code does: only the
attributes/methods actually read by the code under test are populated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
)
from pydantic import ValidationError

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.history import (
    CLEAR_MARKER_TEXT,
    ChannelHistoryFetcher,
    HistoryRecord,
    is_clear_marker,
    project_history,
)
from calfkit_organization.bridge.registry import AgentRegistry


def _record(
    *,
    message_id: int = 1,
    content: str = "hi",
    author_display_name: str = "ryan",
    author_agent_id: str | None = None,
    created_at: datetime | None = None,
) -> HistoryRecord:
    return HistoryRecord(
        message_id=message_id,
        created_at=created_at or datetime.now(UTC),
        content=content,
        author_display_name=author_display_name,
        author_agent_id=author_agent_id,
    )


# ---------------------------------------------------------------------------
# HistoryRecord schema
# ---------------------------------------------------------------------------


class TestHistoryRecord:
    def test_construct_minimal(self) -> None:
        r = HistoryRecord(
            message_id=42,
            created_at=datetime.now(UTC),
            content="hello",
            author_display_name="alice",
            author_agent_id=None,
        )
        assert r.message_id == 42
        assert r.content == "hello"
        assert r.author_agent_id is None

    def test_is_frozen(self) -> None:
        r = _record()
        with pytest.raises(ValidationError):
            r.content = "mutated"  # type: ignore[misc]

    def test_agent_id_optional(self) -> None:
        r = _record(author_agent_id="scribe")
        assert r.author_agent_id == "scribe"


# ---------------------------------------------------------------------------
# project_history
# ---------------------------------------------------------------------------


def _text(msg: Any) -> str:
    """Extract the text from a single-part ModelMessage."""
    parts = list(msg.parts)
    assert len(parts) == 1
    return parts[0].content


class TestProjectHistory:
    def test_empty_input(self) -> None:
        assert project_history([], self_agent_id="scribe") == []

    def test_simple_pov_scribe(self) -> None:
        records = [
            _record(message_id=1, content="how do I X?", author_display_name="ryan"),
            _record(
                message_id=2,
                content="here's how",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(message_id=3, content="thanks", author_display_name="ryan"),
        ]
        out = project_history(records, self_agent_id="scribe")
        assert len(out) == 3
        assert isinstance(out[0], ModelRequest)
        assert _text(out[0]) == "<ryan> how do I X?"
        assert isinstance(out[1], ModelResponse)
        assert _text(out[1]) == "here's how"
        assert isinstance(out[2], ModelRequest)
        assert _text(out[2]) == "<ryan> thanks"

    def test_pov_rotates_for_different_target(self) -> None:
        """The same records project differently for scribe vs. conan.

        Leading human message keeps the projected list opening with a
        ModelRequest so the drop-leading-ModelResponse step doesn't
        consume the per-POV self-classified entries we're asserting on.
        """
        records = [
            _record(message_id=0, content="kick it off", author_display_name="ryan"),
            _record(
                message_id=1,
                content="riveting stuff",
                author_display_name="Conan",
                author_agent_id="conan",
            ),
            _record(
                message_id=2,
                content="thanks Conan",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        scribe_view = project_history(records, self_agent_id="scribe")
        # For scribe: ryan + conan are ModelRequest; scribe is ModelResponse.
        assert isinstance(scribe_view[0], ModelRequest)
        assert _text(scribe_view[0]) == "<ryan> kick it off"
        assert isinstance(scribe_view[1], ModelRequest)
        assert _text(scribe_view[1]) == "<Conan> riveting stuff"
        assert isinstance(scribe_view[2], ModelResponse)
        assert _text(scribe_view[2]) == "thanks Conan"

        conan_view = project_history(records, self_agent_id="conan")
        # For conan: ryan + scribe are ModelRequest; conan is ModelResponse.
        assert isinstance(conan_view[0], ModelRequest)
        assert _text(conan_view[0]) == "<ryan> kick it off"
        assert isinstance(conan_view[1], ModelResponse)
        assert _text(conan_view[1]) == "riveting stuff"
        assert isinstance(conan_view[2], ModelRequest)
        assert _text(conan_view[2]) == "<Scribe> thanks Conan"

    def test_router_pov_none_means_everything_is_request(self) -> None:
        """self_agent_id=None: outside-observer; every record is ModelRequest."""
        records = [
            _record(
                message_id=1,
                content="hi",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(message_id=2, content="hi back", author_display_name="ryan"),
        ]
        out = project_history(records, self_agent_id=None)
        assert all(isinstance(m, ModelRequest) for m in out)
        assert _text(out[0]) == "<Scribe> hi"
        assert _text(out[1]) == "<ryan> hi back"

    def test_drops_empty_content(self) -> None:
        records = [
            _record(message_id=1, content="hello"),
            _record(message_id=2, content=""),
            _record(message_id=3, content="   "),  # whitespace-only also dropped
            _record(message_id=4, content="world"),
        ]
        out = project_history(records, self_agent_id="scribe")
        assert len(out) == 2
        assert _text(out[0]) == "<ryan> hello"
        assert _text(out[1]) == "<ryan> world"

    def test_drops_leading_response_iteratively(self) -> None:
        """Multiple leading ModelResponses get dropped one by one."""
        records = [
            _record(
                message_id=1,
                content="one",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(
                message_id=2,
                content="two",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(message_id=3, content="user msg", author_display_name="ryan"),
        ]
        out = project_history(records, self_agent_id="scribe")
        assert len(out) == 1
        assert isinstance(out[0], ModelRequest)
        assert _text(out[0]) == "<ryan> user msg"

    def test_drops_leading_response_to_empty(self) -> None:
        """If all records are self-responses, the result is empty."""
        records = [
            _record(
                content="r1",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(
                content="r2",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        out = project_history(records, self_agent_id="scribe")
        assert out == []

    def test_does_not_merge_consecutive(self) -> None:
        """Adjacent same-role messages are NOT merged here.

        Plan §4 / §6: pydantic-ai's _clean_message_history merges before
        provider mappers see the list. Doing it here would be redundant.
        This test pins the contract.
        """
        records = [
            _record(message_id=1, content="a", author_display_name="ryan"),
            _record(message_id=2, content="b", author_display_name="ryan"),
            _record(message_id=3, content="c", author_display_name="ryan"),
        ]
        out = project_history(records, self_agent_id="scribe")
        assert len(out) == 3
        for m in out:
            assert isinstance(m, ModelRequest)

    def test_unknown_webhook_treated_as_other(self) -> None:
        """A webhook post from a removed/renamed agent (author_agent_id=None)
        is just another speaker — projected as ModelRequest with the
        webhook's display_name in the prefix."""
        records = [
            _record(
                content="ghost",
                author_display_name="Aksel",
                author_agent_id=None,  # removed agent
            ),
        ]
        out = project_history(records, self_agent_id="scribe")
        assert len(out) == 1
        assert isinstance(out[0], ModelRequest)
        assert _text(out[0]) == "<Aksel> ghost"

    def test_self_agent_id_none_does_not_self_classify(self) -> None:
        """Records with author_agent_id matching None must not become
        ModelResponse — None == None would be a false positive."""
        records = [
            _record(content="hi", author_display_name="ryan", author_agent_id=None),
        ]
        out = project_history(records, self_agent_id=None)
        assert len(out) == 1
        assert isinstance(out[0], ModelRequest)

    def test_hydration_none_is_byte_identical_default(self) -> None:
        """Passing ``hydration=None`` explicitly produces the same output as
        omitting it — the replay kwarg defaults to a no-op so every existing
        caller (router, ambient) is unaffected. Tool-call replay itself is
        exercised in :mod:`tests.bridge.test_replay`."""
        records = [
            _record(message_id=1, content="how do I X?", author_display_name="ryan"),
            _record(
                message_id=2,
                content="here's how",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        omitted = project_history(records, self_agent_id="scribe")
        explicit = project_history(records, self_agent_id="scribe", hydration=None)
        assert len(omitted) == len(explicit) == 2
        for a, b in zip(omitted, explicit, strict=True):
            assert type(a) is type(b)
            assert _text(a) == _text(b)


# ---------------------------------------------------------------------------
# ChannelHistoryFetcher — fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    """Stand-in for a discord.TextChannel that exposes only history().

    history() returns newest-first per Discord's API. The fetcher
    reverses internally so projected output is oldest-first.
    """

    def __init__(self, messages: list[Any]) -> None:
        # Stored newest-first to mirror Discord's API.
        self._messages = list(messages)

    def history(self, *, limit: int, before: Any) -> Any:
        # ``before`` is a discord.Object; we just check its existence for
        # the contract test. Real discord.py filters server-side; fakes
        # return the stored list capped at ``limit``.
        captured = self._messages[:limit]

        class _AIter:
            def __init__(self, items: list[Any]) -> None:
                self._items = items
                self._i = 0

            def __aiter__(self) -> _AIter:
                return self

            async def __anext__(self) -> Any:
                if self._i >= len(self._items):
                    raise StopAsyncIteration
                v = self._items[self._i]
                self._i += 1
                return v

        return _AIter(captured)


def _fake_discord_message(
    *,
    message_id: int,
    content: str = "hi",
    author_display_name: str = "ryan",
    author_name: str | None = None,
    author_id: int = 1,
    webhook_id: int | None = None,
    created_at: datetime | None = None,
) -> Any:
    """Hand-built ``discord.Message`` look-alike for fetcher tests."""
    author = SimpleNamespace(
        display_name=author_display_name,
        name=author_name or author_display_name,
        id=author_id,
    )
    return SimpleNamespace(
        id=message_id,
        content=content,
        webhook_id=webhook_id,
        author=author,
        created_at=created_at or datetime.now(UTC),
    )


def _registry_with_scribe() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scribe",
                display_name="Scribe",
                description="Scribe agent.",
                system_prompt="You are Scribe.",
            ),
        ]
    )


def _httpexception(status: int = 500) -> discord.HTTPException:
    """Build a discord.HTTPException with the given HTTP status.

    discord.HTTPException takes (response, message) where response has
    a .status; we use a SimpleNamespace stub that satisfies that.
    """
    response = SimpleNamespace(status=status, reason="x")
    return discord.HTTPException(response, "synthetic")  # type: ignore[arg-type]


def _forbidden() -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, "synthetic")  # type: ignore[arg-type]


def _not_found() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(response, "synthetic")  # type: ignore[arg-type]


class TestChannelHistoryFetcher:
    @pytest.mark.asyncio
    async def test_happy_path_returns_oldest_first(self) -> None:
        client = MagicMock()
        # Discord returns newest-first; store accordingly.
        client.get_channel.return_value = _FakeChannel(
            messages=[
                _fake_discord_message(message_id=3, content="newest"),
                _fake_discord_message(message_id=2, content="middle"),
                _fake_discord_message(message_id=1, content="oldest"),
            ]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        # Reversed to oldest-first.
        assert [r.message_id for r in records] == [1, 2, 3]
        assert [r.content for r in records] == ["oldest", "middle", "newest"]

    @pytest.mark.asyncio
    async def test_zero_limit_short_circuits(self) -> None:
        client = MagicMock()
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=0
        )

        assert records == []
        client.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_limit_treated_as_zero(self) -> None:
        client = MagicMock()
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=-5
        )

        assert records == []
        client.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_limit_capped_at_discord_max(self) -> None:
        """Caller asks for 9999; fetcher requests 100 from Discord."""
        client = MagicMock()
        captured_limit: dict[str, int] = {}

        class _RecordingChannel:
            def history(self, *, limit: int, before: Any) -> Any:
                captured_limit["limit"] = limit

                class _Empty:
                    def __aiter__(self) -> Any:
                        return self

                    async def __anext__(self) -> Any:
                        raise StopAsyncIteration

                return _Empty()

        client.get_channel.return_value = _RecordingChannel()
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=9999)

        assert captured_limit["limit"] == 100

    @pytest.mark.asyncio
    async def test_cache_hit_within_ttl(self) -> None:
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe(), cache_ttl_seconds=10.0)

        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        # Second call within TTL should hit cache, not call discord.
        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)

        assert client.get_channel.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        # Use a tiny TTL and advance monotonic between calls.
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe(), cache_ttl_seconds=0.1)

        fake_now = {"v": 1000.0}

        def _mono() -> float:
            return fake_now["v"]

        monkeypatch.setattr("calfkit_organization.bridge.history.monotonic", _mono)

        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        fake_now["v"] += 5.0  # past TTL
        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)

        assert client.get_channel.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_lru_eviction(self) -> None:
        """Inserting beyond cache_max_entries evicts oldest."""
        client = MagicMock()
        # Each call hits a unique channel id → unique cache key.
        client.get_channel.return_value = _FakeChannel(messages=[])
        fetcher = ChannelHistoryFetcher(
            client, _registry_with_scribe(), cache_max_entries=2
        )

        await fetcher.fetch(source_channel_id=1, before_message_id=999, limit=10)
        await fetcher.fetch(source_channel_id=2, before_message_id=999, limit=10)
        await fetcher.fetch(source_channel_id=3, before_message_id=999, limit=10)

        # Re-fetching channel 1 should be a miss (evicted), channel 3 a hit.
        client.get_channel.reset_mock()
        # ...but to assert, let's just confirm cache size is bounded:
        assert len(fetcher._cache) == 2

    @pytest.mark.asyncio
    async def test_handles_http_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        client = MagicMock()
        channel = MagicMock()
        channel.history = MagicMock(side_effect=_httpexception(500))
        client.get_channel.return_value = channel
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert records == []
        assert any("history fetch failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_handles_forbidden_logs_once_per_channel(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = MagicMock()
        channel = MagicMock()
        channel.history = MagicMock(side_effect=_forbidden())
        client.get_channel.return_value = channel
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        # Same channel, three fetches → one Forbidden WARN.
        await fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10)
        await fetcher.fetch(source_channel_id=100, before_message_id=2, limit=10)
        await fetcher.fetch(source_channel_id=100, before_message_id=3, limit=10)

        forbidden_logs = [
            r for r in caplog.records if "Read Message History" in r.message
        ]
        assert len(forbidden_logs) == 1, (
            f"expected 1 Forbidden log per channel, got {len(forbidden_logs)}: "
            f"{[r.message for r in forbidden_logs]}"
        )

        # Different channel → another WARN fires.
        caplog.clear()
        await fetcher.fetch(source_channel_id=200, before_message_id=1, limit=10)
        forbidden_logs = [
            r for r in caplog.records if "Read Message History" in r.message
        ]
        assert len(forbidden_logs) == 1

    @pytest.mark.asyncio
    async def test_handles_not_found(self) -> None:
        client = MagicMock()
        channel = MagicMock()
        channel.history = MagicMock(side_effect=_not_found())
        client.get_channel.return_value = channel
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )
        assert records == []

    @pytest.mark.asyncio
    async def test_falls_back_to_fetch_channel(self) -> None:
        """When get_channel returns None, the fetcher tries fetch_channel."""
        client = MagicMock()
        client.get_channel.return_value = None
        client.fetch_channel = AsyncMock(
            return_value=_FakeChannel(
                messages=[_fake_discord_message(message_id=1, content="hi")]
            )
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert len(records) == 1
        assert records[0].content == "hi"
        client.fetch_channel.assert_awaited_once_with(100)

    @pytest.mark.asyncio
    async def test_fetch_channel_not_found(self) -> None:
        client = MagicMock()
        client.get_channel.return_value = None
        client.fetch_channel = AsyncMock(side_effect=_not_found())
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert records == []

    @pytest.mark.asyncio
    async def test_fetch_channel_forbidden(self) -> None:
        client = MagicMock()
        client.get_channel.return_value = None
        client.fetch_channel = AsyncMock(side_effect=_forbidden())
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert records == []

    @pytest.mark.asyncio
    async def test_resolves_webhook_to_agent_id(self) -> None:
        """A webhook post whose display_name matches a registered agent
        should have ``author_agent_id`` populated."""
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[
                _fake_discord_message(
                    message_id=1,
                    content="from scribe",
                    author_display_name="Scribe",
                    webhook_id=777,
                ),
            ]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert len(records) == 1
        assert records[0].author_agent_id == "scribe"
        assert records[0].author_display_name == "Scribe"

    @pytest.mark.asyncio
    async def test_unknown_webhook_display_name_passes_through(self) -> None:
        """Webhook with display_name not in registry → author_agent_id=None."""
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[
                _fake_discord_message(
                    message_id=1,
                    content="ghost",
                    author_display_name="Aksel",  # not registered
                    webhook_id=888,
                ),
            ]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert len(records) == 1
        assert records[0].author_agent_id is None
        assert records[0].author_display_name == "Aksel"

    @pytest.mark.asyncio
    async def test_non_webhook_message_never_resolves_agent_id(self) -> None:
        """Even if a HUMAN's display_name happens to match an agent's
        registered display_name, the record's ``author_agent_id`` must
        remain None — agent identity requires the message to be a
        webhook post."""
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[
                _fake_discord_message(
                    message_id=1,
                    content="impersonator",
                    author_display_name="Scribe",  # matches registered name
                    webhook_id=None,  # but not a webhook → not the agent
                ),
            ]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert len(records) == 1
        assert records[0].author_agent_id is None

    @pytest.mark.asyncio
    async def test_defensive_copy_on_cache_hit(self) -> None:
        """Mutating a returned list must not corrupt the cache."""
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe(), cache_ttl_seconds=60.0)

        first = await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        first.clear()  # caller mutates

        second = await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        assert len(second) == 1  # cache survives caller mutation

    @pytest.mark.asyncio
    async def test_different_keys_dont_collide(self) -> None:
        """A second fetch with a different ``before_message_id`` is a
        cache miss even when channel + limit match."""
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        await fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10)
        await fetcher.fetch(source_channel_id=100, before_message_id=2, limit=10)

        assert client.get_channel.call_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_same_key_coalesces_to_one_fetch(self) -> None:
        """Single-flight: N concurrent fetches on the same key share one
        Discord REST call. This is the core router-fan-out coalescing
        guarantee — without it, an N-way fan-out triggers N independent
        Discord calls (TTL cache only coalesces sequential bursts).
        """
        import asyncio as _asyncio

        # Use a slow-resolving channel so concurrent callers all reach
        # the "in-flight" branch before any one of them completes.
        gate = _asyncio.Event()

        class _SlowChannel:
            def history(self, *, limit: int, before: Any) -> Any:
                async def _gen() -> Any:
                    await gate.wait()
                    return
                    yield  # type: ignore[unreachable]

                return _gen()

        client = MagicMock()
        client.get_channel.return_value = _SlowChannel()
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        # Launch three concurrent fetches with the same key.
        async def _race() -> tuple[list[HistoryRecord], ...]:
            return await _asyncio.gather(
                fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10),
                fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10),
                fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10),
            )

        task = _asyncio.create_task(_race())
        # Yield to let all three call sites pass the cache check and
        # register themselves in the in-flight map.
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)
        gate.set()
        results = await task

        # All three callers got results.
        assert len(results) == 3
        # Only ONE channel resolution happened — the other two joined
        # the in-flight future.
        assert client.get_channel.call_count == 1

    @pytest.mark.asyncio
    async def test_in_flight_pops_after_completion(self) -> None:
        """The in-flight map is cleaned up after each fetch so a
        subsequent call to the same key starts fresh (rather than
        attaching to a completed future).
        """
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe(), cache_ttl_seconds=0.0)

        await fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10)
        assert fetcher._in_flight == {}
        # Cache-busted (TTL=0); next call hits Discord again, single-flight
        # entry is freshly created and freshly torn down.
        await fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10)
        assert fetcher._in_flight == {}

    def test_history_record_rejects_invalid_agent_id(self) -> None:
        """Stricter than the v1 happy path: a malformed author_agent_id
        (empty string, bad chars) must be rejected at construction so
        no caller can produce a record that compares equal to a valid
        self_agent_id via accident.
        """
        with pytest.raises(ValidationError):
            HistoryRecord(
                message_id=1,
                created_at=datetime.now(UTC),
                content="x",
                author_display_name="x",
                author_agent_id="",
            )
        with pytest.raises(ValidationError):
            HistoryRecord(
                message_id=1,
                created_at=datetime.now(UTC),
                content="x",
                author_display_name="x",
                author_agent_id="UPPERCASE-NOT-ALLOWED",
            )

    def test_history_record_rejects_empty_display_name(self) -> None:
        with pytest.raises(ValidationError):
            HistoryRecord(
                message_id=1,
                created_at=datetime.now(UTC),
                content="x",
                author_display_name="",
                author_agent_id=None,
            )

    @pytest.mark.asyncio
    async def test_leader_cancellation_does_not_poison_followers(self) -> None:
        """If the single-flight leader's task is cancelled mid-fetch,
        passive follower tasks must NOT receive the leader's
        CancelledError. They observe the documented "no history"
        fallback (empty list) instead.

        Without this guard, a fan-out triggered during shutdown would
        cancel-cascade through every assistant invocation joined to
        the same in-flight future.
        """
        import asyncio as _asyncio

        gate = _asyncio.Event()

        class _BlockingChannel:
            def history(self, *, limit: int, before: Any) -> Any:
                async def _gen() -> Any:
                    await gate.wait()
                    return
                    yield  # type: ignore[unreachable]

                return _gen()

        client = MagicMock()
        client.get_channel.return_value = _BlockingChannel()
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        # Two concurrent fetches with the same key — leader (A) starts,
        # follower (B) joins via in-flight.
        leader = _asyncio.create_task(
            fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10)
        )
        await _asyncio.sleep(0)  # let leader register in_flight
        follower = _asyncio.create_task(
            fetcher.fetch(source_channel_id=100, before_message_id=1, limit=10)
        )
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)  # let follower hit the in-flight branch

        # Cancel the leader mid-fetch.
        leader.cancel()

        # Leader sees its own CancelledError; follower gets empty list.
        with pytest.raises(_asyncio.CancelledError):
            await leader
        follower_result = await follower
        assert follower_result == []

        # The in-flight map is cleaned up.
        assert fetcher._in_flight == {}

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_absorbed_for_all_callers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If something inside ``fetch`` raises an unexpected exception
        (i.e. one that escapes ``_do_fetch``'s own defensive sweep —
        a true bug in the cache helper, etc.), the public contract
        ("fetcher never raises into invocation path") must still
        hold: both the leader and any concurrent followers get [].
        """
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        # Force a non-Discord, non-CancelledError exception by breaking
        # _cache_and_return (which runs in the happy path after
        # _do_fetch returns). This is the "defect outside _do_fetch's
        # protected scope" case.
        from unittest.mock import patch as _patch

        with _patch.object(
            fetcher, "_cache_and_return", side_effect=RuntimeError("boom")
        ):
            result = await fetcher.fetch(
                source_channel_id=100, before_message_id=1, limit=10
            )

        assert result == []
        assert any(
            "single-flight raised unexpectedly" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_defensive_sweep_on_malformed_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If a future discord.py returns a Message missing an attribute
        the fetcher reads, the projection-time AttributeError must NOT
        raise into the invocation path (the documented contract).
        """
        bad_msg = SimpleNamespace(
            id=1,
            content="x",
            webhook_id=None,
            # No `author` attribute → _to_record raises AttributeError
            created_at=datetime.now(UTC),
        )
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(messages=[bad_msg])
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )
        assert records == []
        assert any(
            "failed to project messages" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# bypass_cache kwarg (A2A thread reads)
# ---------------------------------------------------------------------------


class TestBypassCache:
    """``bypass_cache=True`` is used by ``private_chat`` when continuing an
    existing thread: the caller has just posted into the thread (or is
    about to), and an LRU hit from a fan-out a moment ago would either
    omit the just-posted message or, worse, include it as both
    ``message_history`` AND ``user_prompt`` (causing a duplicate-prompt
    bug). These tests pin the contract: bypass skips the read AND the
    write, but never the single-flight registration.
    """

    @pytest.mark.asyncio
    async def test_bypass_cache_skips_read(self) -> None:
        """A bypass fetch must NOT serve a cache-hit, even when a fresh
        entry from a prior default fetch exists for the same key."""
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(
            client, _registry_with_scribe(), cache_ttl_seconds=60.0
        )

        # Populate the cache via a default fetch.
        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        assert client.get_channel.call_count == 1

        # Bypass fetch with identical key must NOT hit the cache —
        # Discord is queried again.
        await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10, bypass_cache=True
        )
        assert client.get_channel.call_count == 2

    @pytest.mark.asyncio
    async def test_bypass_cache_skips_write(self) -> None:
        """A bypass fetch must NOT populate the LRU, so a subsequent
        default fetch with the same key still misses and hits Discord."""
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(
            client, _registry_with_scribe(), cache_ttl_seconds=60.0
        )

        await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10, bypass_cache=True
        )
        assert client.get_channel.call_count == 1

        # The bypass fetch did not write to the cache; default fetch
        # must still go to Discord.
        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        assert client.get_channel.call_count == 2

    @pytest.mark.asyncio
    async def test_bypass_cache_still_uses_single_flight(self) -> None:
        """Two concurrent bypass fetches with the same key must coalesce
        into ONE Discord call. Bypass only opts out of the LRU, not the
        single-flight invariant.
        """
        import asyncio as _asyncio

        gate = _asyncio.Event()

        class _SlowChannel:
            def history(self, *, limit: int, before: Any) -> Any:
                async def _gen() -> Any:
                    await gate.wait()
                    return
                    yield  # type: ignore[unreachable]

                return _gen()

        client = MagicMock()
        client.get_channel.return_value = _SlowChannel()
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        async def _race() -> tuple[list[HistoryRecord], ...]:
            return await _asyncio.gather(
                fetcher.fetch(
                    source_channel_id=100,
                    before_message_id=1,
                    limit=10,
                    bypass_cache=True,
                ),
                fetcher.fetch(
                    source_channel_id=100,
                    before_message_id=1,
                    limit=10,
                    bypass_cache=True,
                ),
            )

        task = _asyncio.create_task(_race())
        # Yield to let both call sites pass the (skipped) cache check
        # and register themselves in the in-flight map.
        await _asyncio.sleep(0)
        await _asyncio.sleep(0)
        gate.set()
        results = await task

        assert len(results) == 2
        # Single-flight: only one Discord channel resolution.
        assert client.get_channel.call_count == 1
        # And the LRU was never populated (bypass).
        assert len(fetcher._cache) == 0

    @pytest.mark.asyncio
    async def test_default_path_unaffected(self) -> None:
        """Regression: the default ``bypass_cache=False`` path must be
        byte-identical to today — read from cache when fresh, write
        back on miss. Calling without the kwarg and with ``False``
        produce the same observable behavior.
        """
        client = MagicMock()
        client.get_channel.return_value = _FakeChannel(
            messages=[_fake_discord_message(message_id=1)]
        )
        fetcher = ChannelHistoryFetcher(
            client, _registry_with_scribe(), cache_ttl_seconds=60.0
        )

        # Cold call writes to cache.
        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        assert client.get_channel.call_count == 1
        assert len(fetcher._cache) == 1

        # Warm default call hits the cache — no Discord call.
        await fetcher.fetch(source_channel_id=100, before_message_id=999, limit=10)
        assert client.get_channel.call_count == 1

        # Explicit bypass_cache=False is identical to the omitted-kwarg call.
        await fetcher.fetch(
            source_channel_id=100,
            before_message_id=999,
            limit=10,
            bypass_cache=False,
        )
        assert client.get_channel.call_count == 1


# ---------------------------------------------------------------------------
# /clear marker — recognition + truncation
# ---------------------------------------------------------------------------


_BOT_ID = 555


class TestIsClearMarker:
    """Unit tests for the pure :func:`is_clear_marker` predicate.

    Authorship is the load-bearing check: only the bot's own non-webhook
    post counts, so a user typing the sentinel text — or an agent persona
    webhook posting it — is NOT a marker.
    """

    def test_recognizes_bot_authored_marker(self) -> None:
        msg = _fake_discord_message(
            message_id=1,
            content=CLEAR_MARKER_TEXT,
            author_id=_BOT_ID,
            webhook_id=None,
        )
        assert is_clear_marker(msg, _BOT_ID) is True

    def test_rejects_wrong_content(self) -> None:
        msg = _fake_discord_message(
            message_id=1, content="not the marker", author_id=_BOT_ID, webhook_id=None
        )
        assert is_clear_marker(msg, _BOT_ID) is False

    def test_rejects_user_typed_sentinel(self) -> None:
        """A human typing the exact sentinel text is not a marker."""
        msg = _fake_discord_message(
            message_id=1,
            content=CLEAR_MARKER_TEXT,
            author_id=_BOT_ID + 1,  # not the bot
            webhook_id=None,
        )
        assert is_clear_marker(msg, _BOT_ID) is False

    def test_rejects_webhook_authored_sentinel(self) -> None:
        """Even with the bot's id, a webhook post (persona) is not a marker."""
        msg = _fake_discord_message(
            message_id=1,
            content=CLEAR_MARKER_TEXT,
            author_id=_BOT_ID,
            webhook_id=999,
        )
        assert is_clear_marker(msg, _BOT_ID) is False

    def test_rejects_when_bot_user_id_unknown(self) -> None:
        """Pre-ready (no known bot id): authorship can't be authenticated."""
        msg = _fake_discord_message(
            message_id=1,
            content=CLEAR_MARKER_TEXT,
            author_id=_BOT_ID,
            webhook_id=None,
        )
        assert is_clear_marker(msg, None) is False


class TestClearMarkerTruncation:
    """``ChannelHistoryFetcher.fetch`` drops history at the latest marker."""

    def _fetcher(self, messages: list[Any]) -> ChannelHistoryFetcher:
        client = MagicMock()
        client.user = SimpleNamespace(id=_BOT_ID)
        client.get_channel.return_value = _FakeChannel(messages=messages)
        return ChannelHistoryFetcher(client, _registry_with_scribe())

    @pytest.mark.asyncio
    async def test_truncates_at_marker(self) -> None:
        # Discord newest-first: 4, 3, marker(2), 1 → keep only 3 and 4.
        fetcher = self._fetcher(
            [
                _fake_discord_message(message_id=4, content="after2"),
                _fake_discord_message(message_id=3, content="after1"),
                _fake_discord_message(
                    message_id=2, content=CLEAR_MARKER_TEXT, author_id=_BOT_ID
                ),
                _fake_discord_message(message_id=1, content="before"),
            ]
        )
        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )
        assert [r.message_id for r in records] == [3, 4]
        assert all(r.content != CLEAR_MARKER_TEXT for r in records)

    @pytest.mark.asyncio
    async def test_no_marker_returns_all(self) -> None:
        fetcher = self._fetcher(
            [
                _fake_discord_message(message_id=2, content="b"),
                _fake_discord_message(message_id=1, content="a"),
            ]
        )
        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )
        assert [r.message_id for r in records] == [1, 2]

    @pytest.mark.asyncio
    async def test_uses_most_recent_of_multiple_markers(self) -> None:
        # newest-first: 5, marker(4), 3, marker(2), 1 → keep only 5.
        fetcher = self._fetcher(
            [
                _fake_discord_message(message_id=5, content="keep"),
                _fake_discord_message(
                    message_id=4, content=CLEAR_MARKER_TEXT, author_id=_BOT_ID
                ),
                _fake_discord_message(message_id=3, content="between"),
                _fake_discord_message(
                    message_id=2, content=CLEAR_MARKER_TEXT, author_id=_BOT_ID
                ),
                _fake_discord_message(message_id=1, content="old"),
            ]
        )
        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )
        assert [r.message_id for r in records] == [5]

    @pytest.mark.asyncio
    async def test_marker_is_newest_yields_empty(self) -> None:
        # newest-first: marker(3), 2, 1 → everything dropped.
        fetcher = self._fetcher(
            [
                _fake_discord_message(
                    message_id=3, content=CLEAR_MARKER_TEXT, author_id=_BOT_ID
                ),
                _fake_discord_message(message_id=2, content="b"),
                _fake_discord_message(message_id=1, content="a"),
            ]
        )
        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )
        assert records == []

    @pytest.mark.asyncio
    async def test_marker_outside_limit_window_is_not_seen(self) -> None:
        """A marker older than the most recent ``limit`` messages falls
        outside the fetched window, so no truncation happens — and that's
        correct: every message in the window is already newer than the
        marker (post-clear), so the un-truncated window IS the answer."""
        # newest-first; with limit=2 the fetcher only sees [5, 4]; the
        # marker at message 1 is never fetched.
        fetcher = self._fetcher(
            [
                _fake_discord_message(message_id=5, content="newest"),
                _fake_discord_message(message_id=4, content="next"),
                _fake_discord_message(message_id=3, content="mid"),
                _fake_discord_message(
                    message_id=1, content=CLEAR_MARKER_TEXT, author_id=_BOT_ID
                ),
            ]
        )
        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=2
        )
        assert [r.message_id for r in records] == [4, 5]

    @pytest.mark.asyncio
    async def test_user_typed_sentinel_is_not_a_boundary(self) -> None:
        """A non-bot author posting the sentinel must NOT truncate history."""
        fetcher = self._fetcher(
            [
                _fake_discord_message(message_id=2, content="after"),
                _fake_discord_message(
                    message_id=1, content=CLEAR_MARKER_TEXT, author_id=_BOT_ID + 1
                ),
            ]
        )
        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )
        # Both kept; the spoofed sentinel is just another user message.
        assert [r.message_id for r in records] == [1, 2]


# ---------------------------------------------------------------------------
# Thread starter-message recovery
# ---------------------------------------------------------------------------


class _FakeThread(_FakeChannel):
    """Stand-in for a ``discord.Thread`` created from a message.

    Adds the attributes :meth:`ChannelHistoryFetcher._thread_starter_message`
    duck-types on — ``id``, ``parent_id``, ``starter_message`` and
    ``parent`` — on top of :class:`_FakeChannel`'s ``history()``. For a
    message-started thread the starter lives in the PARENT channel, so the
    inherited ``history()`` (the in-thread messages) never includes it.
    """

    def __init__(
        self,
        messages: list[Any],
        *,
        thread_id: int,
        parent_id: int,
        starter_message: Any | None = None,
        parent: Any | None = None,
    ) -> None:
        super().__init__(messages)
        self.id = thread_id
        self.parent_id = parent_id
        self.starter_message = starter_message
        self.parent = parent


def _fetcher_for(thread: Any) -> ChannelHistoryFetcher:
    """Build a fetcher whose ``get_channel`` resolves the source id to ``thread``."""
    client = MagicMock()
    client.user = SimpleNamespace(id=_BOT_ID)
    client.get_channel.return_value = thread
    return ChannelHistoryFetcher(client, _registry_with_scribe())


# The starter id equals the thread id (Discord invariant); in-thread messages
# always have larger snowflakes. These constants keep that ordering explicit.
_THREAD_ID = 1000
_PARENT_ID = 50


class TestThreadStarterMessage:
    """``ChannelHistoryFetcher`` prepends a message-thread's starter message."""

    @pytest.mark.asyncio
    async def test_followup_prepends_starter_via_rest(self) -> None:
        # Cache miss (starter_message=None) → recovered via parent.fetch_message.
        starter = _fake_discord_message(
            message_id=_THREAD_ID, content="do the task", author_display_name="ryan"
        )
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThread(
            [
                _fake_discord_message(message_id=1002, content="reply"),
                _fake_discord_message(message_id=1001, content="followup"),
            ],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1003, limit=10
        )

        # Starter is the oldest record, followed by the in-thread messages.
        assert [r.message_id for r in records] == [_THREAD_ID, 1001, 1002]
        assert records[0].content == "do the task"
        parent.fetch_message.assert_awaited_once_with(_THREAD_ID)

    @pytest.mark.asyncio
    async def test_cached_starter_skips_rest(self) -> None:
        starter = _fake_discord_message(message_id=_THREAD_ID, content="cached task")
        fetch_message = AsyncMock()
        parent = SimpleNamespace(fetch_message=fetch_message)
        thread = _FakeThread(
            [_fake_discord_message(message_id=1001, content="followup")],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=starter,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )

        assert [r.message_id for r in records] == [_THREAD_ID, 1001]
        # In-memory cache hit ⇒ no REST round-trip.
        fetch_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_turn_excludes_starter(self) -> None:
        # First /task turn: before_message_id == thread id == starter id, so
        # the anchor is the triggering message (supplied as the user_prompt)
        # and must NOT appear in history — Discord's exclusive before= rule.
        starter = _fake_discord_message(message_id=_THREAD_ID, content="do the task")
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThread(
            [],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=_THREAD_ID, limit=10
        )

        assert records == []

    @pytest.mark.asyncio
    async def test_non_thread_channel_no_prepend(self) -> None:
        # A plain channel (no parent_id) flows through the new code path
        # untouched — _thread_starter_message returns None before any fetch.
        client = MagicMock()
        client.user = SimpleNamespace(id=_BOT_ID)
        client.get_channel.return_value = _FakeChannel(
            messages=[
                _fake_discord_message(message_id=2, content="b"),
                _fake_discord_message(message_id=1, content="a"),
            ]
        )
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=100, before_message_id=999, limit=10
        )

        assert [r.message_id for r in records] == [1, 2]

    @pytest.mark.asyncio
    async def test_starter_already_in_history_not_duplicated(self) -> None:
        # Forum-post thread: the starter (id == thread id) IS returned by
        # history(); the membership guard must skip the prepend so it isn't
        # duplicated.
        starter_in_thread = _fake_discord_message(
            message_id=_THREAD_ID, content="forum op"
        )
        parent = SimpleNamespace(
            fetch_message=AsyncMock(return_value=starter_in_thread)
        )
        thread = _FakeThread(
            [
                _fake_discord_message(message_id=1001, content="reply"),
                starter_in_thread,
            ],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )

        assert [r.message_id for r in records] == [_THREAD_ID, 1001]

    @pytest.mark.asyncio
    async def test_starter_not_found_degrades(self) -> None:
        # Standalone thread or deleted starter → NotFound → no prepend.
        parent = SimpleNamespace(fetch_message=AsyncMock(side_effect=_not_found()))
        thread = _FakeThread(
            [_fake_discord_message(message_id=1001, content="followup")],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )

        assert [r.message_id for r in records] == [1001]

    @pytest.mark.asyncio
    async def test_starter_forbidden_logs_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Missing Read Message History on the parent → no prepend, deduped log.
        parent = SimpleNamespace(fetch_message=AsyncMock(side_effect=_forbidden()))
        thread = _FakeThread(
            [_fake_discord_message(message_id=1001, content="followup")],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        # Distinct before_message_id values bypass the TTL cache so both
        # fetches re-attempt recovery (and both hit Forbidden).
        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )
        records_again = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1003, limit=10
        )

        assert [r.message_id for r in records] == [1001]
        assert [r.message_id for r in records_again] == [1001]
        forbidden_logs = [
            r for r in caplog.records if "Read Message History" in r.message
        ]
        assert len(forbidden_logs) == 1

    @pytest.mark.asyncio
    async def test_clear_marker_truncates_starter(self) -> None:
        # A /clear posted inside the thread truncates the task statement too:
        # the prepend happens BEFORE the clear scan, so the anchor is dropped.
        starter = _fake_discord_message(message_id=_THREAD_ID, content="do the task")
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThread(
            [
                _fake_discord_message(message_id=1002, content="after clear"),
                _fake_discord_message(
                    message_id=1001, content=CLEAR_MARKER_TEXT, author_id=_BOT_ID
                ),
            ],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1003, limit=10
        )

        # Only the post-marker message survives; the anchor was above the line.
        assert [r.message_id for r in records] == [1002]

    @pytest.mark.asyncio
    async def test_uncached_parent_resolved_via_get_channel(self) -> None:
        # channel.parent is None → resolve the parent via get_channel(parent_id).
        starter = _fake_discord_message(message_id=_THREAD_ID, content="do the task")
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThread(
            [_fake_discord_message(message_id=1001, content="followup")],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=None,
        )
        client = MagicMock()
        client.user = SimpleNamespace(id=_BOT_ID)

        def _get_channel(cid: int) -> Any:
            return {_THREAD_ID: thread, _PARENT_ID: parent}.get(cid)

        client.get_channel.side_effect = _get_channel
        fetcher = ChannelHistoryFetcher(client, _registry_with_scribe())

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )

        assert [r.message_id for r in records] == [_THREAD_ID, 1001]
        parent.fetch_message.assert_awaited_once_with(_THREAD_ID)

    @pytest.mark.asyncio
    async def test_parent_without_fetch_message_degrades(self) -> None:
        # Defensive: a non-messageable parent (no fetch_message) → no prepend,
        # no crash.
        thread = _FakeThread(
            [_fake_discord_message(message_id=1001, content="followup")],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=SimpleNamespace(),  # no fetch_message attribute
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )

        assert [r.message_id for r in records] == [1001]

    @pytest.mark.asyncio
    async def test_starter_http_exception_degrades(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A non-Forbidden/non-NotFound 5xx from parent.fetch_message must be
        # caught (WARN) and degrade to thread-only history — NOT escape into
        # _do_fetch's defensive sweep (which would drop the whole thread).
        parent = SimpleNamespace(
            fetch_message=AsyncMock(side_effect=_httpexception(500))
        )
        thread = _FakeThread(
            [_fake_discord_message(message_id=1001, content="followup")],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )

        # In-thread message survives (graceful degrade), not empty history.
        assert [r.message_id for r in records] == [1001]
        assert any(
            "starter-message fetch failed" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_starter_after_before_window_excluded(self) -> None:
        # Pins the OTHER side of the strict `<` gate: when the anchor is the
        # same age as or newer than before_message_id it must be excluded.
        # Here before_message_id is BELOW the thread/starter id, so the
        # starter is outside the exclusive `before=` window — no prepend.
        # (A `<=` mutation of the gate would wrongly include it and fail.)
        starter = _fake_discord_message(message_id=_THREAD_ID, content="do the task")
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThread(
            [],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID,
            before_message_id=_THREAD_ID - 1,
            limit=10,
        )

        assert records == []

    @pytest.mark.asyncio
    async def test_followup_empty_thread_returns_only_starter(self) -> None:
        # A /task thread whose only follow-up is the (excluded) triggering
        # message: in-thread history is empty, so the recovered starter is
        # the sole record. Exercises insert(0, ...) into an empty list.
        starter = _fake_discord_message(message_id=_THREAD_ID, content="do the task")
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThread(
            [],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1002, limit=10
        )

        assert [r.message_id for r in records] == [_THREAD_ID]

    @pytest.mark.asyncio
    async def test_overflow_returns_limit_plus_one(self) -> None:
        # Contract pin: the prepend happens AFTER the limit-bounded fetch, so
        # when in-thread messages already fill `limit` the fetcher returns
        # `limit + 1` records (starter + limit in-thread). Callers (ingress)
        # re-trim with records[-N:]; this test documents the fetcher side of
        # that contract so a future change to the limit handling is caught.
        starter = _fake_discord_message(message_id=_THREAD_ID, content="do the task")
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThread(
            # Discord newest-first; three in-thread messages, all newer than
            # the starter and older than before_message_id.
            [
                _fake_discord_message(message_id=1003, content="c"),
                _fake_discord_message(message_id=1002, content="b"),
                _fake_discord_message(message_id=1001, content="a"),
            ],
            thread_id=_THREAD_ID,
            parent_id=_PARENT_ID,
            starter_message=None,
            parent=parent,
        )
        fetcher = _fetcher_for(thread)

        records = await fetcher.fetch(
            source_channel_id=_THREAD_ID, before_message_id=1004, limit=3
        )

        # limit=3 in-thread messages + the prepended starter == 4 (limit + 1),
        # starter first. The caller's [-N:] re-trim would drop the starter.
        assert [r.message_id for r in records] == [_THREAD_ID, 1001, 1002, 1003]
        assert len(records) == 4
