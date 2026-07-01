"""Unit tests for :class:`calfcord.bridge.progress.ProgressRenderer`.

The renderer is driven exactly as the
:class:`~calfcord.bridge.mention_handler.MentionHandler` drives it: per-step
``on_step`` calls off the normalized ``StepEvent`` stream, with a ``finish`` in
the handler's ``finally``. Its sink is a single transient in-channel "progress"
message (the live trace itself — model text / ``tool_name(args)`` / ``⎿ result``
lines, no header) posted under the emitting agent's persona, edited (debounced)
as steps stream in, and deleted on ``finish``. There is no Kafka, no cursor, and
no DB here.

The tests exercise:

* lifecycle: post-once on the first renderable step, accumulate + debounced
  edit (coalescing a burst), ``finish`` cancels a pending edit + deletes, a
  turn with no renderable step posts nothing and ``finish`` no-ops;
* per-step persona resolution and thread routing;
* failure swallowing (a failed post leaves the id unset so the next step
  retries; edit/delete ``NotFound`` swallowed; ``RateLimited`` funnelled);
* the tail-window/clamp end-to-end; typing fired per step.

For debounce determinism, an autouse fixture collapses the debounce window to 0
and tests await ``entry.debounce_task``; the coalescing test instead gates the
sleep on an event so the task is provably parked across the burst. discord.py
and the LLM stack are mocked out; the repo runs ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import calfcord.bridge.progress as progress_mod
import calfcord.bridge.steps_render as steps_render
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.progress import ProgressEntry, ProgressRenderer
from calfcord.bridge.step_events import StepEvent
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage

_CORRELATION_ID = "evt-1"
_CHANNEL_ID = 6789
_MESSAGE_ID = 12345
_PROGRESS_MESSAGE_ID = 99999


@pytest.fixture(autouse=True)
def _zero_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the trailing-debounce window to 0 so a scheduled edit task is
    ready to run the instant we await it — never a real 1s sleep. The task is
    still scheduled (not synchronous); tests await ``entry.debounce_task``."""
    monkeypatch.setattr(progress_mod, "_PROGRESS_DEBOUNCE_SECONDS", 0.0)


@pytest.fixture
def persona_sender() -> AsyncMock:
    """REST-only persona sender. ``send`` returns a SentMessage carrying the
    progress message id; ``edit_message`` / ``delete_message`` are no-op
    AsyncMocks the tests assert against."""
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=SentMessage(id=_PROGRESS_MESSAGE_ID, channel_id=_CHANNEL_ID))
    sender.edit_message = AsyncMock(return_value=None)
    sender.delete_message = AsyncMock(return_value=None)
    return sender


def _req(*, channel_id: int = _CHANNEL_ID, source_channel_id: int = _CHANNEL_ID) -> MentionRequest:
    """A mention request. ``source_channel_id != channel_id`` represents a wire
    that originated inside a Discord thread (the renderer reads only these two)."""
    return MentionRequest(
        content="hello",
        mention_ids=("aksel",),
        author_label="alice",
        message_id=_MESSAGE_ID,
        source_channel_id=source_channel_id,
        channel_id=channel_id,
        wire=WireMessage(
            event_id="e1",
            kind="message",
            message_id=_MESSAGE_ID,
            channel_id=channel_id,
            source_channel_id=source_channel_id,
            guild_id=1,
            content="hello",
            author=WireAuthor(discord_user_id=1, display_name="alice", is_bot=False, is_webhook=False),
            created_at=datetime.now(UTC),
        ),
        reply_target=None,
    )


def _step(
    kind: str,
    *,
    emitter: str = "aksel",
    correlation_id: str = _CORRELATION_ID,
    text: str = "",
    name: str | None = None,
    args: dict[str, object] | None = None,
) -> StepEvent:
    return StepEvent(
        kind=kind,  # type: ignore[arg-type]
        correlation_id=correlation_id,
        depth=0,
        emitter=emitter,
        text=text,
        name=name,
        args=args,
    )


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


async def _drain_debounce(renderer: ProgressRenderer, correlation_id: str = _CORRELATION_ID) -> None:
    """Await the entry's pending debounce task so its (zero-delay) edit fires
    deterministically. No-op if the entry is gone or has no live task."""
    entry = renderer._entries.get(correlation_id)
    if entry is not None and entry.debounce_task is not None:
        await entry.debounce_task


class TestProgressPost:
    """The first renderable step posts the transient progress message exactly
    once, under the step's emitter persona, plain (no reply/thread/buttons)."""

    async def test_first_renderable_step_posts_once_under_emitter_persona(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="Let me check."), _req())

        assert persona_sender.send.await_count == 1
        persona_sender.edit_message.assert_not_called()
        persona_sender.delete_message.assert_not_called()

        call = persona_sender.send.call_args
        assert call.kwargs["channel_id"] == _CHANNEL_ID
        # The message IS the live trace — just the model's interim text here.
        assert call.kwargs["content"] == "Let me check."
        assert call.kwargs["persona"].name == "aksel"
        assert call.kwargs["thread_id"] is None
        # Plain send: no reply, no extra buttons.
        assert call.kwargs.get("reply_to") is None
        assert call.kwargs.get("extra_buttons") is None

        entry = renderer._entries[_CORRELATION_ID]
        assert entry.progress_message_id == _PROGRESS_MESSAGE_ID
        assert entry.rendered_lines == ["Let me check."]

    async def test_tool_call_empty_args_posts_name_parens(self, persona_sender: AsyncMock) -> None:
        """A tool call whose args the seam coerced to ``{}`` (or ``None``)
        renders as ``name()`` — the dict-only formatter, no raw-JSON fallback."""
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="f", args={}), _req())
        assert persona_sender.send.call_args.kwargs["content"] == "`f()`"

    async def test_post_persona_is_each_posting_steps_emitter(self, persona_sender: AsyncMock) -> None:
        """Persona is resolved per step from ``persona_for(step.emitter)`` — two
        correlations whose first renderable steps are emitted by different agents
        post under different personas (nothing is cached at seed time)."""
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="x", emitter="codex", correlation_id="corr-a"), _req())
        await renderer.on_step(_step("agent_message", text="y", emitter="conan", correlation_id="corr-b"), _req())
        personas = [c.kwargs["persona"].name for c in persona_sender.send.call_args_list]
        assert personas == ["codex", "conan"]


class TestNothingRenderable:
    """A step (or whole turn) that renders nothing posts no message; finish
    then has nothing to delete."""

    async def test_empty_step_seeds_entry_but_posts_nothing(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        # A whitespace-only preamble renders to None.
        await renderer.on_step(_step("agent_message", text="   \n  "), _req())
        persona_sender.send.assert_not_called()
        # The entry IS seeded (typing routing needs it) but never posts.
        entry = renderer._entries[_CORRELATION_ID]
        assert entry.progress_message_id is None
        assert entry.rendered_lines == []

        await renderer.finish(_CORRELATION_ID)
        persona_sender.delete_message.assert_not_called()
        assert _CORRELATION_ID not in renderer._entries

    async def test_finish_without_any_step_is_noop(self, persona_sender: AsyncMock) -> None:
        """A pure-text turn produces ZERO StepEvents, so on_step is never called;
        finish in the handler's finally must no-op cleanly."""
        renderer = ProgressRenderer(persona_sender)
        await renderer.finish("never-seen")
        persona_sender.send.assert_not_called()
        persona_sender.delete_message.assert_not_called()


class TestDebouncedEdit:
    """Subsequent renderable steps accumulate lines and edit (debounced) — they
    never post a second message."""

    async def test_second_step_edits_with_accumulated_trace(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        # Step 1: a tool call → posts the call line.
        await renderer.on_step(_step("tool_call", name="t", args={"x": 1}), _req())
        assert persona_sender.send.await_count == 1
        # Step 2: the tool result → debounced edit appends the return line.
        await renderer.on_step(_step("tool_result", text="result"), _req())
        assert persona_sender.send.await_count == 1  # no second post

        await _drain_debounce(renderer)
        persona_sender.edit_message.assert_awaited_once()
        edit_call = persona_sender.edit_message.call_args
        assert edit_call.args[0] == _CHANNEL_ID
        assert edit_call.args[1] == _PROGRESS_MESSAGE_ID
        assert edit_call.kwargs["content"] == "`t(x=1)`\n```\n⎿ result\n```"
        assert renderer._entries[_CORRELATION_ID].rendered_lines == ["`t(x=1)`", "```\n⎿ result\n```"]

    async def test_burst_coalesces_to_one_edit_at_latest_trace(
        self, persona_sender: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two edit-triggering steps while a debounce task is still pending
        coalesce into a single edit that reflects the LATEST trace (lines are
        read at FIRE time, not schedule time). The sleep is gated on an event so
        the task is provably parked across the burst — no reliance on event-loop
        scheduling of a zero-delay sleep."""
        gate = asyncio.Event()

        async def _gated_sleep(_seconds: float) -> None:
            await gate.wait()

        monkeypatch.setattr(progress_mod.asyncio, "sleep", _gated_sleep)

        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="a"), _req())  # posts
        await renderer.on_step(_step("agent_message", text="b"), _req())  # schedules debounce, parks
        first_task = renderer._entries[_CORRELATION_ID].debounce_task
        assert first_task is not None

        # A third step while the parked task is pending must reuse it, not spawn a second.
        await renderer.on_step(_step("agent_message", text="c"), _req())
        assert renderer._entries[_CORRELATION_ID].debounce_task is first_task

        gate.set()
        await _drain_debounce(renderer)

        persona_sender.edit_message.assert_awaited_once()
        assert persona_sender.edit_message.call_args.kwargs["content"] == "a\nb\nc"
        assert renderer._entries[_CORRELATION_ID].rendered_lines == ["a", "b", "c"]
        assert persona_sender.send.await_count == 1  # still only one post


class TestFinish:
    """``finish`` cancels a pending debounce and deletes the progress message,
    then drops the entry — on success and on fault (it runs in a finally)."""

    async def test_finish_deletes_posted_progress_message(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="t", args={}), _req())
        await renderer.finish(_CORRELATION_ID)
        persona_sender.delete_message.assert_awaited_once_with(_CHANNEL_ID, _PROGRESS_MESSAGE_ID, thread_id=None)
        assert _CORRELATION_ID not in renderer._entries

    async def test_finish_cancels_pending_debounce_then_deletes(
        self, persona_sender: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin a long window so the scheduled task is GUARANTEED still pending
        # (never reaching edit_message) when finish cancels it.
        monkeypatch.setattr(progress_mod, "_PROGRESS_DEBOUNCE_SECONDS", 3600.0)
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="a"), _req())  # posts
        await renderer.on_step(_step("agent_message", text="b"), _req())  # schedules debounce
        pending = renderer._entries[_CORRELATION_ID].debounce_task
        assert pending is not None and not pending.done()

        await renderer.finish(_CORRELATION_ID)
        assert pending.cancelled()
        persona_sender.edit_message.assert_not_called()
        persona_sender.delete_message.assert_awaited_once_with(_CHANNEL_ID, _PROGRESS_MESSAGE_ID, thread_id=None)
        assert _CORRELATION_ID not in renderer._entries


class TestThreadRouting:
    """A thread-originated request posts/edits/deletes INTO the thread; the
    persona webhook still hosts on the parent channel."""

    async def test_thread_originated_request_posts_and_deletes_in_thread(self, persona_sender: AsyncMock) -> None:
        thread_id = 555_001
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="t", args={}), _req(source_channel_id=thread_id))

        persona_sender.send.assert_awaited_once()
        kwargs = persona_sender.send.call_args.kwargs
        assert kwargs["channel_id"] == _CHANNEL_ID  # webhook host = parent
        assert kwargs["thread_id"] == thread_id  # routed into the thread
        assert renderer._entries[_CORRELATION_ID].thread_id == thread_id

        await renderer.finish(_CORRELATION_ID)
        persona_sender.delete_message.assert_awaited_once_with(_CHANNEL_ID, _PROGRESS_MESSAGE_ID, thread_id=thread_id)


class TestFailureSwallowing:
    """Discord failures on the progress surface must never escape the renderer."""

    async def test_post_failure_leaves_id_none_and_next_step_retries(self, persona_sender: AsyncMock) -> None:
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.Forbidden, 403),
                SentMessage(id=_PROGRESS_MESSAGE_ID, channel_id=_CHANNEL_ID),
            ]
        )
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="a"), _req())
        entry = renderer._entries[_CORRELATION_ID]
        # Post failed and was swallowed: id stays None, the line still accumulated.
        assert entry.progress_message_id is None
        assert entry.rendered_lines == ["a"]

        # Next renderable step retries the POST (id still None ⇒ not an edit).
        await renderer.on_step(_step("agent_message", text="b"), _req())
        assert persona_sender.send.await_count == 2
        persona_sender.edit_message.assert_not_called()
        assert entry.progress_message_id == _PROGRESS_MESSAGE_ID
        assert persona_sender.send.call_args.kwargs["content"] == "a\nb"

    async def test_rate_limited_on_post_is_swallowed(self, persona_sender: AsyncMock) -> None:
        """``discord.RateLimited`` is a ``DiscordException`` but NOT an
        ``HTTPException`` — the broader catch must funnel it through."""
        persona_sender.send = AsyncMock(side_effect=discord.RateLimited(retry_after=1.0))
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="hi"), _req())
        entry = renderer._entries[_CORRELATION_ID]
        assert entry.progress_message_id is None
        assert entry.rendered_lines == ["hi"]

    async def test_forbidden_on_post_is_logged_and_swallowed(
        self, persona_sender: AsyncMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
        renderer = ProgressRenderer(persona_sender)
        with caplog.at_level(logging.WARNING):
            await renderer.on_step(_step("agent_message", text="hi"), _req())
        assert renderer._entries[_CORRELATION_ID].progress_message_id is None
        assert any("Forbidden" in r.message for r in caplog.records)

    async def test_notfound_on_edit_is_swallowed(self, persona_sender: AsyncMock) -> None:
        persona_sender.edit_message = AsyncMock(side_effect=_http_exc(discord.NotFound, 404))
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("agent_message", text="a"), _req())  # post
        await renderer.on_step(_step("agent_message", text="b"), _req())  # schedule edit → NotFound
        # Draining the task must not raise.
        await _drain_debounce(renderer)
        persona_sender.edit_message.assert_awaited_once()

    async def test_notfound_on_delete_is_swallowed(self, persona_sender: AsyncMock) -> None:
        persona_sender.delete_message = AsyncMock(side_effect=_http_exc(discord.NotFound, 404))
        renderer = ProgressRenderer(persona_sender)
        await renderer.on_step(_step("tool_call", name="t", args={}), _req())  # post
        await renderer.finish(_CORRELATION_ID)  # delete → NotFound, swallowed
        persona_sender.delete_message.assert_awaited_once()
        assert _CORRELATION_ID not in renderer._entries


class TestProgressTruncation:
    """End-to-end: a turn that accumulates more trace than fits the tail-window
    budget edits content that stays under Discord's hard cap and carries the
    elision marker, with the most recent steps kept and the oldest dropped."""

    async def test_long_trace_is_tail_windowed_in_edited_content(
        self, persona_sender: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Gate the debounce so no edit fires mid-burst; one edit at the end then
        # renders the full accumulated trace through the tail window.
        gate = asyncio.Event()

        async def _gated_sleep(_seconds: float) -> None:
            await gate.wait()

        monkeypatch.setattr(progress_mod.asyncio, "sleep", _gated_sleep)

        renderer = ProgressRenderer(persona_sender)
        # 30 tool calls, each rendering to a long (per-part-capped) line — well
        # over _PROGRESS_BODY_MAX_CHARS combined. The "i=<n>" prefix survives
        # per-part truncation (it sits at the front of the args).
        for n in range(30):
            await renderer.on_step(_step("tool_call", name="call", args={"i": n, "pad": "x" * 200}), _req())

        gate.set()
        await _drain_debounce(renderer)

        content = persona_sender.edit_message.call_args.kwargs["content"]
        # Never exceeds Discord's hard cap, and the elision is announced.
        assert len(content) <= steps_render._DISCORD_MESSAGE_LIMIT
        assert content.startswith(steps_render._HIDDEN_STEPS_MARKER)
        # Most recent step kept; oldest dropped.
        assert "i=29," in content
        assert "i=0," not in content


class TestTyping:
    """Typing is fired (best-effort, fire-and-forget) for EVERY step — even one
    that renders nothing — into the thread the conversation lives in, else the
    parent channel. ``fire`` is synchronous, so a plain MagicMock records it."""

    async def test_renderable_step_fires_typing_at_channel(self, persona_sender: AsyncMock) -> None:
        notifier = MagicMock()
        renderer = ProgressRenderer(persona_sender, notifier)
        await renderer.on_step(_step("tool_call", name="t", args={}), _req())
        notifier.fire.assert_called_once_with(_CHANNEL_ID)

    async def test_empty_step_still_fires_typing(self, persona_sender: AsyncMock) -> None:
        """Typing reflects that work happened, independent of whether the step
        renders anything visible: a whitespace-only model turn still fires."""
        notifier = MagicMock()
        renderer = ProgressRenderer(persona_sender, notifier)
        await renderer.on_step(_step("agent_message", text="   "), _req())  # renders None
        notifier.fire.assert_called_once_with(_CHANNEL_ID)
        persona_sender.send.assert_not_called()  # nothing renderable → no post

    async def test_thread_originated_step_fires_into_thread(self, persona_sender: AsyncMock) -> None:
        thread_id = 555
        notifier = MagicMock()
        renderer = ProgressRenderer(persona_sender, notifier)
        await renderer.on_step(_step("tool_call", name="t", args={}), _req(source_channel_id=thread_id))
        notifier.fire.assert_called_once_with(thread_id)

    async def test_no_notifier_is_fine(self, persona_sender: AsyncMock) -> None:
        renderer = ProgressRenderer(persona_sender)  # typing_notifier defaults to None
        await renderer.on_step(_step("tool_call", name="t", args={}), _req())
        persona_sender.send.assert_awaited_once()


class TestProgressEntry:
    """The collapsed per-correlation dataclass (replaces the old StepsEntry)."""

    def test_defaults(self) -> None:
        e = ProgressEntry(channel_id=1, thread_id=None)
        assert e.progress_message_id is None
        assert e.rendered_lines == []
        assert e.debounce_task is None

    def test_is_mutable(self) -> None:
        e = ProgressEntry(channel_id=1, thread_id=2)
        e.progress_message_id = 999
        e.rendered_lines.append("step")
        assert e.progress_message_id == 999
        assert e.rendered_lines == ["step"]
