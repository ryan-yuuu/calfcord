"""Unit tests for the steps consumer built by ``build_steps_consumer``.

Drives ``ConsumerNodeDef.handler`` directly with synthetic ``Envelope``s
that carry a hand-rolled ``state.message_history`` representing the
agent's running pydantic_ai conversation. The consumer's sink is a single
transient in-channel "progress" message (the live trace itself — model text /
``tool_name(args)`` / ``⎿ result`` lines, no header) posted
under the agent persona, edited (debounced) as steps stream in, and
deleted on the terminal hop. There is NO database access in this phase.

The tests exercise:

* the preserved per-hop invariants, re-expressed against the progress
  sink: cursor monotonicity, per-hop persona resolution, the no-delta
  early skip, outbox-retry dedup, and thread-originated-wire routing
  (progress posts into the thread, not the parent);
* the progress lifecycle: post-once-on-first-renderable-hop, the trace
  accumulates across hops, debounced edit reflects the latest lines,
  terminal cancels debounce + deletes, pure-text turns post nothing;
* failure swallowing (Discord errors on send/edit/delete, exceptions
  inside ``_render_live_delta``);
* the compact live renderer itself: Hybrid prose + an inline-code
  ``tool_name(args)`` call line + a short fenced ``⎿`` result block (the
  first few lines), keyword-arg formatting, single/triple-backtick handling,
  the per-line/line-count result caps, and the tail-window cap (see
  ``TestLiveRender``).

For debounce, tests monkeypatch ``_PROGRESS_DEBOUNCE_SECONDS`` to 0 and
await ``entry.debounce_task`` rather than sleeping — no real-time waits.

discord.py, Kafka, FastStream, and the LLM stack are all mocked out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from calfkit.models import State
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    Deps,
    SessionRunContext,
    WorkflowState,
)

import calfkit_organization.bridge.steps as steps_mod
from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.pending_wires import PendingWires, make_pending_entry
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.steps import build_steps_consumer
from calfkit_organization.bridge.steps_state import StepsEntry, StepsState
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.messages import SentMessage

_CORRELATION_ID = "evt-1"
_CHANNEL_ID = 6789
_MESSAGE_ID = 12345
_PROGRESS_MESSAGE_ID = 99999


@pytest.fixture(autouse=True)
def _zero_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the trailing-debounce window to 0 so the scheduled edit
    task is ready to run the instant we await it — never a real 1s sleep.
    The task is still scheduled (not synchronous); tests await
    ``entry.debounce_task`` to drive it."""
    monkeypatch.setattr(steps_mod, "_PROGRESS_DEBOUNCE_SECONDS", 0.0)


def _wire(*, source_channel_id: int | None = None) -> WireMessage:
    """Build a synthetic wire. ``source_channel_id`` defaults to None
    (non-thread). Set to a different value than ``_CHANNEL_ID`` to
    represent a wire that originated inside a Discord thread."""
    return WireMessage(
        event_id=_CORRELATION_ID,
        kind="message",
        slash_target=None,
        message_id=_MESSAGE_ID,
        channel_id=_CHANNEL_ID,
        source_channel_id=source_channel_id,
        guild_id=4242,
        content="hello",
        author=WireAuthor(
            discord_user_id=111,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
        ),
        created_at=datetime.now(UTC),
    )


def _registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                display_name="Aksel",
                description="Calendar.",
                avatar_url="https://example.com/aksel.png",
                system_prompt="Test scheduler.",
            ),
        ]
    )


def _two_agent_registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="codex",
                display_name="Codex",
                description="Coder.",
                avatar_url="https://example.com/codex.png",
                system_prompt="A.",
            ),
            AgentDefinition(
                agent_id="conan",
                display_name="Conan",
                description="Detective.",
                avatar_url="https://example.com/conan.png",
                system_prompt="B.",
            ),
        ]
    )


def _envelope(
    *,
    correlation_id: str = _CORRELATION_ID,
    message_history: Sequence[ModelMessage] = (),
    final_text: str | None = None,
) -> Envelope:
    state = State()
    state.message_history = list(message_history)
    if final_text is not None:
        from calfkit.models import TextPart as CalfkitTextPart

        state.final_output_parts = [CalfkitTextPart(text=final_text)]
    call_stack = CallFrameStack()
    call_stack.push(
        CallFrame(
            target_topic="agent.steps",
            callback_topic="discord.outbox",
        )
    )
    return Envelope(
        internal_workflow_state=WorkflowState(call_stack=call_stack),
        context=SessionRunContext(
            state=state,
            deps=Deps(correlation_id=correlation_id, provided_deps={}),
        ),
    )


def _headers(*, emitter: str | None = "scheduler", emitter_kind: str | None = "agent") -> dict[str, Any]:
    h: dict[str, Any] = {}
    if emitter is not None:
        h["x-calf-emitter"] = emitter
    if emitter_kind is not None:
        h["x-calf-emitter-kind"] = emitter_kind
    return h


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


@pytest.fixture
def persona_sender() -> AsyncMock:
    """REST-only persona sender. ``send`` returns a SentMessage carrying
    the progress message id; ``edit_message`` / ``delete_message`` are
    no-op AsyncMocks the tests assert against."""
    sender = AsyncMock()
    sender.send = AsyncMock(
        return_value=SentMessage(id=_PROGRESS_MESSAGE_ID, channel_id=_CHANNEL_ID),
    )
    sender.edit_message = AsyncMock(return_value=None)
    sender.delete_message = AsyncMock(return_value=None)
    return sender


@pytest.fixture
def pending_wires() -> PendingWires:
    pw = PendingWires()
    pw.put(_CORRELATION_ID, make_pending_entry(_wire()))
    return pw


@pytest.fixture
def steps_state() -> StepsState:
    return StepsState()


@pytest.fixture
def broker() -> Any:
    return AsyncMock()


async def _drain_debounce(steps_state: StepsState) -> None:
    """Await the entry's pending debounce task so its (zero-delay) edit
    fires deterministically. No-op if no entry or no live task."""
    entry = steps_state.get(_CORRELATION_ID)
    if entry is not None and entry.debounce_task is not None:
        await entry.debounce_task


class TestProgressPost:
    """First renderable hop posts the transient progress message exactly
    once, under the agent persona, plain (no reply/thread/buttons)."""

    async def test_first_renderable_hop_posts_progress_once(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        history = [
            ModelRequest(parts=[UserPromptPart(content="what's the weather?")]),
            ModelResponse(parts=[TextPart(content="Let me check.")]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # Posted exactly once; never edited/deleted on the first hop.
        assert persona_sender.send.await_count == 1
        persona_sender.edit_message.assert_not_called()
        persona_sender.delete_message.assert_not_called()

        call = persona_sender.send.call_args
        assert call.kwargs["channel_id"] == _CHANNEL_ID
        # The message IS the live trace — just the model's interim text here,
        # no header/counter line.
        assert call.kwargs["content"] == "Let me check."
        assert call.kwargs["persona"].name == "Aksel"
        # Plain send: no reply, no thread, no extra buttons.
        assert call.kwargs.get("reply_to") is None
        assert call.kwargs.get("thread_id") is None
        assert call.kwargs.get("extra_buttons") is None

        entry = steps_state.get(_CORRELATION_ID)
        assert entry is not None
        assert entry.progress_message_id == _PROGRESS_MESSAGE_ID
        assert len(entry.rendered_lines) == 1
        # Cursor advanced past the whole delta.
        assert entry.history_cursor == len(history)

    async def test_multi_part_first_hop_counts_all_parts(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """One ModelResponse with preamble text + a tool call renders BOTH
        parts into the first progress post: the prose line and the tool-call
        line (the live trace has no counter; the message IS the trace)."""
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        history = [
            ModelRequest(parts=[UserPromptPart(content="lookup tokyo")]),
            ModelResponse(
                parts=[
                    TextPart(content="Let me look that up."),
                    ToolCallPart(
                        tool_name="weather_lookup",
                        args={"city": "Tokyo"},
                        tool_call_id="tc-1",
                    ),
                ]
            ),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert persona_sender.send.await_count == 1
        # Preamble prose, then the tool call as an inline-code keyword line.
        assert persona_sender.send.call_args.kwargs["content"] == (
            'Let me look that up.\n`weather_lookup(city="Tokyo")`'
        )
        assert len(steps_state.get(_CORRELATION_ID).rendered_lines) == 2

    async def test_whitespace_only_text_not_counted(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """A whitespace-only TextPart alongside a tool call contributes no
        step — only the tool call counts."""
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        history = [
            ModelResponse(
                parts=[
                    TextPart(content="   \n  "),
                    ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                ]
            ),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Whitespace-only text was skipped; only the empty-args tool call
        # rendered, as `t()`.
        assert persona_sender.send.call_args.kwargs["content"] == "`t()`"

    async def test_renderable_part_types_each_render_a_live_line(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """TextPart, ToolCallPart, and ToolReturnPart each render into the live
        progress content (prose, the call line, the fenced ⎿ block); prompts do
        not. (The live trace has no step counter — that lives on the ⤵ view.)"""
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        history = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="system."),
                    UserPromptPart(content="user message"),
                ]
            ),
            ModelResponse(
                parts=[
                    TextPart(content="thinking"),
                    ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="t", content="ok", tool_call_id="x"),
                ]
            ),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # text + tool call + tool return (prompts excluded): prose line, the
        # call as an inline-code line, the return as a fenced ``⎿`` block.
        assert persona_sender.send.call_args.kwargs["content"] == "thinking\n`t()`\n```\n⎿ ok\n```"


class TestPureText:
    """A turn that never produces a renderable part posts no progress
    message and has nothing to delete."""

    async def test_only_prompts_posts_nothing(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        history = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="system."),
                    UserPromptPart(content="user message"),
                ]
            ),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.send.assert_not_called()
        persona_sender.edit_message.assert_not_called()
        persona_sender.delete_message.assert_not_called()
        # Entry IS seeded (the delta is non-empty; only the render is
        # empty) so a later hop's tool call doesn't re-walk the prompts.
        entry = steps_state.get(_CORRELATION_ID)
        assert entry is not None
        assert entry.history_cursor == len(history)
        assert entry.progress_message_id is None
        assert len(entry.rendered_lines) == 0

    async def test_pure_text_terminal_posts_and_deletes_nothing(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """Terminal-first pure-text reply: no progress message was ever
        posted, so the terminal hop deletes nothing but still marks the
        correlation completed (outbox-retry guard)."""
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="hello"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.send.assert_not_called()
        persona_sender.delete_message.assert_not_called()
        assert steps_state.is_completed(_CORRELATION_ID)


class TestDebouncedEdit:
    """Subsequent renderable hops accumulate lines and edit (debounced) —
    they never post a second message."""

    async def test_second_hop_edits_with_accumulated_trace(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        # Hop 1: a tool call → posts the call line.
        hop1 = [
            ModelRequest(parts=[UserPromptPart(content="lookup tokyo")]),
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="t", args={"x": 1}, tool_call_id="t1"),
                ]
            ),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=hop1),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert persona_sender.send.await_count == 1

        # Hop 2: the tool return arrived → debounced edit appends the return line.
        hop2 = [
            *hop1,
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="t", content="result", tool_call_id="t1"),
                ]
            ),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=hop2),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # No second post.
        assert persona_sender.send.await_count == 1
        # An edit task was scheduled; drive it.
        await _drain_debounce(steps_state)
        persona_sender.edit_message.assert_awaited_once()
        edit_call = persona_sender.edit_message.call_args
        assert edit_call.args[0] == _CHANNEL_ID
        assert edit_call.args[1] == _PROGRESS_MESSAGE_ID
        assert edit_call.kwargs["content"] == "`t(x=1)`\n```\n⎿ result\n```"
        assert len(steps_state.get(_CORRELATION_ID).rendered_lines) == 2

    async def test_burst_coalesces_to_one_edit_at_latest_count(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two edit-triggering hops while a debounce task is still pending
        coalesce into a single edit that reflects the LATEST trace (the lines
        appended during the window are read at FIRE time, not schedule time).

        Determinism: the debounce ``sleep`` is replaced by an event we
        control, so the task is guaranteed to remain parked in its wait
        (trace NOT yet rendered) across both hops. Releasing the gate then
        fires exactly one edit, which re-renders ``rendered_lines`` at that
        instant — by which point hop 3 has appended its line. No reliance on
        event-loop scheduling of a zero-delay sleep."""
        gate = asyncio.Event()

        async def _gated_sleep(_seconds: float) -> None:
            await gate.wait()

        monkeypatch.setattr(steps_mod.asyncio, "sleep", _gated_sleep)

        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        # Hop 1 posts (line "a").
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Hop 2 appends line "b" and schedules a debounce task. It parks in
        # the gated sleep — pending, not done, trace NOT yet re-rendered.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                    ModelResponse(parts=[TextPart(content="b")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        first_task = steps_state.get(_CORRELATION_ID).debounce_task
        assert first_task is not None

        # Hop 3 appends line "c" while the (parked) task is still pending;
        # it must reuse the SAME pending task, not spawn a second.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                    ModelResponse(parts=[TextPart(content="b")]),
                    ModelResponse(parts=[TextPart(content="c")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert steps_state.get(_CORRELATION_ID).debounce_task is first_task

        # Release the gate so the single task wakes and edits NOW, re-rendering
        # the latest trace (a/b/c).
        gate.set()
        await _drain_debounce(steps_state)

        persona_sender.edit_message.assert_awaited_once()
        assert persona_sender.edit_message.call_args.kwargs["content"] == "a\nb\nc"
        assert len(steps_state.get(_CORRELATION_ID).rendered_lines) == 3
        # Still only one post.
        assert persona_sender.send.await_count == 1


class TestTerminalHop:
    """Terminal hop cancels a pending debounce and deletes the progress
    message, then marks completion."""

    async def test_terminal_cancels_debounce_and_deletes(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        # Seed an entry as if a prior renderable hop posted the progress.
        steps_state.put(
            _CORRELATION_ID,
            StepsEntry(
                parent_channel_id=_CHANNEL_ID,
                parent_message_id=_MESSAGE_ID,
                progress_message_id=_PROGRESS_MESSAGE_ID,
                history_cursor=2,
            ),
        )
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        # Terminal envelope: history grew by a tool return + the final
        # ModelResponse answer (the latter is dropped by the [:-1] slice).
        history = [
            ModelRequest(parts=[UserPromptPart(content="lookup tokyo")]),
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1"),
                ]
            ),
            ModelResponse(parts=[TextPart(content="It's 18 degrees in Tokyo.")]),
        ]
        await consumer.handler(
            envelope=_envelope(
                message_history=history,
                final_text="It's 18 degrees in Tokyo.",
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # The progress message was deleted; the final answer text was
        # never posted by this consumer.
        persona_sender.delete_message.assert_awaited_once_with(
            _CHANNEL_ID,
            _PROGRESS_MESSAGE_ID,
            thread_id=None,
        )
        # Entry popped + marked completed.
        assert steps_state.get(_CORRELATION_ID) is None
        assert steps_state.is_completed(_CORRELATION_ID)

    async def test_terminal_cancels_pending_debounce_task(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If a debounce edit is still pending when the terminal hop
        arrives, it is cancelled (and awaited) — no edit lands after the
        delete, and the message is still deleted."""
        # Pin a long debounce window (overriding the autouse 0) so the
        # scheduled task is GUARANTEED to be sleeping — never reaching
        # edit_message — when the terminal hop cancels it. This makes the
        # "still pending" + "cancelled, not fired" assertions deterministic
        # rather than dependent on event-loop scheduling of sleep(0). The
        # test never actually waits the window: the task is cancelled.
        monkeypatch.setattr(steps_mod, "_PROGRESS_DEBOUNCE_SECONDS", 3600.0)
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        # Hop 1 posts.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Hop 2 schedules a debounce edit — leave it pending.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                    ModelResponse(parts=[TextPart(content="b")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        pending_task = steps_state.get(_CORRELATION_ID).debounce_task
        assert pending_task is not None and not pending_task.done()

        # Terminal hop: must cancel the pending edit then delete.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                    ModelResponse(parts=[TextPart(content="b")]),
                    ModelResponse(parts=[TextPart(content="final answer")]),
                ],
                final_text="final answer",
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert pending_task.cancelled()
        persona_sender.edit_message.assert_not_called()
        persona_sender.delete_message.assert_awaited_once_with(
            _CHANNEL_ID,
            _PROGRESS_MESSAGE_ID,
            thread_id=None,
        )
        assert steps_state.is_completed(_CORRELATION_ID)

    async def test_terminal_without_progress_message_deletes_nothing(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """An entry that never posted a progress message (e.g. all prior
        hops were pure-text) has nothing to delete on terminal."""
        steps_state.put(
            _CORRELATION_ID,
            StepsEntry(
                parent_channel_id=_CHANNEL_ID,
                parent_message_id=_MESSAGE_ID,
                history_cursor=1,
            ),
        )
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="done"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.delete_message.assert_not_called()
        assert steps_state.get(_CORRELATION_ID) is None
        assert steps_state.is_completed(_CORRELATION_ID)

    async def test_terminal_first_hop_does_not_post_then_delete(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """A single-pass turn whose FIRST renderable hop is ALSO terminal must
        not post a progress message it would immediately delete (no channel
        flash, no wasted webhook call). The terminal hop renders nothing into
        the live message — the final answer is the outbox's job, and the full
        steps live on the ⤵ transcript."""
        consumer = build_steps_consumer(persona_sender, _registry(), pending_wires, steps_state)
        # Terminal envelope carrying a tool call + return in its delta (the
        # trailing final-answer ModelResponse is dropped by [:-1]); no prior
        # hop ever posted a progress message for this correlation.
        history = [
            ModelRequest(parts=[UserPromptPart(content="weather?")]),
            ModelResponse(parts=[ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1")]),
            ModelResponse(parts=[TextPart(content="It's 18 in Tokyo.")]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history, final_text="It's 18 in Tokyo."),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Never posted ⇒ never deleted (no flash); completion still recorded.
        persona_sender.send.assert_not_called()
        persona_sender.delete_message.assert_not_called()
        assert steps_state.is_completed(_CORRELATION_ID)


class TestTerminalCompletionMarking:
    """The terminal hop must always record completion so an outbox retry
    cannot seed a fresh progress message — across every terminal path."""

    async def test_marks_completed_when_no_pending_wire(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            PendingWires(),
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="hello"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert steps_state.is_completed(_CORRELATION_ID)
        persona_sender.delete_message.assert_not_called()

    async def test_marks_completed_when_wire_is_thread_originated(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        pw = PendingWires()
        pw.put(_CORRELATION_ID, make_pending_entry(_wire(source_channel_id=999999)))
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pw,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="hello"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert steps_state.is_completed(_CORRELATION_ID)
        persona_sender.delete_message.assert_not_called()


class TestSkipPaths:
    async def test_no_pending_wire_skips_silently(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            PendingWires(),
            steps_state,
        )
        with caplog.at_level(logging.DEBUG):
            await consumer.handler(
                envelope=_envelope(
                    message_history=[
                        ModelResponse(parts=[TextPart(content="hi")]),
                    ]
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        persona_sender.send.assert_not_called()
        assert any("no pending wire" in r.message for r in caplog.records)

    async def test_non_agent_emitter_kind_skips(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="hi")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter_kind="tool"),
            broker=broker,
        )
        persona_sender.send.assert_not_called()

    async def test_missing_emitter_id_skips(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="hi")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter=None),
            broker=broker,
        )
        persona_sender.send.assert_not_called()

    async def test_unknown_emitter_in_registry_skips(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        empty_registry = AgentRegistry([])
        consumer = build_steps_consumer(
            persona_sender,
            empty_registry,
            pending_wires,
            steps_state,
        )
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(
                    message_history=[
                        ModelResponse(parts=[TextPart(content="hi")]),
                    ]
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        persona_sender.send.assert_not_called()
        assert any("unknown emitter" in r.message for r in caplog.records)


class TestThreadOriginatedWire:
    """When the inbound wire originated inside a Discord thread, the progress
    surface posts INTO that thread — identical behavior to a top-level
    channel. The persona webhook still hosts on the parent ``channel_id``;
    ``thread_id`` routes the progress message into the thread."""

    async def test_thread_originated_wire_posts_progress_into_thread(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        thread_id = 999999
        pw = PendingWires()
        # source_channel_id != channel_id => message came from a thread.
        pw.put(_CORRELATION_ID, make_pending_entry(_wire(source_channel_id=thread_id)))
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pw,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(
                        parts=[
                            ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                        ]
                    ),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Progress posted: webhook hosts on the parent channel, thread_id
        # routes it into the thread.
        persona_sender.send.assert_awaited_once()
        kwargs = persona_sender.send.call_args.kwargs
        assert kwargs["channel_id"] == _CHANNEL_ID
        assert kwargs["thread_id"] == thread_id
        # The entry was seeded carrying the thread id.
        entry = steps_state.get(_CORRELATION_ID)
        assert entry is not None
        assert entry.thread_id == thread_id


class TestInitialCursorSeed:
    """The cursor must skip the projected channel-history prefix that
    ``BridgeIngress`` passes into ``invoke_node`` — otherwise the agent's
    prior channel replies (rendered by ``project_history`` as
    ``ModelResponse(TextPart(...))``) get re-counted as fresh steps."""

    async def test_cursor_seeded_from_pending_entry_skips_prior_history(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        prior_history = [
            ModelResponse(parts=[TextPart(content="prior reply 1")]),
            ModelResponse(parts=[TextPart(content="prior reply 2")]),
            ModelResponse(parts=[TextPart(content="prior reply 3")]),
        ]
        pw = PendingWires()
        pw.put(
            _CORRELATION_ID,
            make_pending_entry(_wire(), message_history=tuple(prior_history)),
        )
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pw,
            steps_state,
        )
        new_history = [
            *prior_history,
            ModelResponse(parts=[TextPart(content="this turn")]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=new_history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Only the one new TextPart counts; the prior 3 are skipped.
        assert persona_sender.send.await_count == 1
        assert persona_sender.send.call_args.kwargs["content"] == "this turn"
        assert len(steps_state.get(_CORRELATION_ID).rendered_lines) == 1


class TestCursorMonotonicity:
    """PRESERVED invariant #1: a peer envelope arriving with a SHORTER
    message_history after the real emitter advanced the cursor must NOT
    regress it (no duplicate count, no extra post/edit)."""

    async def test_peer_short_envelope_does_not_regress_cursor(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        hop1_history = [
            ModelResponse(parts=[TextPart(content="real hop 1")]),
            ModelResponse(parts=[TextPart(content="real hop 1 cont.")]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=hop1_history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="scheduler"),
            broker=broker,
        )
        entry = steps_state.get(_CORRELATION_ID)
        cursor_after_real = entry.history_cursor
        assert cursor_after_real == len(hop1_history)
        assert len(entry.rendered_lines) == 2
        assert persona_sender.send.await_count == 1

        # Peer envelope: shorter (empty) history → must not rewind cursor,
        # append lines, or touch Discord.
        await consumer.handler(
            envelope=_envelope(message_history=[]),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="scheduler"),
            broker=broker,
        )
        entry = steps_state.get(_CORRELATION_ID)
        assert entry.history_cursor == cursor_after_real
        assert len(entry.rendered_lines) == 2
        assert persona_sender.send.await_count == 1
        persona_sender.edit_message.assert_not_called()

        # Next real hop appends one entry → exactly one new step + one edit.
        hop2_history = [
            *hop1_history,
            ModelResponse(parts=[TextPart(content="real hop 2 new")]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=hop2_history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="scheduler"),
            broker=broker,
        )
        await _drain_debounce(steps_state)
        # Still only one post; the new step is reflected via a single edit.
        assert persona_sender.send.await_count == 1
        assert len(steps_state.get(_CORRELATION_ID).rendered_lines) == 3
        persona_sender.edit_message.assert_awaited_once()
        assert persona_sender.edit_message.call_args.kwargs["content"] == (
            "real hop 1\nreal hop 1 cont.\nreal hop 2 new"
        )


class TestPerHopPersona:
    """PRESERVED invariant #2: persona is resolved per hop from
    ``result.emitter_node_id`` (never cached on the entry), so distinct
    real emitters flip the persona used on the progress message."""

    async def test_peer_no_delta_envelope_does_not_claim_persona(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """A gated-out peer (conan) with an empty delta posts nothing and
        seeds no persona; the real emitter (codex) then posts the progress
        message under ITS OWN persona — proving the entry carries no
        cached persona from the peer."""
        consumer = build_steps_consumer(
            persona_sender,
            _two_agent_registry(),
            pending_wires,
            steps_state,
        )
        # Peer hop first — gates filtered conan; empty history.
        await consumer.handler(
            envelope=_envelope(message_history=[]),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="conan"),
            broker=broker,
        )
        persona_sender.send.assert_not_called()

        # Real emitter's first renderable hop.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="from codex")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="codex"),
            broker=broker,
        )
        assert persona_sender.send.await_count == 1
        assert persona_sender.send.call_args.kwargs["persona"].name == "Codex"

    async def test_progress_persona_flips_between_distinct_emitters(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """The persona on the posted progress message is resolved from the
        hop's emitter: a codex-emitted first hop posts under Codex; an
        otherwise-identical conan-emitted first hop posts under Conan. A
        regression that cached persona on ``StepsEntry`` at seed time
        could not flip it per-emitter like this."""
        registry = _two_agent_registry()

        # Correlation A: first renderable hop emitted by codex.
        pw_a = PendingWires()
        pw_a.put("corr-a", make_pending_entry(_wire()))
        consumer_a = build_steps_consumer(
            persona_sender,
            registry,
            pw_a,
            StepsState(),
        )
        await consumer_a.handler(
            envelope=_envelope(
                correlation_id="corr-a",
                message_history=[ModelResponse(parts=[TextPart(content="x")])],
            ),
            correlation_id="corr-a",
            headers=_headers(emitter="codex"),
            broker=broker,
        )
        # Correlation B: first renderable hop emitted by conan.
        pw_b = PendingWires()
        pw_b.put("corr-b", make_pending_entry(_wire()))
        consumer_b = build_steps_consumer(
            persona_sender,
            registry,
            pw_b,
            StepsState(),
        )
        await consumer_b.handler(
            envelope=_envelope(
                correlation_id="corr-b",
                message_history=[ModelResponse(parts=[TextPart(content="y")])],
            ),
            correlation_id="corr-b",
            headers=_headers(emitter="conan"),
            broker=broker,
        )

        personas = [c.kwargs["persona"].name for c in persona_sender.send.call_args_list]
        assert personas == ["Codex", "Conan"]


class TestEarlySkip:
    """PRESERVED invariant #3: a no-delta non-terminal hop short-circuits
    BEFORE ``_render_live_delta`` runs (and posts/edits nothing)."""

    async def test_early_skip_bypasses_render(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        spy = MagicMock(return_value=[])
        monkeypatch.setattr(steps_mod, "_render_live_delta", spy)

        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(message_history=[]),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        spy.assert_not_called()
        persona_sender.send.assert_not_called()
        persona_sender.edit_message.assert_not_called()


class TestOutboxRetryDedup:
    """PRESERVED invariant #4: a re-published completed correlation must
    NOT post a second progress message (completed-set guard)."""

    async def test_completed_correlation_is_skipped(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        # Simulate the original terminal hop having already run.
        steps_state.pop_and_mark_completed(_CORRELATION_ID)
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelRequest(parts=[UserPromptPart(content="retry prompt")]),
                    ModelResponse(
                        parts=[
                            TextPart(content="trying again"),
                            ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                        ]
                    ),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.send.assert_not_called()
        persona_sender.edit_message.assert_not_called()
        persona_sender.delete_message.assert_not_called()


class TestFailureSwallowing:
    """Discord and rendering failures must never escape the consumer or
    crash the final-reply path."""

    async def test_rate_limited_on_progress_send_is_swallowed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """``discord.RateLimited`` is a ``DiscordException`` but NOT an
        ``HTTPException`` — the broader catch must funnel it through."""
        persona_sender.send = AsyncMock(
            side_effect=discord.RateLimited(retry_after=1.0),
        )
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="hi")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Swallowed: no id stored, count still advanced (next hop retries post).
        entry = steps_state.get(_CORRELATION_ID)
        assert entry is not None
        assert entry.progress_message_id is None
        assert len(entry.rendered_lines) == 1

    async def test_forbidden_on_progress_send_is_swallowed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        persona_sender.send = AsyncMock(
            side_effect=_http_exc(discord.Forbidden, 403),
        )
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(
                    message_history=[
                        ModelResponse(parts=[TextPart(content="hi")]),
                    ]
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert steps_state.get(_CORRELATION_ID).progress_message_id is None
        assert any("Forbidden" in r.message for r in caplog.records)

    async def test_notfound_on_edit_is_swallowed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """A debounced edit hitting NotFound (message already deleted) is
        ignored — the consumer does not raise."""
        persona_sender.edit_message = AsyncMock(
            side_effect=_http_exc(discord.NotFound, 404),
        )
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        # Hop 1 posts.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Hop 2 schedules an edit that will hit NotFound.
        await consumer.handler(
            envelope=_envelope(
                message_history=[
                    ModelResponse(parts=[TextPart(content="a")]),
                    ModelResponse(parts=[TextPart(content="b")]),
                ]
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Draining the task must not raise.
        await _drain_debounce(steps_state)
        persona_sender.edit_message.assert_awaited_once()

    async def test_notfound_on_delete_is_swallowed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """A terminal-hop delete hitting NotFound (already gone) is
        ignored; completion is still recorded."""
        persona_sender.delete_message = AsyncMock(
            side_effect=_http_exc(discord.NotFound, 404),
        )
        steps_state.put(
            _CORRELATION_ID,
            StepsEntry(
                parent_channel_id=_CHANNEL_ID,
                parent_message_id=_MESSAGE_ID,
                progress_message_id=_PROGRESS_MESSAGE_ID,
                history_cursor=1,
            ),
        )
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="done"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.delete_message.assert_awaited_once()
        assert steps_state.get(_CORRELATION_ID) is None
        assert steps_state.is_completed(_CORRELATION_ID)

    async def test_live_render_exception_is_swallowed_and_cursor_advances(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If the live renderer raises (here ``_render_live_tool_call_part``),
        the exception is logged and the cursor still advances so we don't loop
        on the bad message — and no progress message is posted for the failed
        delta."""
        original = steps_mod._render_live_tool_call_part

        def _raise(_part: Any) -> str:
            raise RuntimeError("synthetic render failure")

        steps_mod._render_live_tool_call_part = _raise
        try:
            consumer = build_steps_consumer(
                persona_sender,
                _registry(),
                pending_wires,
                steps_state,
            )
            history = [
                ModelResponse(
                    parts=[
                        ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                    ]
                ),
            ]
            with caplog.at_level(logging.ERROR):
                await consumer.handler(
                    envelope=_envelope(message_history=history),
                    correlation_id=_CORRELATION_ID,
                    headers=_headers(),
                    broker=broker,
                )
            persona_sender.send.assert_not_called()
            entry = steps_state.get(_CORRELATION_ID)
            assert entry is not None
            assert entry.history_cursor == len(history)
            assert len(entry.rendered_lines) == 0
            assert any("_render_live_delta raised" in r.message for r in caplog.records)
        finally:
            steps_mod._render_live_tool_call_part = original


class TestProgressTruncation:
    """End-to-end (through ``_consume``): a turn that accumulates more trace
    than fits the tail-window budget posts content that stays under Discord's
    hard cap and carries the elision marker, with the most recent steps kept
    and the oldest dropped. Guards the accumulator → tail-window → clamp
    wiring that the per-function unit tests exercise only in isolation."""

    async def test_long_trace_is_tail_windowed_in_posted_content(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        consumer = build_steps_consumer(persona_sender, _registry(), pending_wires, steps_state)
        # 30 tool calls, each rendering to a long (per-part-capped) line —
        # well over _PROGRESS_BODY_MAX_CHARS combined. The "i=<n>" prefix
        # survives per-part truncation (it sits at the front of the args), so
        # we can identify which steps the tail window kept vs dropped.
        history = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="call", args={"i": n, "pad": "x" * 200}, tool_call_id=f"t{n}")
                    for n in range(30)
                ]
            ),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        persona_sender.send.assert_awaited_once()
        content = persona_sender.send.call_args.kwargs["content"]
        # Never exceeds Discord's hard cap, and the elision is announced.
        assert len(content) <= steps_mod._DISCORD_MESSAGE_LIMIT
        assert content.startswith(steps_mod._HIDDEN_STEPS_MARKER)
        # Most recent step kept; oldest dropped.
        assert "i=29," in content
        assert "i=0," not in content


class TestLiveRender:
    """The compact live renderer (Hybrid style: prose text, an inline-code
    ``tool_name(args)`` call line, and a short fenced ``⎿`` result block of up
    to a few lines) and the tail-window cap that keeps the in-place edit under
    Discord's 2000-char limit."""

    def test_text_part_kept_as_prose(self) -> None:
        delta = [ModelResponse(parts=[TextPart(content="On it — checking now.")])]
        assert steps_mod._render_live_delta(delta) == ["On it — checking now."]

    def test_tool_call_renders_keyword_args_as_inline_code(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="weather", args={"city": "Tokyo", "n": 5}, tool_call_id="t1")])
        ]
        assert steps_mod._render_live_delta(delta) == ['`weather(city="Tokyo", n=5)`']

    def test_empty_args_render_bare_parens(self) -> None:
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="ping", args={}, tool_call_id="t1")])]
        assert steps_mod._render_live_delta(delta) == ["`ping()`"]

    def test_non_object_args_fall_back_to_raw_json(self) -> None:
        # args is a JSON string that parses to a list, not an object →
        # args_as_dict() asserts; the renderer falls back to the raw JSON.
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="f", args="[1, 2]", tool_call_id="t1")])]
        assert steps_mod._render_live_delta(delta) == ["`f([1, 2])`"]

    def test_scalar_arg_falls_back_to_raw_json(self) -> None:
        # A bare scalar arg also fails args_as_dict (not an object) → fallback.
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="f", args="42", tool_call_id="t1")])]
        assert steps_mod._render_live_delta(delta) == ["`f(42)`"]

    def test_nested_object_arg_values_render_as_compact_json(self) -> None:
        # Non-scalar arg VALUES are JSON-encoded with compact separators (no
        # space after ':'/','), keeping the call line tight.
        delta = [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="q",
                        args={"filter": {"gte": 5, "lt": 10}, "tags": [1, 2]},
                        tool_call_id="t1",
                    )
                ]
            )
        ]
        assert steps_mod._render_live_delta(delta) == ['`q(filter={"gte":5,"lt":10}, tags=[1,2])`']

    def test_tool_return_renders_short_result_in_fenced_block(self) -> None:
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1")])]
        # ``⎿`` first line, wrapped in a fence so a stray ``` can't break out.
        assert steps_mod._render_live_delta(delta) == ["```\n⎿ 18C\n```"]

    def test_tool_return_preserves_real_lines(self) -> None:
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="line1\nline2\nline3", tool_call_id="x")])]
        # Real lines are PRESERVED (not collapsed): ⎿ first, continuation
        # aligned two spaces under it.
        assert steps_mod._render_live_delta(delta) == ["```\n⎿ line1\n  line2\n  line3\n```"]

    def test_tool_return_keeps_first_lines_and_marks_dropped_lines(self) -> None:
        content = "\n".join(f"line{i}" for i in range(5))
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content=content, tool_call_id="x")])]
        rendered = steps_mod._render_live_delta(delta)[0]
        # Only the first _LIVE_RETURN_MAX_LINES lines survive; the DROPPED-lines
        # marker rides the last kept line and the full result stays on the ⤵ view.
        assert "⎿ line0" in rendered
        assert "line2 … (truncated)" in rendered
        assert "line3" not in rendered

    def test_exactly_max_lines_is_not_marked_truncated(self) -> None:
        # A result of exactly _LIVE_RETURN_MAX_LINES lines, each short, is
        # complete — no spurious "(truncated)" marker (guards the > vs >= edge).
        content = "\n".join(f"line{i}" for i in range(steps_mod._LIVE_RETURN_MAX_LINES))
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content=content, tool_call_id="x")])]
        rendered = steps_mod._render_live_delta(delta)[0]
        assert "(truncated)" not in rendered
        assert "…" not in rendered

    def test_single_backticks_in_return_preserved_in_fence(self) -> None:
        # Inside a code fence single/double backticks render literally — no need
        # to mangle them the way the old inline-code span did.
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="use `code` now", tool_call_id="x")])]
        assert steps_mod._render_live_delta(delta) == ["```\n⎿ use `code` now\n```"]

    def test_triple_backticks_in_return_neutralized(self) -> None:
        # A run of 3+ backticks would close the fence early; it is woven with
        # zero-width spaces so only the wrapping fence survives as a raw run.
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="```py", tool_call_id="x")])]
        rendered = steps_mod._render_live_delta(delta)[0]
        assert rendered.count("```") == 2  # the wrapping fence only
        assert "\u200b" in rendered

    def test_oversized_tool_return_line_is_cut_on_that_line(self) -> None:
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="x" * 5000, tool_call_id="x")])]
        rendered = steps_mod._render_live_delta(delta)[0]
        # A single over-long line is cut with a trailing "…" ON that line (the
        # cut is marked where it happened); no whole lines were dropped, so
        # there is NO "(truncated)" marker. The block never carries the payload.
        assert rendered.count("x") <= steps_mod._LIVE_RETURN_LINE_MAX_CHARS
        assert rendered.rstrip("`\n").endswith("…")
        assert "(truncated)" not in rendered

    def test_tail_window_drops_oldest_and_marks_elision(self) -> None:
        lines = [f"line{i}" for i in range(100)]
        body = steps_mod._tail_window(lines, max_chars=40)
        assert body.startswith(steps_mod._HIDDEN_STEPS_MARKER)
        assert "line99" in body  # most recent survives
        assert "line0\n" not in body  # oldest dropped

    def test_tail_window_no_marker_when_everything_fits(self) -> None:
        body = steps_mod._tail_window(["a", "b", "c"], max_chars=1000)
        assert body == "a\nb\nc"
        assert steps_mod._HIDDEN_STEPS_MARKER not in body

    def test_progress_content_is_body_only_and_hard_clamped(self) -> None:
        entry = StepsEntry(parent_channel_id=1, parent_message_id=2)
        entry.rendered_lines = [f"`⎿ {'x' * 150}`" for _ in range(200)]
        content = steps_mod._progress_content(entry)
        # No header line; the message IS the (tail-windowed) trace, never over
        # Discord's hard cap.
        assert not content.startswith("⚙ running…")
        assert len(content) <= steps_mod._DISCORD_MESSAGE_LIMIT


class TestTreeRender:
    """The full ``⤵ steps`` transcript renderer (``_render_tree_blocks``):
    Claude-Code-style ``● tool(args)`` / ``⎿ result`` blocks, one per visual
    block (a tool call and its result are ONE block), no per-part truncation,
    paired by ``tool_call_id`` (handles parallel calls); a return whose call is
    absent from the slice renders standalone."""

    def test_text_then_call_pair_counts_as_two_blocks(self) -> None:
        delta = [
            ModelResponse(
                parts=[
                    TextPart(content="Let me check."),
                    ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1"),
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1")]),
        ]
        blocks = steps_mod._render_tree_blocks(delta)
        # Prose block + ONE call/return block — the result is folded into its
        # call, so a tool use credits a single step.
        assert blocks == ["Let me check.", '```\n● weather(c="Tokyo")\n  ⎿  18C\n```']

    def test_multiline_result_nests_with_aligned_continuation(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"cmd": "ls"}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="shell", content="a\nb\nc", tool_call_id="t1")]),
        ]
        assert steps_mod._render_tree_blocks(delta) == ['```\n● shell(cmd="ls")\n  ⎿  a\n     b\n     c\n```']

    def test_parallel_calls_pair_to_their_own_returns(self) -> None:
        delta = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="a"),
                    ToolCallPart(tool_name="news", args={"t": "tech"}, tool_call_id="b"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="weather", content="18C", tool_call_id="a"),
                    ToolReturnPart(tool_name="news", content="headline", tool_call_id="b"),
                ]
            ),
        ]
        # Two call/return blocks, each return matched to its call BY ID (not by
        # position) and rendered in call order.
        assert steps_mod._render_tree_blocks(delta) == [
            '```\n● weather(c="Tokyo")\n  ⎿  18C\n```',
            '```\n● news(t="tech")\n  ⎿  headline\n```',
        ]

    def test_call_without_return_renders_call_line_alone(self) -> None:
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="slow", args={"x": 1}, tool_call_id="p")])]
        assert steps_mod._render_tree_blocks(delta) == ["```\n● slow(x=1)\n```"]

    def test_orphan_return_renders_standalone_not_dropped(self) -> None:
        # A return whose call predates the slice must NOT be silently dropped —
        # that would also skew the step count gating the ⤵ button.
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="z")])]
        assert steps_mod._render_tree_blocks(delta) == ["```\n⎿  18C\n```"]

    def test_no_per_part_truncation_in_full_view(self) -> None:
        big = "y" * 9000
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="dump", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="dump", content=big, tool_call_id="t1")]),
        ]
        rendered = steps_mod._render_tree_blocks(delta)[0]
        # The full payload survives — the only bound is the overall message cap
        # (enforced by steps_toggle's file-attachment path), not a per-part cap.
        assert rendered.count("y") == 9000

    def test_triple_backticks_in_result_cannot_break_the_fence(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="echo", content="```py\ncode\n```", tool_call_id="t1")]),
        ]
        rendered = steps_mod._render_tree_blocks(delta)[0]
        # Only the wrapping fence survives as a raw triple-backtick run; the
        # embedded fences are woven with zero-width spaces.
        assert rendered.count("```") == 2
        assert "\u200b" in rendered

    def test_skips_prompt_parts(self) -> None:
        delta = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="system."),
                    UserPromptPart(content="hello"),
                ]
            ),
        ]
        assert steps_mod._render_tree_blocks(delta) == []

    def test_parallel_call_with_one_missing_return(self) -> None:
        # Two parallel calls, only the first has returned this slice: the
        # paired call folds its result, the in-flight one renders alone.
        delta = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="a", args={}, tool_call_id="a"),
                    ToolCallPart(tool_name="b", args={}, tool_call_id="b"),
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="a", content="ra", tool_call_id="a")]),
        ]
        assert steps_mod._render_tree_blocks(delta) == [
            "```\n● a()\n  ⎿  ra\n```",
            "```\n● b()\n```",
        ]

    def test_return_before_its_call_renders_once_not_twice(self) -> None:
        # Order-independence: a return that appears BEFORE its call in the
        # slice must fold into the call exactly once — never render both
        # standalone AND nested (which would also inflate the step count).
        delta = [
            ModelRequest(parts=[ToolReturnPart(tool_name="a", content="EARLY", tool_call_id="x")]),
            ModelResponse(parts=[ToolCallPart(tool_name="a", args={}, tool_call_id="x")]),
        ]
        assert steps_mod._render_tree_blocks(delta) == ["```\n● a()\n  ⎿  EARLY\n```"]

    def test_full_view_preserves_arg_whitespace_fidelity(self) -> None:
        # collapse=False on the full view keeps inner whitespace in arg values
        # byte-for-byte (the live preview would collapse "a  b" -> "a b").
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="run", args={"cmd": "a  b"}, tool_call_id="t1")])]
        assert steps_mod._render_tree_blocks(delta) == ['```\n● run(cmd="a  b")\n```']

    def test_count_gate_parity_tree_empty_iff_live_empty(self) -> None:
        # Load-bearing invariant: the tree renderer (whose block count gates the
        # ⤵ button + transcript row in the outbox) renders zero blocks for
        # EXACTLY the deltas the live renderer renders nothing for.
        cases: list[list[ModelMessage]] = [
            [ModelRequest(parts=[UserPromptPart(content="hi"), SystemPromptPart(content="s")])],
            [ModelResponse(parts=[TextPart(content="   ")])],
            [ModelResponse(parts=[TextPart(content="hello")])],
            [
                ModelResponse(parts=[ToolCallPart(tool_name="w", args={}, tool_call_id="t")]),
                ModelRequest(parts=[ToolReturnPart(tool_name="w", content="r", tool_call_id="t")]),
            ],
            [
                ModelResponse(
                    parts=[
                        ToolCallPart(tool_name="a", args={}, tool_call_id="a"),
                        ToolCallPart(tool_name="b", args={}, tool_call_id="b"),
                    ]
                ),
                ModelRequest(
                    parts=[
                        ToolReturnPart(tool_name="a", content="ra", tool_call_id="a"),
                        ToolReturnPart(tool_name="b", content="rb", tool_call_id="b"),
                    ]
                ),
            ],
            [ModelRequest(parts=[ToolReturnPart(tool_name="w", content="r", tool_call_id="z")])],
        ]
        for delta in cases:
            tree_empty = steps_mod._render_tree_blocks(delta) == []
            live_empty = steps_mod._render_live_delta(delta) == []
            assert tree_empty == live_empty, f"count-gate parity broken for {delta!r}"


class TestFenceSafe:
    """``_fence_safe`` neutralizes runs of 3+ backticks (which would close a
    Discord code fence early regardless of the opening fence length) while
    leaving 1-2 backtick runs — which render literally inside a block —
    untouched."""

    def test_single_and_double_backtick_runs_untouched(self) -> None:
        assert steps_mod._fence_safe("a `b` c") == "a `b` c"
        assert steps_mod._fence_safe("``x``") == "``x``"

    def test_runs_of_three_or_more_are_woven_with_zwsp(self) -> None:
        for n in (3, 4, 6):
            out = steps_mod._fence_safe("`" * n)
            assert "```" not in out  # no raw 3-run survives to close a fence
            assert out.count("`") == n  # every backtick preserved, just separated
            assert out.count("\u200b") == n - 1
