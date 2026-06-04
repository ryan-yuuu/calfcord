"""Unit tests for the ``private_chat`` A2A tool.

Tests call the bare async function directly with a constructed
:class:`ToolContext`, bypassing calfkit's tool dispatch. The module-level
singletons are populated via ``monkeypatch.setattr`` per-test (so leak is
impossible across tests), and the phonebook arrives via ``ctx.deps`` —
mirroring the bridge ingress, which is the tool's only source of agent
identity.

The architecture under test is the **unified-channel + per-conversation
thread** model: every A2A invocation lives inside a Discord thread
under one shared ``private-a2a-chats`` channel. ``thread_id=None`` (default)
forks a fresh thread; passing an int continues an existing one with
projected history injected as ``message_history``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from calfkit.client import Client
from calfkit.models import ToolContext

from calfcord.agents.phonebook import PhonebookEntry, phonebook_to_deps
from calfcord.bridge.egress import A2AChannelResolver
from calfcord.bridge.history import HistoryRecord
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.tools.builtin import private_chat as pc

# Pattern for the success return surface: starts with a `<thread_id>NNN</thread_id>`
# tag, then a newline, then any (possibly empty, possibly multi-line) response.
_RETURN_TAG_PATTERN = re.compile(r"^<thread_id>(\d+)</thread_id>\n", re.DOTALL)


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    """Build a discord HTTPException-family instance without hitting the network.

    Mirrors :func:`tests.bridge.test_outbox._http_exc` — both modules need
    synthetic HTTPException instances; the duplicated helper is cheaper
    than a shared test-fixtures package for two callsites."""
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


def _wire(
    *,
    content: str = "hi",
    kind: str = "slash",
    slash_target: str | None = None,
    channel_id: int = 999,
) -> WireMessage:
    """Build a minimal WireMessage representing the inbound that triggered
    the calling agent. The WireMessage validator requires
    ``slash_target`` iff ``kind == "slash"`` — this helper enforces the
    invariant: ``slash_target`` defaults to ``"alice"`` for slash kind and
    ``None`` for message kind."""
    if kind == "slash" and slash_target is None:
        slash_target = "alice"
    if kind == "message":
        slash_target = None
    return WireMessage(
        event_id="evt-1",
        kind=kind,  # type: ignore[arg-type]
        slash_target=slash_target,
        message_id=42,
        channel_id=channel_id,
        guild_id=10,
        content=content,
        author=WireAuthor(
            discord_user_id=1,
            display_name="ryan",
            is_bot=False,
            is_webhook=False,
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _entry(
    agent_id: str,
    *,
    tools: tuple[str, ...] = (),
    history_turns: int = 30,
) -> PhonebookEntry:
    return PhonebookEntry(
        agent_id=agent_id,
        display_name=f"{agent_id.title()} Bot",
        avatar_url=f"https://example.com/{agent_id}.png",
        description="test",
        tools=tools,
        history_turns=history_turns,
    )


# Default phonebook used by ``_ctx``: just alice and bob, no tools. Tests
# that need a different roster construct one inline and pass via ``phonebook=``.
_DEFAULT_PHONEBOOK = [_entry("alice"), _entry("bob")]

# Canonical ids returned by the default resolver / send mocks. Named
# constants keep the success-path assertions readable.
_UNIFIED_CHANNEL_ID = 12345
_REQUEST_SENT_MESSAGE_ID = 555
_RESPONSE_SENT_MESSAGE_ID = 556
_NEW_THREAD_ID = 99999


def _ctx(
    *,
    caller: str = "alice",
    wire: WireMessage | None = None,
    phonebook: list[PhonebookEntry] | None = None,
    extra_deps: dict[str, Any] | None = None,
) -> ToolContext:
    """Construct a ToolContext mirroring what calfkit's dispatch builds.

    The bridge ingress populates ``deps["phonebook"]`` on every invocation;
    tests do the same so the tool reads the same shape it would in
    production. ``extra_deps`` adds further ambient keys the bridge may seed
    at the root (e.g. the memory-prompt template) to verify they project
    forward across A2A.
    """
    if wire is None:
        wire = _wire()
    if phonebook is None:
        phonebook = _DEFAULT_PHONEBOOK
    return ToolContext(
        deps={
            "discord": wire.model_dump(mode="json"),
            "phonebook": phonebook_to_deps(phonebook),
            **(extra_deps or {}),
        },
        run_id="corr-1",
        agent_name=caller,
    )


def _sent_message(message_id: int, *, channel_id: int = _UNIFIED_CHANNEL_ID) -> SentMessage:
    return SentMessage(id=message_id, channel_id=channel_id)


def _sequential_send_mock(*message_ids: int) -> AsyncMock:
    """An AsyncMock for ``persona_sender.send`` that returns a fresh
    :class:`SentMessage` per call with the supplied ids in order.

    Most tests need 2 sends (request + response); a handful need 1 (e.g.
    timeout skips the response). Tests that exercise the retry loop will
    interleave exceptions and supply their own ``side_effect``.
    """
    return AsyncMock(side_effect=[_sent_message(mid) for mid in message_ids])


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject mocks into private_chat's module-level singletons.

    No registry: under the decoupled-deployment model the tool's only
    source of agent identity is the phonebook in ``ctx.deps``. Tests
    that want a different phonebook pass it to ``_ctx``.

    ``monkeypatch.setattr`` restores the originals after the test, so
    one test's ``init`` cannot leak into another's.

    Default resolver returns the unified channel id on
    :meth:`resolve_unified_channel` and a fresh thread id on
    :meth:`create_anchored_thread`. Default fetcher returns an empty
    tuple — overridden by continue-path tests.
    """
    client = MagicMock(spec=Client)
    client.execute_node = AsyncMock()
    persona_sender = MagicMock(spec=DiscordPersonaSender)
    persona_sender.send = _sequential_send_mock(
        _REQUEST_SENT_MESSAGE_ID, _RESPONSE_SENT_MESSAGE_ID
    )
    resolver = MagicMock(spec=A2AChannelResolver)
    resolver.resolve_unified_channel = AsyncMock(return_value=_UNIFIED_CHANNEL_ID)
    resolver.create_anchored_thread = AsyncMock(return_value=_NEW_THREAD_ID)
    discord_client = MagicMock(spec=discord.Client)
    # Mock the module-level helper directly rather than discord.Client's
    # `get_channel` / `fetch_channel` / `channel.history` chain. The helper
    # is the boundary the tool body crosses; mocking it lets tests pin the
    # contract surface (thread_id, limit, phonebook) without dragging
    # discord.py's async-iterator quirks into every test.
    fetch_thread_history = AsyncMock(return_value=[])

    monkeypatch.setattr(pc, "_client", client)
    monkeypatch.setattr(pc, "_persona_sender", persona_sender)
    monkeypatch.setattr(pc, "_resolver", resolver)
    monkeypatch.setattr(pc, "_discord_client", discord_client)
    monkeypatch.setattr(pc, "_fetch_thread_history", fetch_thread_history)
    monkeypatch.setattr(pc, "_timeout_seconds", 30.0)

    return {
        "client": client,
        "persona_sender": persona_sender,
        "resolver": resolver,
        "discord_client": discord_client,
        "fetch_thread_history": fetch_thread_history,
    }


def _result(text: str | None) -> Any:
    """A minimal stand-in for ``NodeResult`` carrying only the fields the
    tool reads. The real type has many more fields irrelevant here."""
    r = MagicMock()
    r.output = text
    r.correlation_id = "tool-corr"
    return r


def _record(content: str, *, author_agent_id: str | None = None) -> HistoryRecord:
    """Build a minimal :class:`HistoryRecord` for continue-path projection tests."""
    return HistoryRecord(
        message_id=1,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        content=content,
        author_display_name=author_agent_id or "ryan",
        author_agent_id=author_agent_id,
    )


def _parse_tag(returned: str) -> int:
    """Pull the integer thread id out of a success-path return string."""
    match = _RETURN_TAG_PATTERN.match(returned)
    assert match is not None, f"return missing <thread_id> tag: {returned!r}"
    return int(match.group(1))


class TestBuildThreadName:
    """Pure-function coverage for ``_build_thread_name``."""

    def test_basic(self) -> None:
        out = pc._build_thread_name("conan", "scribe", "please summarize the doc")
        assert out == "conan→scribe: please summarize the doc"

    def test_truncates_long_content(self) -> None:
        long = "x" * 200
        out = pc._build_thread_name("a", "b", long)
        # caller→target: <40 chars of x>
        assert out == "a→b: " + "x" * pc._THREAD_NAME_CONTENT_MAX
        # Topic tail is exactly the cap, no more.
        assert out.count("x") == pc._THREAD_NAME_CONTENT_MAX

    def test_strips_newlines(self) -> None:
        out = pc._build_thread_name("a", "b", "line1\nline2\rline3\n\rline4")
        # Newlines and carriage returns become single spaces; runs collapse.
        assert "\n" not in out
        assert "\r" not in out
        assert out == "a→b: line1 line2 line3 line4"

    def test_total_length_capped_at_100(self) -> None:
        # Pick caller/target names that consume most of the budget so the
        # truncation has to land somewhere inside the topic tail.
        caller = "x" * 40
        target = "y" * 40
        # 40 + 1 (arrow) + 40 + 2 (": ") + 40 (content cap) = 123 > 100
        out = pc._build_thread_name(caller, target, "z" * 200)
        assert len(out) == pc._THREAD_NAME_MAX_TOTAL

    def test_unicode_arrow_does_not_overflow_byte_limit(self) -> None:
        """Discord's cap is char-based, not byte-based. The multi-byte
        arrow consumes 1 char of the budget regardless of its UTF-8
        encoding length."""
        out = pc._build_thread_name("a", "b", "c")
        # 'a' + '→' + 'b' + ': ' + 'c' = 6 chars; arrow encodes as
        # 3 bytes in UTF-8 (verifying our assumption).
        assert len(out) == 6
        assert len(out.encode("utf-8")) > 6  # multi-byte arrow confirmed

    def test_empty_content_uses_placeholder(self) -> None:
        out = pc._build_thread_name("conan", "scribe", "")
        assert out == f"conan→scribe: {pc._THREAD_NAME_EMPTY_PLACEHOLDER}"
        # No trailing whitespace from a bare ": ".
        assert not out.endswith(" ")

    def test_whitespace_only_content(self) -> None:
        out = pc._build_thread_name("conan", "scribe", "   \n\t\r ")
        assert out == f"conan→scribe: {pc._THREAD_NAME_EMPTY_PLACEHOLDER}"

    def test_collapses_internal_whitespace_runs(self) -> None:
        """Runs of mixed whitespace collapse to one space — keeps the
        thread name compact and readable."""
        out = pc._build_thread_name("a", "b", "foo   bar\t\t\tbaz")
        assert out == "a→b: foo bar baz"

    def test_uses_full_unicode_arrow(self) -> None:
        """Regression guard against an ascii ``->`` substitution."""
        out = pc._build_thread_name("a", "b", "x")
        assert "→" in out
        assert "->" not in out


class TestFetchThreadHistory:
    """Cover the real ``_fetch_thread_history`` body (the rest of the
    test file monkeypatches the helper, so its identity-resolution and
    channel-type validation paths are not exercised elsewhere).

    Verifies the helper's three load-bearing behaviors:
    * webhook authors are resolved to ``agent_id`` via the phonebook;
    * non-webhook authors get ``author_agent_id=None`` regardless of
      whether their display_name happens to match an agent's;
    * non-messageable channel ids raise ``TypeError`` (the caller maps
      this to the documented recoverable LLM error string)."""

    @staticmethod
    def _msg(
        *,
        msg_id: int,
        content: str,
        display_name: str,
        webhook_id: int | None,
    ) -> Any:
        m = MagicMock()
        m.id = msg_id
        m.content = content
        m.created_at = datetime.fromtimestamp(1_700_000_000 + msg_id, UTC)
        m.author = MagicMock()
        m.author.display_name = display_name
        m.author.name = display_name
        m.webhook_id = webhook_id
        return m

    @staticmethod
    def _messageable_channel_with_history(messages: list[Any]) -> Any:
        """Build a mock that satisfies ``isinstance(_, discord.abc.Messageable)``
        and exposes an ``async for`` history iterator returning ``messages``
        newest-first (discord.py's actual contract)."""

        async def _ait() -> Any:
            for m in messages:
                yield m

        channel = MagicMock(spec=discord.abc.Messageable)
        channel.history = MagicMock(return_value=_ait())
        return channel

    async def test_webhook_display_name_in_phonebook_resolves_to_agent_id(
        self,
    ) -> None:
        phonebook = [_entry("alice"), _entry("bob")]
        channel = self._messageable_channel_with_history(
            [
                self._msg(msg_id=2, content="hi from alice", display_name="Alice Bot", webhook_id=999),
            ]
        )
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=channel)
        records = await pc._fetch_thread_history(
            client, thread_id=777, limit=10, phonebook=phonebook
        )
        assert len(records) == 1
        assert records[0].author_agent_id == "alice"

    async def test_webhook_display_name_not_in_phonebook_returns_none(
        self,
    ) -> None:
        phonebook = [_entry("alice")]
        channel = self._messageable_channel_with_history(
            [
                self._msg(msg_id=2, content="hi from unknown", display_name="Ghost", webhook_id=999),
            ]
        )
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=channel)
        records = await pc._fetch_thread_history(
            client, thread_id=777, limit=10, phonebook=phonebook
        )
        assert len(records) == 1
        assert records[0].author_agent_id is None

    async def test_non_webhook_author_never_resolves_to_agent(self) -> None:
        """A human happens to be named ``Alice Bot`` (matches the alice
        agent's display_name exactly) — must NOT be mistaken for the
        alice agent. The webhook_id branch is the only path that maps
        to an agent_id; non-webhook authors always stay None."""
        phonebook = [_entry("alice")]
        channel = self._messageable_channel_with_history(
            [
                self._msg(msg_id=2, content="hi from human alice", display_name="Alice Bot", webhook_id=None),
            ]
        )
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=channel)
        records = await pc._fetch_thread_history(
            client, thread_id=777, limit=10, phonebook=phonebook
        )
        assert len(records) == 1
        assert records[0].author_agent_id is None

    async def test_history_reversed_to_chronological(self) -> None:
        """discord.py returns newest-first; the helper must reverse."""
        phonebook = [_entry("alice")]
        channel = self._messageable_channel_with_history(
            [
                self._msg(msg_id=10, content="newest", display_name="Alice Bot", webhook_id=1),
                self._msg(msg_id=5, content="oldest", display_name="Alice Bot", webhook_id=1),
            ]
        )
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=channel)
        records = await pc._fetch_thread_history(
            client, thread_id=777, limit=10, phonebook=phonebook
        )
        # Reversed → oldest first.
        assert [r.content for r in records] == ["oldest", "newest"]

    async def test_limit_zero_short_circuits_no_discord_call(self) -> None:
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock()
        client.fetch_channel = AsyncMock()
        records = await pc._fetch_thread_history(
            client, thread_id=777, limit=0, phonebook=[]
        )
        assert records == []
        client.get_channel.assert_not_called()
        client.fetch_channel.assert_not_called()

    async def test_limit_negative_short_circuits(self) -> None:
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock()
        records = await pc._fetch_thread_history(
            client, thread_id=777, limit=-5, phonebook=[]
        )
        assert records == []
        client.get_channel.assert_not_called()

    async def test_limit_capped_at_discord_max(self) -> None:
        """``limit`` above Discord's 100-message cap is clamped silently."""
        phonebook = [_entry("alice")]
        channel = self._messageable_channel_with_history([])
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=channel)
        await pc._fetch_thread_history(
            client, thread_id=777, limit=500, phonebook=phonebook
        )
        # discord.py's channel.history(limit=...) — the helper must have
        # passed 100, not 500.
        channel.history.assert_called_once()
        assert channel.history.call_args.kwargs["limit"] == 100

    async def test_get_channel_miss_falls_back_to_fetch_channel(self) -> None:
        """The bot's in-memory channel cache misses on cold threads (gateway
        cache doesn't know about every thread). Must fall back to a REST
        ``fetch_channel`` lookup, not silently return ``[]``."""
        phonebook = [_entry("alice")]
        channel = self._messageable_channel_with_history([])
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=None)  # cache miss
        client.fetch_channel = AsyncMock(return_value=channel)
        await pc._fetch_thread_history(
            client, thread_id=777, limit=10, phonebook=phonebook
        )
        client.get_channel.assert_called_once_with(777)
        client.fetch_channel.assert_awaited_once_with(777)

    async def test_non_messageable_channel_raises_type_error(self) -> None:
        """A spoofed thread_id pointing at e.g. a CategoryChannel must NOT
        AttributeError into the agent runtime — raise ``TypeError`` so the
        caller maps it to the recoverable LLM error string."""
        category = MagicMock()  # NOT spec'd to Messageable → isinstance False
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=category)
        with pytest.raises(TypeError, match="not a messageable"):
            await pc._fetch_thread_history(
                client, thread_id=777, limit=10, phonebook=[]
            )


class TestContinueThreadNonMessageableChannel:
    """The TypeError from ``_fetch_thread_history`` when an LLM passes a
    thread_id pointing at a non-messageable channel must surface as the
    same recoverable error string the LLM already knows how to handle
    for Forbidden/NotFound — not as a ``RuntimeError`` or a raw raise."""

    async def test_type_error_returns_recoverable_error_string(
        self, deps: dict[str, Any]
    ) -> None:
        deps["fetch_thread_history"].side_effect = TypeError(
            "channel id=777 is a CategoryChannel, not a messageable channel/thread"
        )
        out = await pc.private_chat(
            _ctx(caller="alice"), "bob", "follow up", thread_id=777
        )
        assert "error:" in out
        assert "thread 777" in out
        assert "not accessible" in out
        # No tag on error — LLM must drop the bad id, not try to continue it.
        assert "<thread_id>" not in out
        deps["client"].execute_node.assert_not_called()


class TestContinueThreadOtherDiscordExceptions:
    """The catchall ``except discord.DiscordException`` branch funnels
    every Discord error class that isn't already handled (Forbidden,
    NotFound, HTTPException) through ``_raise_infra`` — so RateLimited,
    ConnectionClosed, and InvalidData no longer propagate raw."""

    async def test_rate_limited_raises_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        """``discord.RateLimited`` is NOT a ``HTTPException`` subclass —
        without the catchall it would propagate raw out of private_chat."""
        deps["fetch_thread_history"].side_effect = discord.RateLimited(retry_after=1.5)
        with pytest.raises(RuntimeError, match="thread history fetch failed"):
            await pc.private_chat(
                _ctx(caller="alice"), "bob", "follow up", thread_id=777
            )
        deps["client"].execute_node.assert_not_called()


class TestNewThreadPath:
    """Coverage for the default ``thread_id=None`` branch."""

    async def test_resolves_unified_channel_once(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        deps["resolver"].resolve_unified_channel.assert_awaited_once_with()

    async def test_returns_tagged_response(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        # Format pinned: <thread_id>NNN</thread_id>\n{response}.
        assert out == f"<thread_id>{_NEW_THREAD_ID}</thread_id>\nbob's reply"

    async def test_projects_caller_deps_forward_to_target(self, deps: dict[str, Any]) -> None:
        """Ambient deps the bridge seeds at the root (e.g. the memory-prompt
        template) project forward to the A2A target, so a memory agent reached
        via A2A still receives its memory instructions — while the A2A-specific
        keys remain overrides, not the caller's forwarded values."""
        deps["client"].execute_node.return_value = _result("ok")
        ctx = _ctx(caller="alice", extra_deps={"memory_prompt": "TEMPLATE {{MEMORY_DIR}}"})
        await pc.private_chat(ctx, "bob", "hello")
        forwarded = deps["client"].execute_node.await_args.kwargs["deps"]
        assert forwarded["memory_prompt"] == "TEMPLATE {{MEMORY_DIR}}"
        # A2A-owned keys are overridden after the spread (this hop's caller,
        # the forwarded wire, the refreshed roster) — not the caller's copies.
        assert forwarded["caller_agent_id"] == "alice"
        assert "phonebook" in forwarded and "discord" in forwarded

    async def test_forwarding_is_transparent_no_phantom_key(self, deps: dict[str, Any]) -> None:
        """A caller without an ambient key doesn't get one fabricated downstream
        — the spread forwards only what's present."""
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        forwarded = deps["client"].execute_node.await_args.kwargs["deps"]
        assert "memory_prompt" not in forwarded

    async def test_first_send_to_unified_channel_without_thread_id(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        # First call = caller's request projection. Posted to the unified
        # channel (no thread_id) so it can serve as the thread anchor.
        first = deps["persona_sender"].send.await_args_list[0]
        assert first.kwargs["channel_id"] == _UNIFIED_CHANNEL_ID
        assert first.kwargs.get("thread_id") is None

    async def test_create_anchored_thread_called_with_sent_id_and_name(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "summarize the spec")
        deps["resolver"].create_anchored_thread.assert_awaited_once()
        call = deps["resolver"].create_anchored_thread.await_args
        # Channel id and anchor message id positional, name kwarg.
        assert call.args == (_UNIFIED_CHANNEL_ID, _REQUEST_SENT_MESSAGE_ID)
        assert call.kwargs["name"] == "alice→bob: summarize the spec"

    async def test_execute_node_receives_empty_message_history(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        assert (
            deps["client"].execute_node.await_args.kwargs["message_history"] == []
        )

    async def test_response_posted_into_new_thread(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        # Second call = target's response projection, posted into the
        # newly-anchored thread.
        second = deps["persona_sender"].send.await_args_list[1]
        assert second.kwargs["channel_id"] == _UNIFIED_CHANNEL_ID
        assert second.kwargs["thread_id"] == _NEW_THREAD_ID

    async def test_fetch_thread_history_never_called_on_new_thread(
        self, deps: dict[str, Any]
    ) -> None:
        """Default branch must not touch the fetcher — no thread exists yet
        to read from."""
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        deps["fetch_thread_history"].assert_not_called()

    async def test_return_value_matches_documented_pattern(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("multi\nline\nreply")
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        # The DOTALL pattern in the docstring must match the actual return.
        match = _RETURN_TAG_PATTERN.match(out)
        assert match is not None
        assert int(match.group(1)) == _NEW_THREAD_ID

    async def test_caller_persona_sent_first(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        await pc.private_chat(_ctx(caller="alice"), "bob", "alice asks")
        first_persona = deps["persona_sender"].send.await_args_list[0].args[0]
        assert first_persona.name == "Alice Bot"
        first_content = deps["persona_sender"].send.await_args_list[0].kwargs["content"]
        assert first_content == "alice asks"

    async def test_target_persona_sent_second(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        await pc.private_chat(_ctx(caller="alice"), "bob", "alice asks")
        second_persona = deps["persona_sender"].send.await_args_list[1].args[0]
        assert second_persona.name == "Bob Bot"
        second_content = deps["persona_sender"].send.await_args_list[1].kwargs["content"]
        assert second_content == "bob's reply"


class TestContinueThreadPath:
    """Coverage for the ``thread_id=<int>`` continuation branch."""

    async def test_fetcher_called_with_target_history_turns_and_phonebook(
        self, deps: dict[str, Any]
    ) -> None:
        phonebook = [_entry("alice"), _entry("bob", history_turns=15)]
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(
            _ctx(caller="alice", phonebook=phonebook), "bob", "follow up", thread_id=777
        )
        deps["fetch_thread_history"].assert_awaited_once()
        call = deps["fetch_thread_history"].await_args
        # _fetch_thread_history(discord_client, thread_id, *, limit, phonebook)
        assert call.args[0] is deps["discord_client"]
        assert call.args[1] == 777
        assert call.kwargs["limit"] == 15
        # `phonebook` is required so the helper can map webhook display_name → agent_id.
        assert "phonebook" in call.kwargs

    async def test_fetcher_called_strictly_before_request_projection(
        self, deps: dict[str, Any]
    ) -> None:
        """Continue-path ordering invariant: history MUST be fetched
        BEFORE the caller's request is posted into the thread. Posting
        first then fetching would put the just-posted request in both
        ``message_history`` AND ``user_prompt``, surfacing as a duplicate
        to the callee. Pins the ordering so a refactor swap can't
        silently regress."""
        call_order: list[str] = []

        async def record_fetch(*args: Any, **kwargs: Any) -> list[Any]:
            call_order.append("fetch")
            return []

        async def record_send(*args: Any, **kwargs: Any) -> SentMessage:
            call_order.append("send")
            return SentMessage(id=_REQUEST_SENT_MESSAGE_ID, channel_id=_UNIFIED_CHANNEL_ID)

        deps["fetch_thread_history"].side_effect = record_fetch
        deps["persona_sender"].send = record_send
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "follow up", thread_id=777)
        # First two events are exactly fetch → send (then more sends for the response).
        assert call_order[0] == "fetch"
        assert call_order[1] == "send"

    async def test_resolve_unified_channel_called(
        self, deps: dict[str, Any]
    ) -> None:
        """Even on continue we still resolve the channel — the cache makes
        this cheap after the first call but the call still happens (the
        request projection needs the channel id)."""
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "follow up", thread_id=777)
        deps["resolver"].resolve_unified_channel.assert_awaited_once_with()

    async def test_create_anchored_thread_never_called_on_continue(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "follow up", thread_id=777)
        deps["resolver"].create_anchored_thread.assert_not_called()

    async def test_message_history_projected_from_target_pov(
        self, deps: dict[str, Any]
    ) -> None:
        """Prior records authored by the target become :class:`ModelResponse`;
        all others become :class:`ModelRequest`. The projection function
        is tested exhaustively in ``tests/bridge/test_history.py``;
        here we just confirm the tool wires the right ``self_agent_id``."""
        deps["fetch_thread_history"].return_value = [
            _record("alice asked", author_agent_id="alice"),
            _record("bob replied", author_agent_id="bob"),
        ]
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "follow up", thread_id=777)
        passed_history = deps["client"].execute_node.await_args.kwargs["message_history"]
        # Two records, one from each side; passes both through.
        assert len(passed_history) == 2

    async def test_message_history_passed_to_execute_node(
        self, deps: dict[str, Any]
    ) -> None:
        """Distinct from the per-POV projection test — pins that the
        history actually rides on the ``execute_node`` kwarg."""
        deps["fetch_thread_history"].return_value = [
            _record("alice asked", author_agent_id="alice"),
        ]
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "follow up", thread_id=777)
        history = deps["client"].execute_node.await_args.kwargs["message_history"]
        assert len(history) == 1

    async def test_request_projection_posted_into_thread(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "follow up", thread_id=777)
        first = deps["persona_sender"].send.await_args_list[0]
        assert first.kwargs["channel_id"] == _UNIFIED_CHANNEL_ID
        assert first.kwargs["thread_id"] == 777

    async def test_response_projection_posted_into_same_thread(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "follow up", thread_id=777)
        second = deps["persona_sender"].send.await_args_list[1]
        assert second.kwargs["channel_id"] == _UNIFIED_CHANNEL_ID
        assert second.kwargs["thread_id"] == 777

    async def test_return_tag_carries_same_thread_id(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        out = await pc.private_chat(
            _ctx(caller="alice"), "bob", "follow up", thread_id=777
        )
        assert _parse_tag(out) == 777


class TestContinueThreadFetcherErrors:
    async def test_fetcher_forbidden_returns_recoverable_error(
        self, deps: dict[str, Any]
    ) -> None:
        """``discord.Forbidden`` on the thread fetch is LLM-recoverable —
        the caller's id is invalid. The tool returns the documented
        error string with no ``<thread_id>`` tag so the LLM doesn't try
        to continue a dead thread."""
        deps["fetch_thread_history"].side_effect = _http_exc(discord.Forbidden, 403)
        out = await pc.private_chat(
            _ctx(caller="alice"), "bob", "follow up", thread_id=777
        )
        assert "error:" in out
        assert "thread 777" in out
        assert "not accessible" in out
        assert "omitting thread_id" in out
        # No tag on error.
        assert "<thread_id>" not in out
        # execute_node was never reached.
        deps["client"].execute_node.assert_not_called()

    async def test_fetcher_not_found_returns_recoverable_error(
        self, deps: dict[str, Any]
    ) -> None:
        deps["fetch_thread_history"].side_effect = _http_exc(discord.NotFound, 404)
        out = await pc.private_chat(
            _ctx(caller="alice"), "bob", "follow up", thread_id=777
        )
        assert "error:" in out
        assert "thread 777" in out
        assert "<thread_id>" not in out
        deps["client"].execute_node.assert_not_called()

    async def test_fetcher_http_5xx_raises_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        """Transient 5xx is infrastructure, not LLM input — funnel through
        ``_raise_infra``."""
        deps["fetch_thread_history"].side_effect = _http_exc(discord.HTTPException, 503)
        with pytest.raises(RuntimeError, match="thread history fetch failed"):
            await pc.private_chat(
                _ctx(caller="alice"), "bob", "follow up", thread_id=777
            )
        deps["client"].execute_node.assert_not_called()

    async def test_fetcher_error_logs_caller_target_thread(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The WARN log on a recoverable fetch failure must include
        caller/target/thread_id so an operator can correlate."""
        import logging as _logging

        deps["fetch_thread_history"].side_effect = _http_exc(discord.Forbidden, 403)
        with caplog.at_level(_logging.WARNING):
            await pc.private_chat(
                _ctx(caller="alice"), "bob", "follow up", thread_id=777
            )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "caller=alice" in joined
        assert "target=bob" in joined
        assert "thread_id=777" in joined


class TestNewThreadAnchorFailure:
    async def test_anchor_failure_raises_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        """``create_anchored_thread`` raising any Discord exception means
        the conversation has no thread and continuation is impossible —
        infra escalation. The already-posted request projection is an
        acceptable orphan (audit gap, not failure)."""
        deps["resolver"].create_anchored_thread = AsyncMock(
            side_effect=_http_exc(discord.Forbidden, 403)
        )
        with pytest.raises(RuntimeError, match="create_anchored_thread failed"):
            await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        # The first persona.send (request projection) happened; that's
        # acceptable orphan data.
        assert deps["persona_sender"].send.await_count >= 1
        # The execute_node call never happened.
        deps["client"].execute_node.assert_not_called()

    async def test_anchor_failure_on_not_found_raises(
        self, deps: dict[str, Any]
    ) -> None:
        """Anchor message disappeared between post and create_thread (race)."""
        deps["resolver"].create_anchored_thread = AsyncMock(
            side_effect=_http_exc(discord.NotFound, 404)
        )
        with pytest.raises(RuntimeError, match="create_anchored_thread failed"):
            await pc.private_chat(_ctx(caller="alice"), "bob", "hello")

    async def test_anchor_failure_on_http_exception_raises(
        self, deps: dict[str, Any]
    ) -> None:
        """5xx on anchor creation is still infra — no thread, no continuation."""
        deps["resolver"].create_anchored_thread = AsyncMock(
            side_effect=_http_exc(discord.HTTPException, 503)
        )
        with pytest.raises(RuntimeError, match="create_anchored_thread failed"):
            await pc.private_chat(_ctx(caller="alice"), "bob", "hello")

    async def test_request_projection_audit_gap_raises_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        """If the request projection exhausts retries and accepts an
        audit gap (returns None), the new-thread branch has no anchor
        message id — there is nothing to call ``create_thread`` on, so
        the tool must escalate via ``_raise_infra`` rather than skip
        thread creation silently."""
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 503),  # request attempt 1
                _http_exc(discord.HTTPException, 503),  # request attempt 2
            ]
        )
        with patch(
            "calfcord.tools.builtin.private_chat.asyncio.sleep", new=AsyncMock()
        ), pytest.raises(RuntimeError, match="no anchor message available"):
            await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        deps["resolver"].create_anchored_thread.assert_not_called()
        deps["client"].execute_node.assert_not_called()


class TestInputErrors:
    async def test_self_target_returns_error_string(
        self, deps: dict[str, Any]
    ) -> None:
        """LLM-recoverable error: returned as a string so the calling LLM
        can adapt rather than aborting the whole turn. No tag on error."""
        out = await pc.private_chat(_ctx(caller="alice"), "alice", "x")
        assert "cannot privately chat with itself" in out
        assert "<thread_id>" not in out
        deps["client"].execute_node.assert_not_called()

    async def test_unknown_target_returns_error_with_known_list(
        self, deps: dict[str, Any]
    ) -> None:
        out = await pc.private_chat(_ctx(caller="alice"), "carol", "x")
        assert "unknown agent" in out
        assert "alice" in out
        assert "bob" in out
        assert "<thread_id>" not in out
        deps["client"].execute_node.assert_not_called()


class TestInfraErrors:
    async def test_not_initialized_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling the tool body without ``init()`` is a runner bug; raise
        so it surfaces in logs rather than degrading silently."""
        monkeypatch.setattr(pc, "_client", None)
        monkeypatch.setattr(pc, "_persona_sender", None)
        monkeypatch.setattr(pc, "_resolver", None)
        monkeypatch.setattr(pc, "_discord_client", None)
        with pytest.raises(RuntimeError, match="not initialized"):
            await pc.private_chat(_ctx(), "bob", "x")

    async def test_missing_discord_client_alone_raises(
        self, deps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with every other singleton set, an absent discord client
        must still fail-fast — pin the contract that ``init`` validates
        all four."""
        monkeypatch.setattr(pc, "_discord_client", None)
        with pytest.raises(RuntimeError, match="not initialized"):
            await pc.private_chat(_ctx(), "bob", "x")

    async def test_missing_emitter_node_id_raises(self, deps: dict[str, Any]) -> None:
        ctx = _ctx()
        ctx.agent_name = None  # simulate missing emitter
        with pytest.raises(RuntimeError, match="emitter_node_id"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_missing_phonebook_dep_raises(self, deps: dict[str, Any]) -> None:
        ctx = ToolContext(
            deps={},
            run_id="c",
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="deps\\['phonebook'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_missing_discord_dep_raises(self, deps: dict[str, Any]) -> None:
        ctx = ToolContext(
            deps={"phonebook": phonebook_to_deps(_DEFAULT_PHONEBOOK)},
            run_id="c",
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="deps\\['discord'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_unknown_caller_raises(self, deps: dict[str, Any]) -> None:
        phonebook = [_entry("bob")]
        with pytest.raises(RuntimeError, match="not in the phonebook"):
            await pc.private_chat(
                _ctx(caller="ghost", phonebook=phonebook), "bob", "x"
            )

    async def test_malformed_phonebook_wrapped_as_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        ctx = ToolContext(
            deps={
                "discord": _wire().model_dump(mode="json"),
                "phonebook": [{"agent_id": "alice"}],
            },
            run_id="c",
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="malformed deps\\['phonebook'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_non_list_phonebook_wrapped_as_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        ctx = ToolContext(
            deps={
                "discord": _wire().model_dump(mode="json"),
                "phonebook": "not a list",
            },
            run_id="c",
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="malformed deps\\['phonebook'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_malformed_wire_wrapped_as_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        ctx = ToolContext(
            deps={
                "discord": {"only": "garbage"},
                "phonebook": phonebook_to_deps(_DEFAULT_PHONEBOOK),
            },
            run_id="c",
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="malformed deps\\['discord'\\]"):
            await pc.private_chat(ctx, "bob", "x")


class TestRequestProjectionBestEffort:
    """Request-side projection (pre-RPC) is best-effort *on continue*: the
    calfkit RPC runs even if it fails. README documents this. Response-side
    projection raises (see :class:`TestResponseProjectionRaises`).

    Note: on the *new-thread* branch a persistent request failure must
    raise instead — see
    :meth:`TestNewThreadAnchorFailure.test_request_projection_audit_gap_raises_runtime_error`
    — because we cannot anchor a thread on a phantom message.
    """

    async def test_continue_request_transient_failure_does_not_abort(
        self, deps: dict[str, Any]
    ) -> None:
        """On continue, a persistent transient request projection failure
        is logged and accepted; the RPC still runs."""
        deps["client"].execute_node.return_value = _result("bob's reply")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 503),  # request attempt 1
                _http_exc(discord.HTTPException, 503),  # request attempt 2
                _sent_message(_RESPONSE_SENT_MESSAGE_ID),  # response attempt 1
            ]
        )
        with patch(
            "calfcord.tools.builtin.private_chat.asyncio.sleep", new=AsyncMock()
        ):
            out = await pc.private_chat(
                _ctx(caller="alice"), "bob", "x", thread_id=777
            )
        assert _parse_tag(out) == 777
        assert out.endswith("bob's reply")

    async def test_continue_request_failure_logs_accepting_gap(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 503),
                _http_exc(discord.HTTPException, 503),
                _sent_message(_RESPONSE_SENT_MESSAGE_ID),
            ]
        )
        with patch(
            "calfcord.tools.builtin.private_chat.asyncio.sleep", new=AsyncMock()
        ), caplog.at_level(_logging.WARNING):
            await pc.private_chat(
                _ctx(caller="alice"), "bob", "x", thread_id=777
            )
        final = [r for r in caplog.records if "accepting audit gap" in r.message]
        assert final, "expected the audit-gap log on persistent request failure"
        assert all(r.levelno >= _logging.ERROR for r in final)
        joined = " ".join(r.getMessage() for r in final)
        assert "caller=alice" in joined
        assert "target=bob" in joined
        assert "thread_id=777" in joined

    async def test_projection_succeeds_on_retry(self, deps: dict[str, Any]) -> None:
        """First attempt fails transient, second succeeds. Pins that the
        retry loop still returns the eventual ``SentMessage`` (needed for
        the new-thread anchor)."""
        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 503),
                _sent_message(_REQUEST_SENT_MESSAGE_ID),
                _sent_message(_RESPONSE_SENT_MESSAGE_ID),
            ]
        )
        with patch(
            "calfcord.tools.builtin.private_chat.asyncio.sleep", new=AsyncMock()
        ):
            out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Three sends: request attempt 1 (fail), request attempt 2 (ok),
        # response attempt (ok).
        assert deps["persona_sender"].send.await_count == 3
        # The retry's SentMessage flowed through as the anchor — verified
        # by create_anchored_thread receiving the retried message id.
        call = deps["resolver"].create_anchored_thread.await_args
        assert call.args[1] == _REQUEST_SENT_MESSAGE_ID
        # And the return is properly tagged.
        assert _parse_tag(out) == _NEW_THREAD_ID

    async def test_retry_sleeps_with_module_constant(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 503),
                _sent_message(_REQUEST_SENT_MESSAGE_ID),
                _sent_message(_RESPONSE_SENT_MESSAGE_ID),
            ]
        )
        with patch(
            "calfcord.tools.builtin.private_chat.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep_mock:
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        sleep_mock.assert_awaited_once_with(pc._PROJECTION_RETRY_DELAY_SECONDS)

    async def test_non_discord_projection_error_propagates(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=RuntimeError("sender not started")
        )
        with pytest.raises(RuntimeError, match="sender not started"):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")

    async def test_forbidden_propagates_without_retry(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=_http_exc(discord.Forbidden, 403)
        )
        with pytest.raises(discord.Forbidden):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert deps["persona_sender"].send.await_count == 1

    async def test_not_found_propagates_without_retry(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=_http_exc(discord.NotFound, 404)
        )
        with pytest.raises(discord.NotFound):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert deps["persona_sender"].send.await_count == 1


class TestResponseProjectionRaises:
    """Transient / non-agent-fixable failures on the response side raise
    via ``_raise_infra`` rather than swallowing — the caller's LLM
    cannot fix Discord-side outage (5xx) or permission errors
    (403/404/etc.) by re-thinking its reply. Agent-fixable cases
    (400 family) go through ``_post_response_with_feedback_retries``'s
    retry path instead and are covered by ``TestA2ARetryWithFeedback``.
    """

    async def test_transient_5xx_raises_infra(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),  # request side OK
                _http_exc(discord.HTTPException, 503),    # response side 5xx
            ]
        )
        with pytest.raises(
            RuntimeError, match="a2a audit projection failed"
        ) as ei:
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert isinstance(ei.value.__cause__, discord.HTTPException)
        assert "transient status=503" in str(ei.value)

    async def test_non_agent_fixable_403_raises_infra(
        self, deps: dict[str, Any]
    ) -> None:
        """403 Forbidden is non-agent-fixable (missing Manage Webhooks
        permission) — the LLM can't fix permission issues by changing
        content. Retry-with-feedback does not fire."""
        deps["client"].execute_node.return_value = _result("bob's reply")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),  # request side OK
                _http_exc(discord.Forbidden, 403),        # response side 403
            ]
        )
        with pytest.raises(
            RuntimeError, match="a2a audit projection failed"
        ) as ei:
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert isinstance(ei.value.__cause__, discord.HTTPException)
        assert "non-agent-fixable status=403" in str(ei.value)
        # Only ONE response-side send attempt — drop is immediate, no
        # retry-with-feedback for permission errors.
        assert deps["persona_sender"].send.await_count == 2

    async def test_rate_limited_response_raises_infra(
        self, deps: dict[str, Any]
    ) -> None:
        """:class:`discord.RateLimited` is a sibling of
        :class:`HTTPException` (not a subclass) — discord.py's internal
        backoff has already exhausted its budget by the time this
        bubbles up. The orchestrator's ``except`` clause must be broad
        enough to catch it (``discord.DiscordException``, not just
        ``HTTPException``); ``classify_error`` returns ``"drop"`` and
        the orchestrator raises infra. Without the broadened catch this
        exception would escape uncaught and the caller's LLM would see
        a raw ``RateLimited`` traceback bypassing the documented
        ``RuntimeError`` contract."""
        deps["client"].execute_node.return_value = _result("bob's reply")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),  # request side OK
                discord.RateLimited(retry_after=5.0),      # response side RateLimited
            ]
        )
        with pytest.raises(
            RuntimeError, match="a2a audit projection failed"
        ) as ei:
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert isinstance(ei.value.__cause__, discord.RateLimited)
        # Status string falls back to type name for RateLimited (no
        # ``.status`` attribute on the exception).
        assert "non-agent-fixable status=RateLimited" in str(ei.value)

    async def test_response_projection_failure_logs_correlation_caller_target(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        deps["client"].execute_node.return_value = _result("bob's reply")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),
                _http_exc(discord.HTTPException, 503),
            ]
        )
        with caplog.at_level(_logging.ERROR), pytest.raises(RuntimeError):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "caller=alice" in joined
        assert "target=bob" in joined
        assert "correlation_id=tool-corr" in joined


class TestInit:
    """``init()`` is the only path the runner uses to wire dependencies.
    A regression that swapped parameters would silently break A2A at
    runtime — pin the bindings."""

    def test_init_binds_each_arg_to_its_singleton(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "_client", None)
        monkeypatch.setattr(pc, "_persona_sender", None)
        monkeypatch.setattr(pc, "_resolver", None)
        monkeypatch.setattr(pc, "_discord_client", None)
        monkeypatch.setattr(pc, "_timeout_seconds", -1.0)

        client = MagicMock(spec=Client)
        persona_sender = MagicMock(spec=DiscordPersonaSender)
        resolver = MagicMock(spec=A2AChannelResolver)
        discord_client = MagicMock(spec=discord.Client)

        pc.init(
            client=client,
            persona_sender=persona_sender,
            resolver=resolver,
            discord_client=discord_client,
            timeout_seconds=42.0,
        )

        assert pc._client is client
        assert pc._persona_sender is persona_sender
        assert pc._resolver is resolver
        assert pc._discord_client is discord_client
        assert pc._timeout_seconds == 42.0


class TestExecuteNodeFailures:
    async def test_timeout_returns_error_string_not_raise(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.side_effect = TimeoutError()
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert "did not reply" in out
        assert "bob" in out
        # Timeout is an error path — no tag.
        assert "<thread_id>" not in out

    async def test_timeout_skips_response_projection(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.side_effect = TimeoutError()
        await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Only the request projection ran (1 send call).
        assert deps["persona_sender"].send.await_count == 1

    async def test_connection_error_wrapped_as_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        original = ConnectionError("kafka unreachable")
        deps["client"].execute_node.side_effect = original
        with pytest.raises(RuntimeError, match="execute_node failed") as ei:
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        msg = str(ei.value)
        assert "agent.bob.in" in msg
        assert ei.value.__cause__ is original

    async def test_generic_runtime_error_wrapped_via_raise_infra(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        original = RuntimeError("some calfkit internal")
        deps["client"].execute_node.side_effect = original
        with caplog.at_level(_logging.ERROR), pytest.raises(RuntimeError) as ei:
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert ei.value is not original
        assert ei.value.__cause__ is original
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "caller=alice" in joined
        assert "target=bob" in joined
        assert "correlation_id=corr-1" in joined

    async def test_cancelled_error_propagates_untouched(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")


class TestResolverFailure:
    async def test_resolver_failure_propagates_and_skips_invocation(
        self, deps: dict[str, Any]
    ) -> None:
        deps["resolver"].resolve_unified_channel.side_effect = discord.Forbidden(
            MagicMock(status=403), "missing permission"
        )
        with pytest.raises(discord.Forbidden):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        deps["client"].execute_node.assert_not_called()
        deps["persona_sender"].send.assert_not_called()
        deps["resolver"].create_anchored_thread.assert_not_called()

    async def test_resolver_failure_logs_caller_and_target(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging as _logging

        deps["resolver"].resolve_unified_channel.side_effect = discord.Forbidden(
            MagicMock(status=403), "missing permission"
        )
        with caplog.at_level(_logging.ERROR), pytest.raises(discord.Forbidden):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "caller=alice" in joined
        assert "target=bob" in joined


class TestEmptyResponseInsideThread:
    async def test_empty_response_posts_placeholder_into_new_thread(
        self, deps: dict[str, Any]
    ) -> None:
        """Empty content placeholder behavior still applies inside a
        thread — regression for the projection's empty-content branch
        when posting to a thread_id."""
        deps["client"].execute_node.return_value = _result("")
        await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        second = deps["persona_sender"].send.await_args_list[1]
        assert second.kwargs["content"] == "(empty response)"
        assert second.kwargs["thread_id"] == _NEW_THREAD_ID

    async def test_empty_response_posts_placeholder_into_continued_thread(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("")
        await pc.private_chat(_ctx(caller="alice"), "bob", "x", thread_id=777)
        second = deps["persona_sender"].send.await_args_list[1]
        assert second.kwargs["content"] == "(empty response)"
        assert second.kwargs["thread_id"] == 777

    async def test_none_response_treated_as_empty(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result(None)
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Return surface: tag + newline + empty string. We assert the
        # tag is present and the post-newline body is empty.
        match = _RETURN_TAG_PATTERN.match(out)
        assert match is not None
        assert out[match.end():] == ""
        second_content = (
            deps["persona_sender"].send.await_args_list[1].kwargs["content"]
        )
        assert second_content == "(empty response)"


class TestReturnTagFormat:
    async def test_success_return_starts_with_tag(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("hello")
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert out.startswith(f"<thread_id>{_NEW_THREAD_ID}</thread_id>\n")

    async def test_error_returns_have_no_tag(
        self, deps: dict[str, Any]
    ) -> None:
        """Every recoverable error string is bare — the LLM must not try
        to continue an error response."""
        # self-target
        out = await pc.private_chat(_ctx(caller="alice"), "alice", "x")
        assert "<thread_id>" not in out
        # unknown target
        out = await pc.private_chat(_ctx(caller="alice"), "carol", "x")
        assert "<thread_id>" not in out
        # timeout
        deps["client"].execute_node.side_effect = TimeoutError()
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert "<thread_id>" not in out

    async def test_tagged_thread_id_round_trip(
        self, deps: dict[str, Any]
    ) -> None:
        """Call with thread_id=None, parse the returned thread id, call
        again with that id and assert the fetcher received it."""
        deps["client"].execute_node.return_value = _result("first reply")
        first = await pc.private_chat(_ctx(caller="alice"), "bob", "hello")
        returned_thread_id = _parse_tag(first)
        assert returned_thread_id == _NEW_THREAD_ID

        # Second call: use that id. Reset the send mock to have enough
        # SentMessage returns for the second invocation.
        deps["persona_sender"].send = _sequential_send_mock(
            _REQUEST_SENT_MESSAGE_ID + 100, _RESPONSE_SENT_MESSAGE_ID + 100
        )
        await pc.private_chat(
            _ctx(caller="alice"), "bob", "follow up", thread_id=returned_thread_id
        )
        deps["fetch_thread_history"].assert_awaited_once()
        call = deps["fetch_thread_history"].await_args
        # _fetch_thread_history(discord_client, thread_id, *, limit, phonebook)
        assert call.args[0] is deps["discord_client"]
        assert call.args[1] == returned_thread_id


class TestForwardedWire:
    """The wire forwarded to the target preserves Discord context but
    rewrites slash_target/kind/content. Regression coverage from the
    pre-thread implementation; the architecture change does not affect
    this surface."""

    async def test_forwarded_wire_overrides_slash_target_and_kind(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        inbound = _wire(kind="message", content="orig")
        await pc.private_chat(_ctx(caller="alice", wire=inbound), "bob", "new")
        forwarded = deps["client"].execute_node.await_args.kwargs["deps"]["discord"]
        assert forwarded["slash_target"] == "bob"
        assert forwarded["kind"] == "slash"

    async def test_forwarded_wire_content_is_a2a_payload(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        inbound = _wire(content="original")
        await pc.private_chat(
            _ctx(caller="alice", wire=inbound), "bob", "the a2a request"
        )
        forwarded = deps["client"].execute_node.await_args.kwargs["deps"]["discord"]
        assert forwarded["content"] == "the a2a request"

    async def test_forwarded_wire_preserves_channel_and_author(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        inbound = _wire(channel_id=777)
        await pc.private_chat(_ctx(caller="alice", wire=inbound), "bob", "x")
        forwarded = deps["client"].execute_node.await_args.kwargs["deps"]["discord"]
        assert forwarded["channel_id"] == 777
        assert forwarded["author"]["display_name"] == "ryan"

    async def test_passes_caller_agent_id_in_deps(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "msg")
        passed_deps = deps["client"].execute_node.await_args.kwargs["deps"]
        assert passed_deps["caller_agent_id"] == "alice"

    async def test_invokes_target_inbox_topic(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "msg")
        call = deps["client"].execute_node.await_args
        assert call.kwargs["topic"] == "agent.bob.in"

    async def test_uses_configured_timeout(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(), "bob", "x")
        assert deps["client"].execute_node.await_args.kwargs["timeout"] == 30.0

    async def test_passes_temp_instructions_for_target(
        self, deps: dict[str, Any]
    ) -> None:
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("private_chat",)),
            _entry("carol"),
        ]
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice", phonebook=phonebook), "bob", "x")
        instructions = deps["client"].execute_node.await_args.kwargs[
            "temp_instructions"
        ]
        assert instructions is not None
        assert "carol" in instructions
        assert "bob" not in instructions

    async def test_propagates_phonebook_to_target_deps(
        self, deps: dict[str, Any]
    ) -> None:
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("private_chat",)),
            _entry("carol"),
        ]
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice", phonebook=phonebook), "bob", "x")
        passed_deps = deps["client"].execute_node.await_args.kwargs["deps"]
        propagated = passed_deps["phonebook"]
        ids = sorted(e["agent_id"] for e in propagated)
        assert ids == ["alice", "bob", "carol"]
        propagated_bob = next(e for e in propagated if e["agent_id"] == "bob")
        assert propagated_bob["tools"] == ["private_chat"]
        assert propagated_bob["display_name"] == "Bob Bot"
        assert propagated_bob["avatar_url"] == "https://example.com/bob.png"


class TestA2ARetryWithFeedback:
    """Agent-fixable Discord rejections (400 family) trigger
    retry-with-feedback: the target is re-invoked with a
    ``<system-reminder>``-tagged prompt + its failed reply in
    history, and gets a chance to adapt (typically by shortening).
    Mirrors the bridge outbox's behavior so channel replies and
    A2A replies share the same UX contract.

    The orchestrator is
    :func:`~calfcord.tools.builtin.private_chat._post_response_with_feedback_retries`;
    it directly invokes ``_persona_sender.send`` (NOT
    ``_post_projection``) so HTTPException can reach
    ``classify_error`` instead of being swallowed by
    ``_post_projection``'s internal retry-and-raise.
    """

    async def test_retry_succeeds_on_second_attempt(
        self,
        deps: dict[str, Any],
    ) -> None:
        """First response projection 400s; orchestrator triggers a
        retry; target produces shorter text; second projection
        succeeds. Caller receives the SHORTER text — that's what the
        audit thread shows, and the caller's LLM gets the same view."""
        long_reply = "x" * 3000
        short_reply = "x" * 1000
        # execute_node is called twice: first for the original
        # response, second for the retry.
        deps["client"].execute_node = AsyncMock(
            side_effect=[_result(long_reply), _result(short_reply)]
        )
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),  # request side OK
                _http_exc(discord.HTTPException, 400),    # response attempt 1 → 400
                _sent_message(99998),                      # response attempt 2 OK
            ]
        )
        returned = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Caller sees the shorter retry reply, not the rejected one.
        assert short_reply in returned
        assert long_reply not in returned
        # Two execute_node calls (original + retry).
        assert deps["client"].execute_node.await_count == 2
        # Three persona_sender.send calls (request + failed response + retried response).
        assert deps["persona_sender"].send.await_count == 3

    async def test_retry_envelope_contains_system_reminder_and_failed_text(
        self,
        deps: dict[str, Any],
    ) -> None:
        """The retry envelope pins every load-bearing kwarg so a
        regression in any of:

        * ``user_prompt`` (the system-reminder text)
        * ``message_history`` (original + caller request + failed reply)
        * ``temp_instructions`` (peer roster for A2A target)
        * ``timeout`` (matches the original call's budget)
        * ``deps`` (discord wire + caller_agent_id + phonebook)
        * ``topic`` (the target's agent inbox)

        ...fails loudly rather than silently regressing what the
        target LLM sees on retry."""
        from calfkit._vendor.pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            TextPart,
            UserPromptPart,
        )
        long_reply = "x" * 3000
        deps["client"].execute_node = AsyncMock(
            side_effect=[_result(long_reply), _result("short")]
        )
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),
                _http_exc(discord.HTTPException, 400),
                _sent_message(99998),
            ]
        )
        # Phonebook with bob having ``private_chat`` so the A2A
        # ``temp_instructions`` is non-None (it gates on the target
        # having the tool — without it the helper correctly returns
        # ``None`` and there's nothing to assert against).
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("private_chat",)),
        ]
        await pc.private_chat(
            _ctx(caller="alice", phonebook=phonebook),
            "bob",
            "the original caller content",
        )
        # Inspect the retry call's kwargs (second execute_node).
        retry_call = deps["client"].execute_node.await_args_list[1]
        kwargs = retry_call.kwargs
        # User prompt is the system-reminder.
        assert "<system-reminder>" in kwargs["user_prompt"]
        # History ends with the caller's request + the failed reply.
        history = kwargs["message_history"]
        assert isinstance(history[-2], ModelRequest)
        assert isinstance(history[-2].parts[0], UserPromptPart)
        assert history[-2].parts[0].content == "the original caller content"
        assert isinstance(history[-1], ModelResponse)
        assert isinstance(history[-1].parts[0], TextPart)
        assert history[-1].parts[0].content == long_reply
        # Target inbox topic.
        assert kwargs["topic"] == pc._AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id="bob")
        # Timeout matches the configured A2A budget so a high-effort
        # original call doesn't silently get a different deadline on
        # retry.
        assert kwargs["timeout"] == pc._timeout_seconds
        # temp_instructions present and contains the A2A roster intro
        # (the peer-roster builder uses ``channel=False`` for A2A).
        assert kwargs["temp_instructions"] is not None
        assert "private_chat" in kwargs["temp_instructions"]
        # deps carries the discord wire, the caller id, and the
        # phonebook propagated for further A2A chaining.
        assert "discord" in kwargs["deps"]
        assert kwargs["deps"]["caller_agent_id"] == "alice"
        assert "phonebook" in kwargs["deps"]
        # output_type is still ``str`` so the response shape stays
        # uniform between original and retry.
        assert kwargs["output_type"] is str

    async def test_retry_forwards_caller_ambient_deps(
        self,
        deps: dict[str, Any],
    ) -> None:
        """The retry path also projects the caller's ambient deps (e.g. the
        bridge-seeded memory-prompt template) forward, so a memory agent that
        hits a content-rejection retry still gets its memory instructions on the
        retry turn. Guards the spread at the second (retry) execute_node site —
        which the main-path projection test does not exercise."""
        deps["client"].execute_node = AsyncMock(
            side_effect=[_result("x" * 3000), _result("short")]
        )
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),
                _http_exc(discord.HTTPException, 400),
                _sent_message(99998),
            ]
        )
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("private_chat",)),
        ]
        await pc.private_chat(
            _ctx(
                caller="alice",
                phonebook=phonebook,
                extra_deps={"memory_prompt": "TEMPLATE {{MEMORY_DIR}}"},
            ),
            "bob",
            "the original caller content",
        )
        retry_deps = deps["client"].execute_node.await_args_list[1].kwargs["deps"]
        assert retry_deps["memory_prompt"] == "TEMPLATE {{MEMORY_DIR}}"
        # A2A-owned keys still override the forwarded copies on the retry too.
        assert retry_deps["caller_agent_id"] == "alice"

    async def test_retry_budget_exhausted_chunks_and_returns_full(
        self,
        deps: dict[str, Any],
    ) -> None:
        """All 3 attempts (1 + 2 retries) hit 400; orchestrator
        falls back to chunk-splitting the latest text into the audit
        thread; caller receives the FULL untruncated latest text."""
        long_reply = "x" * 5000  # forces chunk_split to produce >=3 chunks
        deps["client"].execute_node = AsyncMock(
            return_value=_result(long_reply)
        )
        # Side-effect plan: request OK; 3 response attempts all 400; then
        # N chunk-split sends (each OK).
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),  # request OK
                _http_exc(discord.HTTPException, 400),    # attempt 1
                _http_exc(discord.HTTPException, 400),    # attempt 2 (retry 1)
                _http_exc(discord.HTTPException, 400),    # attempt 3 (retry 2 → budget exhausted)
                _sent_message(1),
                _sent_message(2),
                _sent_message(3),
                _sent_message(4),  # cushion in case chunks > 3
            ]
        )
        returned = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Caller gets the FULL latest text (which here is the same as
        # the original since execute_node always returns long_reply).
        assert long_reply in returned
        # execute_node called 3 times: original + 2 retries.
        assert deps["client"].execute_node.await_count == 3

    async def test_retry_execute_node_failure_falls_back_to_chunks(
        self,
        deps: dict[str, Any],
    ) -> None:
        """If the retry RPC itself fails (timeout, broker error), don't
        propagate — chunk-split the current text and return it. The
        caller still gets a useful reply."""
        long_reply = "x" * 5000
        deps["client"].execute_node = AsyncMock(
            side_effect=[
                _result(long_reply),       # original response
                TimeoutError("retry hung"),  # retry RPC times out
            ]
        )
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),  # request OK
                _http_exc(discord.HTTPException, 400),    # response attempt 1 → 400
                _sent_message(1),                          # chunk 1
                _sent_message(2),                          # chunk 2
                _sent_message(3),                          # chunk 3 (cushion)
                _sent_message(4),
            ]
        )
        returned = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Caller still receives the original long reply.
        assert long_reply in returned

    async def test_retry_empty_response_treated_as_attempt(
        self,
        deps: dict[str, Any],
    ) -> None:
        """If the retry returns empty content, the orchestrator
        substitutes the empty-content placeholder and posts that on the
        next loop iteration. Empty content itself satisfies Discord's
        no-empty-content rule via the placeholder, so the empty retry
        simply burns one attempt slot."""
        long_reply = "x" * 3000
        deps["client"].execute_node = AsyncMock(
            side_effect=[
                _result(long_reply),
                _result(""),  # retry returns empty
            ]
        )
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(_REQUEST_SENT_MESSAGE_ID),  # request OK
                _http_exc(discord.HTTPException, 400),    # original 400
                _sent_message(99998),                      # retry placeholder OK
            ]
        )
        returned = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Returned text is empty (the retry's content); the tag is still
        # present.
        assert returned.startswith("<thread_id>")
        # Empty content was substituted with placeholder for the send,
        # but the returned value to the caller is the empty string.
        assert returned.endswith("</thread_id>\n")


class TestA2APostChunkedProjection:
    """Direct coverage for ``_post_chunked_projection`` — the audit-
    thread fallback the orchestrator invokes when retry budget is
    exhausted or the retry RPC itself fails. Mirrors the bridge's
    ``TestChunkedFallback`` / ``TestAllChunksFailSummary`` for the
    A2A side."""

    _PERSONA = SimpleNamespace(name="Bob Bot", avatar_url="https://example.com/bob.png")
    _CHANNEL_ID = 4242
    _THREAD_ID = 9090

    async def test_empty_text_logs_warning_and_returns_without_sending(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Empty text → ``chunk_split`` returns ``[]`` → log + early
        return; ``persona_sender.send`` never called. The orchestrator
        shouldn't reach this state in practice, but the guard prevents
        an empty audit-thread post."""
        import logging as _logging
        deps["persona_sender"].send = AsyncMock()
        with caplog.at_level(_logging.WARNING):
            await pc._post_chunked_projection(
                self._PERSONA, self._CHANNEL_ID, self._THREAD_ID,
                "", "alice", "bob",
            )
        assert deps["persona_sender"].send.await_count == 0
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "received empty text" in joined
        assert "caller=alice" in joined
        assert "target=bob" in joined

    async def test_short_text_posts_one_chunk_into_thread(
        self,
        deps: dict[str, Any],
    ) -> None:
        """Text under the chunk-size limit posts as a single send into
        the audit thread. Note: A2A posts use ``thread_id`` (NOT a
        reply_to anchor) — the bridge variant uses reply_to for the
        first chunk; A2A's audit thread is always a thread post."""
        deps["persona_sender"].send = AsyncMock(
            return_value=_sent_message(1, channel_id=self._CHANNEL_ID)
        )
        await pc._post_chunked_projection(
            self._PERSONA, self._CHANNEL_ID, self._THREAD_ID,
            "short reply", "alice", "bob",
        )
        assert deps["persona_sender"].send.await_count == 1
        kwargs = deps["persona_sender"].send.await_args.kwargs
        assert kwargs["thread_id"] == self._THREAD_ID
        assert kwargs["channel_id"] == self._CHANNEL_ID
        assert kwargs["content"] == "short reply"

    async def test_long_text_splits_and_posts_multiple_into_thread(
        self,
        deps: dict[str, Any],
    ) -> None:
        """Text over the chunk limit splits and posts each chunk as a
        separate thread post. All posts use ``thread_id`` — there's no
        reply-anchor distinction across chunks (unlike the bridge)."""
        deps["persona_sender"].send = AsyncMock(
            return_value=_sent_message(1, channel_id=self._CHANNEL_ID)
        )
        await pc._post_chunked_projection(
            self._PERSONA, self._CHANNEL_ID, self._THREAD_ID,
            "x" * 5000, "alice", "bob",
        )
        # 5000 chars / 1990 chunk size → at least 3 chunks
        assert deps["persona_sender"].send.await_count >= 3
        for call in deps["persona_sender"].send.await_args_list:
            assert call.kwargs["thread_id"] == self._THREAD_ID

    async def test_per_chunk_failure_continues_with_next(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One chunk failing must not abort the remaining sends — the
        partial-delivery guarantee preserves whatever the audit can
        get. Each per-chunk failure logs ERROR independently."""
        import logging as _logging
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(1, channel_id=self._CHANNEL_ID),
                _http_exc(discord.HTTPException, 400),  # chunk 2 fails
                _sent_message(3, channel_id=self._CHANNEL_ID),
                _sent_message(4, channel_id=self._CHANNEL_ID),  # cushion
            ]
        )
        with caplog.at_level(_logging.ERROR):
            await pc._post_chunked_projection(
                self._PERSONA, self._CHANNEL_ID, self._THREAD_ID,
                "x" * 5000, "alice", "bob",
            )
        # Function does NOT raise — partial loss is logged + tolerated.
        assert any(
            "chunk-split failed" in r.getMessage() and "status=400" in r.getMessage()
            for r in caplog.records
        )

    async def test_rate_limited_at_chunk_layer_is_swallowed(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The catch is ``discord.DiscordException`` (broader than
        ``HTTPException``) so :class:`RateLimited` at the chunk layer
        — the last resort — does not propagate. Nothing useful can
        route around chunk-fallback failures."""
        import logging as _logging
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _sent_message(1, channel_id=self._CHANNEL_ID),
                discord.RateLimited(retry_after=2.0),
                _sent_message(3, channel_id=self._CHANNEL_ID),
                _sent_message(4, channel_id=self._CHANNEL_ID),
            ]
        )
        with caplog.at_level(_logging.ERROR):
            # Must not raise.
            await pc._post_chunked_projection(
                self._PERSONA, self._CHANNEL_ID, self._THREAD_ID,
                "x" * 5000, "alice", "bob",
            )
        # The RateLimited has no ``.status``; status is logged as None.
        assert any(
            "chunk-split failed" in r.getMessage() and "status=None" in r.getMessage()
            for r in caplog.records
        )

    async def test_all_chunks_fail_logs_dominant_status_summary(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When every chunk fails, an aggregate WARNING is logged with
        the dominant status so operators see one actionable line
        instead of N per-chunk ERRORs. Mirrors the bridge's
        ``TestAllChunksFailSummary`` for parity."""
        import logging as _logging
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 500),
                _http_exc(discord.HTTPException, 500),
                _http_exc(discord.HTTPException, 502),
                _http_exc(discord.HTTPException, 500),  # cushion
            ]
        )
        with caplog.at_level(_logging.WARNING):
            await pc._post_chunked_projection(
                self._PERSONA, self._CHANNEL_ID, self._THREAD_ID,
                "x" * 5000, "alice", "bob",
            )
        warns = [r for r in caplog.records if r.levelno == _logging.WARNING]
        # The summary line names the dominant status (500 occurs twice).
        assert any(
            "delivered 0/" in r.getMessage() and "dominant_status=500" in r.getMessage()
            for r in warns
        )


class TestRetryFeedbackSharedSymbols:
    """Parity guard: both the bridge outbox and the A2A tool import the
    SAME symbols from ``discord.retry_feedback``. The whole refactor's
    load-bearing claim is "policy lives in one place"; a regression
    that re-introduced a local copy in either caller would silently
    drift over time. These identity assertions fail loud."""

    def test_bridge_imports_shared_symbols(self) -> None:
        from calfcord.bridge import outbox
        from calfcord.discord import retry_feedback
        assert outbox.classify_error is retry_feedback.classify_error
        assert outbox.build_retry_reminder is retry_feedback.build_retry_reminder
        assert outbox.build_retry_history is retry_feedback.build_retry_history
        assert outbox.chunk_split is retry_feedback.chunk_split
        assert outbox.MAX_REPLY_RETRY_ATTEMPTS is retry_feedback.MAX_REPLY_RETRY_ATTEMPTS
        assert outbox.NON_AGENT_FIXABLE_STATUSES is retry_feedback.NON_AGENT_FIXABLE_STATUSES

    def test_a2a_imports_shared_symbols(self) -> None:
        from calfcord.discord import retry_feedback
        from calfcord.tools.builtin import private_chat
        assert private_chat.classify_error is retry_feedback.classify_error
        assert private_chat.build_retry_reminder is retry_feedback.build_retry_reminder
        assert private_chat.build_retry_history is retry_feedback.build_retry_history
        assert private_chat.chunk_split is retry_feedback.chunk_split
        assert private_chat.MAX_REPLY_RETRY_ATTEMPTS is retry_feedback.MAX_REPLY_RETRY_ATTEMPTS
