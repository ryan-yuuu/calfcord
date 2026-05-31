"""Unit tests for the steps consumer built by ``build_steps_consumer``.

Drives ``ConsumerNodeDef.handler`` directly with synthetic ``Envelope``s
that carry a hand-rolled ``state.message_history`` representing the
agent's running pydantic_ai conversation. The consumer's sink is a single
transient in-channel "progress" message (``⚙ running… N steps``) posted
under the agent persona, edited (debounced) as steps stream in, and
deleted on the terminal hop. There is NO database access in this phase.

The tests exercise:

* the preserved per-hop invariants, re-expressed against the progress
  sink: cursor monotonicity, per-hop persona resolution, the no-delta
  early skip, outbox-retry dedup, and the thread-originated-wire skip;
* the progress lifecycle: post-once-on-first-renderable-hop, step_count
  increments across hops, debounced edit reflects the latest count,
  terminal cancels debounce + deletes, pure-text turns post nothing;
* failure swallowing (Discord errors on send/edit/delete, exceptions
  inside ``_render_delta``).

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
        assert call.kwargs["content"] == "⚙ running… 1 step"
        assert call.kwargs["persona"].name == "Aksel"
        # Plain send: no reply, no thread, no extra buttons.
        assert call.kwargs.get("reply_to") is None
        assert call.kwargs.get("thread_id") is None
        assert call.kwargs.get("extra_buttons") is None

        entry = steps_state.get(_CORRELATION_ID)
        assert entry is not None
        assert entry.progress_message_id == _PROGRESS_MESSAGE_ID
        assert entry.step_count == 1
        # Cursor advanced past the whole delta.
        assert entry.history_cursor == len(history)

    async def test_multi_part_first_hop_counts_all_parts(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """One ModelResponse with preamble text + a tool call renders to
        TWO parts → the first progress post already reads '2 steps'."""
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
        assert persona_sender.send.call_args.kwargs["content"] == "⚙ running… 2 steps"
        assert steps_state.get(_CORRELATION_ID).step_count == 2

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
        assert persona_sender.send.call_args.kwargs["content"] == "⚙ running… 1 step"

    async def test_renderable_part_types_counted(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
    ) -> None:
        """TextPart, ToolCallPart, and ToolReturnPart each count as one
        step; prompts do not."""
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
        # text + tool call + tool return = 3 (prompts excluded).
        assert persona_sender.send.call_args.kwargs["content"] == "⚙ running… 3 steps"


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
        assert entry.step_count == 0

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
    """Subsequent renderable hops bump step_count and edit (debounced) —
    they never post a second message."""

    async def test_second_hop_edits_with_running_count(
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
        # Hop 1: a tool call → posts "1 step".
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

        # Hop 2: the tool return arrived → debounced edit to "2 steps".
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
        assert edit_call.kwargs["content"] == "⚙ running… 2 steps"
        assert steps_state.get(_CORRELATION_ID).step_count == 2

    async def test_burst_coalesces_to_one_edit_at_latest_count(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two edit-triggering hops while a debounce task is still pending
        coalesce into a single edit that reflects the LATEST count (the
        bump during the window is read at FIRE time, not schedule time).

        Determinism: the debounce ``sleep`` is replaced by an event we
        control, so the task is guaranteed to remain parked in its wait
        (count NOT yet read) across both hops. Releasing the gate then
        fires exactly one edit, which reads ``step_count`` at that instant
        — by which point hop 3 has bumped it to 3. No reliance on
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
        # Hop 1 posts ("1 step").
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
        # Hop 2 schedules a debounce task (count now 2). It parks in the
        # gated sleep — pending, not done, count NOT yet read.
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

        # Hop 3 bumps the count to 3 while the (parked) task is still
        # pending; it must reuse the SAME pending task, not spawn a second.
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

        # Release the gate so the single task wakes and edits NOW, reading
        # the latest count (3).
        gate.set()
        await _drain_debounce(steps_state)

        persona_sender.edit_message.assert_awaited_once()
        assert persona_sender.edit_message.call_args.kwargs["content"] == "⚙ running… 3 steps"
        assert steps_state.get(_CORRELATION_ID).step_count == 3
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
                step_count=2,
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
    """When the inbound wire originated inside a Discord thread, the
    progress surface is disabled for that correlation (the parent-channel
    webhook would post the counter in the wrong place). PRESERVED
    invariant: thread-originated wires skip — no progress message."""

    async def test_thread_originated_wire_skips_seeding(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pw = PendingWires()
        # source_channel_id != channel_id => message came from a thread.
        pw.put(_CORRELATION_ID, make_pending_entry(_wire(source_channel_id=999999)))
        consumer = build_steps_consumer(
            persona_sender,
            _registry(),
            pw,
            steps_state,
        )
        with caplog.at_level(logging.DEBUG):
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
        persona_sender.send.assert_not_called()
        persona_sender.edit_message.assert_not_called()
        assert steps_state.get(_CORRELATION_ID) is None
        assert any("wire originated in a thread" in r.message for r in caplog.records)


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
        assert persona_sender.send.call_args.kwargs["content"] == "⚙ running… 1 step"
        assert steps_state.get(_CORRELATION_ID).step_count == 1


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
        assert entry.step_count == 2
        assert persona_sender.send.await_count == 1

        # Peer envelope: shorter (empty) history → must not rewind cursor,
        # bump the count, or touch Discord.
        await consumer.handler(
            envelope=_envelope(message_history=[]),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="scheduler"),
            broker=broker,
        )
        entry = steps_state.get(_CORRELATION_ID)
        assert entry.history_cursor == cursor_after_real
        assert entry.step_count == 2
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
        assert steps_state.get(_CORRELATION_ID).step_count == 3
        persona_sender.edit_message.assert_awaited_once()
        assert persona_sender.edit_message.call_args.kwargs["content"] == "⚙ running… 3 steps"


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
    BEFORE ``_render_delta`` runs (and posts/edits nothing)."""

    async def test_early_skip_bypasses_render_delta(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        spy = MagicMock(return_value=[])
        monkeypatch.setattr(steps_mod, "_render_delta", spy)

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
        assert entry.step_count == 1

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
                step_count=1,
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

    async def test_render_delta_exception_is_swallowed_and_cursor_advances(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If rendering a part raises, the exception is logged and the
        cursor still advances so we don't loop on the bad message — and no
        progress message is posted for the failed delta."""
        original = steps_mod._render_tool_call_part

        def _raise(_part: Any) -> str:
            raise RuntimeError("synthetic render failure")

        steps_mod._render_tool_call_part = _raise
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
            assert entry.step_count == 0
            assert any("_render_delta raised" in r.message for r in caplog.records)
        finally:
            steps_mod._render_tool_call_part = original
