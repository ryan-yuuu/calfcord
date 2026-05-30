"""Unit tests for the steps consumer built by ``build_steps_consumer``.

Drives ``ConsumerNodeDef.handler`` directly with synthetic ``Envelope``s
that carry a hand-rolled ``state.message_history`` representing the
agent's running pydantic_ai conversation. The tests exercise:

* delta extraction across multiple hops (cursor advances correctly);
* initial cursor seed from ``PendingEntry.initial_message_history_length``;
* thread create-on-first-render + persona post sequence;
* terminal-hop renders the prior-tool-return delta before locking;
* terminal hop locks + marks correlation completed (outbox-retry dedup);
* the various skip paths (no wire, non-agent emitter, unknown agent,
  thread-originated wire);
* the v1 render rules (TextPart + ToolCallPart + ToolReturnPart only,
  whitespace skip, oversize truncation);
* failure swallowing (RateLimited on Discord calls, exceptions inside
  ``_render_delta``).

discord.py, Kafka, FastStream, and the LLM stack are all mocked out.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.pending_wires import PendingWires, make_pending_entry
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.steps import (
    THREAD_HEADER,
    build_steps_consumer,
)
from calfkit_organization.bridge.steps_state import StepsState
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.messages import SentMessage

_CORRELATION_ID = "evt-1"
_CHANNEL_ID = 6789
_MESSAGE_ID = 12345
_THREAD_ID = 555555


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


def _headers(
    *, emitter: str | None = "scheduler", emitter_kind: str | None = "agent"
) -> dict[str, Any]:
    h: dict[str, Any] = {}
    if emitter is not None:
        h["x-calf-emitter"] = emitter
    if emitter_kind is not None:
        h["x-calf-emitter-kind"] = emitter_kind
    return h


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


def _fake_bot_client(thread_id: int = _THREAD_ID) -> MagicMock:
    """Build a fake REST-only discord.Client supporting fetch_channel,
    message.create_thread, and thread.edit(locked=True)."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.edit = AsyncMock(return_value=None)

    message = MagicMock(spec=discord.Message)
    message.create_thread = AsyncMock(return_value=thread)

    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=message)

    client = MagicMock()
    # fetch_channel routes to channel by message id, thread by thread id.
    async def _fetch(cid: int) -> Any:
        if cid == thread_id:
            return thread
        return channel
    client.fetch_channel = AsyncMock(side_effect=_fetch)
    # Attach for assertion access from tests.
    client._fake_channel = channel
    client._fake_message = message
    client._fake_thread = thread
    return client


@pytest.fixture
def persona_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=SentMessage(id=42, channel_id=_THREAD_ID))
    sender.client = _fake_bot_client()
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
def broker() -> MagicMock:
    return MagicMock()


class TestRenderDelta:
    """First-hop scenarios: empty cursor, various part types."""

    async def test_text_part_alone_creates_thread_and_posts(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
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

        # Thread created exactly once + header + the text content = 2 posts.
        assert persona_sender.send.await_count == 2
        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        assert contents[0] == THREAD_HEADER
        assert contents[1] == "Let me check."
        # All posts target the thread.
        for c in persona_sender.send.call_args_list:
            assert c.kwargs["thread_id"] == _THREAD_ID
            assert c.kwargs["channel_id"] == _CHANNEL_ID
        # Persona is the agent's.
        assert persona_sender.send.call_args_list[0].kwargs["persona"].name == "Aksel"
        # Cursor advanced.
        assert steps_state.get(_CORRELATION_ID).history_cursor == len(history)

    async def test_tool_call_renders_with_json_block(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        history = [
            ModelRequest(parts=[UserPromptPart(content="lookup tokyo")]),
            ModelResponse(parts=[
                ToolCallPart(
                    tool_name="weather_lookup",
                    args={"city": "Tokyo"},
                    tool_call_id="tc-1",
                ),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        # [header, tool-call]
        assert len(contents) == 2
        assert "**Calling `weather_lookup`**" in contents[1]
        assert '"city"' in contents[1]
        assert "```json" in contents[1]

    async def test_text_and_tool_call_in_same_response(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        """The Claude pattern: preamble text alongside the tool call."""
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        history = [
            ModelRequest(parts=[UserPromptPart(content="lookup tokyo")]),
            ModelResponse(parts=[
                TextPart(content="Let me look that up."),
                ToolCallPart(
                    tool_name="weather_lookup",
                    args={"city": "Tokyo"},
                    tool_call_id="tc-1",
                ),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        # [header, text, tool-call]
        assert len(contents) == 3
        assert contents[1] == "Let me look that up."
        assert "**Calling `weather_lookup`**" in contents[2]

    async def test_whitespace_only_text_part_skipped(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        history = [
            ModelResponse(parts=[
                TextPart(content="   \n  "),
                ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        # header + only the tool call (text was whitespace-only).
        assert len(contents) == 2

    async def test_user_and_system_prompts_are_not_echoed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        history = [
            ModelRequest(parts=[
                SystemPromptPart(content="system."),
                UserPromptPart(content="user message"),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Nothing renderable — no posts, no thread.
        persona_sender.send.assert_not_called()
        # Cursor still advances so a later hop doesn't re-walk it.
        entry = steps_state.get(_CORRELATION_ID)
        assert entry is not None
        assert entry.history_cursor == len(history)
        assert entry.thread_id is None  # no thread until something renderable

    async def test_oversize_tool_args_truncated(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        big_value = "x" * 5000
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        history = [
            ModelResponse(parts=[
                ToolCallPart(
                    tool_name="t", args={"v": big_value}, tool_call_id="x",
                ),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        body = contents[1]
        assert "… (truncated)" in body
        # Whole content stays well under Discord's 2000 char cap.
        assert len(body) < 2000

    async def test_tool_return_renders_with_plain_block(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        history = [
            ModelRequest(parts=[
                ToolReturnPart(
                    tool_name="weather_lookup",
                    content="temp 18, cloudy",
                    tool_call_id="tc-1",
                ),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        body = contents[1]
        assert "**`weather_lookup` returned**" in body
        assert "temp 18, cloudy" in body


class TestMultiHop:
    """Sequence of hops on the same correlation_id — cursor advance + single thread."""

    async def test_second_hop_appends_without_recreating_thread(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )

        hop1_history = [
            ModelRequest(parts=[UserPromptPart(content="lookup tokyo")]),
            ModelResponse(parts=[
                ToolCallPart(tool_name="t", args={"x": 1}, tool_call_id="t1"),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=hop1_history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        first_send_count = persona_sender.send.await_count
        first_thread_creates = persona_sender.client._fake_message.create_thread.await_count
        assert first_thread_creates == 1

        # Hop 2: tool return arrived; model has not yet emitted a new response.
        hop2_history = [
            *hop1_history,
            ModelRequest(parts=[
                ToolReturnPart(
                    tool_name="t", content="result", tool_call_id="t1",
                ),
            ]),
        ]
        await consumer.handler(
            envelope=_envelope(message_history=hop2_history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # Only the new ToolReturnPart was rendered + posted.
        assert persona_sender.send.await_count == first_send_count + 1
        # Thread was NOT recreated.
        assert persona_sender.client._fake_message.create_thread.await_count == 1
        # Latest post body carries the tool return.
        latest = persona_sender.send.call_args_list[-1].kwargs["content"]
        assert "returned" in latest


class TestTerminalHop:
    async def test_terminal_hop_locks_thread_and_pops(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        # Seed a state entry as if previous hops happened.
        from calfkit_organization.bridge.steps_state import StepsEntry
        from calfkit_organization.discord.persona import Persona

        steps_state.put(
            _CORRELATION_ID,
            StepsEntry(
                parent_channel_id=_CHANNEL_ID,
                parent_message_id=_MESSAGE_ID,
                persona=Persona(name="Aksel", avatar_url=None),
                thread_id=_THREAD_ID,
                history_cursor=2,
            ),
        )

        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="all done"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # Did not post (the outbox posts the final reply).
        persona_sender.send.assert_not_called()
        # Thread lock was attempted.
        persona_sender.client._fake_thread.edit.assert_awaited_once_with(
            locked=True, archived=False,
        )
        # State entry released.
        assert steps_state.get(_CORRELATION_ID) is None

    async def test_terminal_hop_without_thread_skips_lock(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        """Pure-text agent — no thread was ever created."""
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="hi"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # No state entry → nothing to lock.
        persona_sender.client._fake_thread.edit.assert_not_called()


class TestSkipPaths:
    async def test_no_pending_wire_skips_silently(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), PendingWires(), steps_state,
        )
        with caplog.at_level(logging.DEBUG):
            await consumer.handler(
                envelope=_envelope(message_history=[
                    ModelResponse(parts=[TextPart(content="hi")]),
                ]),
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
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        await consumer.handler(
            envelope=_envelope(message_history=[
                ModelResponse(parts=[TextPart(content="hi")]),
            ]),
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
        broker: MagicMock,
    ) -> None:
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        await consumer.handler(
            envelope=_envelope(message_history=[
                ModelResponse(parts=[TextPart(content="hi")]),
            ]),
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
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        empty_registry = AgentRegistry([])
        consumer = build_steps_consumer(
            persona_sender, empty_registry, pending_wires, steps_state,
        )
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(message_history=[
                    ModelResponse(parts=[TextPart(content="hi")]),
                ]),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        persona_sender.send.assert_not_called()
        assert any("unknown emitter" in r.message for r in caplog.records)


class TestThreadCreateFailure:
    async def test_forbidden_keeps_entry_and_advances_cursor(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Thread-create Forbidden: log once (dedup), advance the cursor,
        keep the entry. Future hops can attempt create again with the
        next delta without re-walking the failed one."""
        persona_sender.client._fake_message.create_thread = AsyncMock(
            side_effect=_http_exc(discord.Forbidden, 403),
        )
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        history = [ModelResponse(parts=[TextPart(content="hi")])]
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(message_history=history),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        persona_sender.send.assert_not_called()
        # Entry retained so the cursor doesn't reset on the next hop.
        entry = steps_state.get(_CORRELATION_ID)
        assert entry is not None
        assert entry.thread_id is None
        assert entry.history_cursor == len(history)
        assert any("Forbidden" in r.message for r in caplog.records)

    async def test_repeated_forbidden_dedups_log(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Two hops both hitting Forbidden produce exactly one WARNING."""
        persona_sender.client._fake_message.create_thread = AsyncMock(
            side_effect=_http_exc(discord.Forbidden, 403),
        )
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(message_history=[
                    ModelResponse(parts=[TextPart(content="one")]),
                ]),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
            await consumer.handler(
                envelope=_envelope(message_history=[
                    ModelResponse(parts=[TextPart(content="one")]),
                    ModelResponse(parts=[TextPart(content="two")]),
                ]),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        forbidden_logs = [
            r for r in caplog.records
            if "Forbidden on create_thread" in r.message
        ]
        assert len(forbidden_logs) == 1


class TestInitialCursorSeed:
    """The cursor must skip the projected channel-history prefix that
    ``BridgeIngress`` passes into ``invoke_node`` — otherwise the agent's
    prior channel replies (rendered by ``project_history`` as
    ``ModelResponse(TextPart(...))``) get re-posted as fresh steps."""

    async def test_cursor_seeded_from_pending_entry_skips_prior_history(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        # 3 prior channel-projection messages + 1 new agent ModelResponse
        prior_history = [
            ModelResponse(parts=[TextPart(content="prior reply 1")]),
            ModelResponse(parts=[TextPart(content="prior reply 2")]),
            ModelResponse(parts=[TextPart(content="prior reply 3")]),
        ]
        pw = PendingWires()
        pw.put(
            _CORRELATION_ID,
            make_pending_entry(
                _wire(),
                message_history=tuple(prior_history),
                # `make_pending_entry` defaults this to len(message_history).
            ),
        )

        consumer = build_steps_consumer(
            persona_sender, _registry(), pw, steps_state,
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
        # 2 posts only: header + the one new TextPart. Prior 3 are skipped.
        assert persona_sender.send.await_count == 2
        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        assert contents[0] == THREAD_HEADER
        assert contents[1] == "this turn"


class TestTerminalDelta:
    """The terminal hop must render the prior-tool-return delta in the
    transcript thread before locking. Only the trailing final
    ``ModelResponse`` (the answer text the outbox posts) is suppressed."""

    async def test_terminal_hop_renders_tool_return_then_locks(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        from calfkit_organization.bridge.steps_state import StepsEntry
        from calfkit_organization.discord.persona import Persona

        # Seed an entry as if a prior tool-call hop happened.
        steps_state.put(
            _CORRELATION_ID,
            StepsEntry(
                parent_channel_id=_CHANNEL_ID,
                parent_message_id=_MESSAGE_ID,
                persona=Persona(name="Aksel", avatar_url=None),
                thread_id=_THREAD_ID,
                history_cursor=2,
            ),
        )
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        # Terminal envelope: history grew by the tool return + the final
        # ModelResponse with the answer.
        history = [
            ModelRequest(parts=[UserPromptPart(content="lookup tokyo")]),
            ModelResponse(parts=[
                ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1"),
            ]),
            ModelRequest(parts=[
                ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1"),
            ]),
            ModelResponse(parts=[TextPart(content="It's 18 degrees in Tokyo.")]),
        ]
        await consumer.handler(
            envelope=_envelope(
                message_history=history, final_text="It's 18 degrees in Tokyo.",
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # The tool return is rendered into the thread; the final
        # ModelResponse(text) is NOT (outbox posts that to the parent channel).
        contents = [c.kwargs["content"] for c in persona_sender.send.call_args_list]
        assert any("**`weather` returned**" in c for c in contents)
        assert not any("It's 18 degrees in Tokyo." in c for c in contents)
        # Lock fired.
        persona_sender.client._fake_thread.edit.assert_awaited_once_with(
            locked=True, archived=False,
        )
        # Entry popped + marked completed.
        assert steps_state.get(_CORRELATION_ID) is None
        assert steps_state.is_completed(_CORRELATION_ID)

    async def test_terminal_marks_completed_even_when_entry_never_existed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        """Pure-text terminal-first reply: no prior hops, no entry to pop —
        but the correlation must still be marked completed so any outbox
        retry doesn't seed a fresh transcript."""
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        await consumer.handler(
            envelope=_envelope(final_text="hello"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert steps_state.is_completed(_CORRELATION_ID)


class TestThreadOriginatedWire:
    """When the inbound wire originated inside a Discord thread,
    transcripts are disabled for that correlation. Discord forbids
    threading off a thread message."""

    async def test_thread_originated_wire_skips_seeding(
        self,
        persona_sender: AsyncMock,
        steps_state: StepsState,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pw = PendingWires()
        # source_channel_id != channel_id => message came from inside a thread.
        pw.put(
            _CORRELATION_ID,
            make_pending_entry(_wire(source_channel_id=999999)),
        )
        consumer = build_steps_consumer(
            persona_sender, _registry(), pw, steps_state,
        )
        with caplog.at_level(logging.DEBUG):
            await consumer.handler(
                envelope=_envelope(message_history=[
                    ModelResponse(parts=[
                        ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                    ]),
                ]),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        persona_sender.send.assert_not_called()
        persona_sender.client._fake_message.create_thread.assert_not_called()
        assert steps_state.get(_CORRELATION_ID) is None
        assert any(
            "wire originated in a thread" in r.message
            for r in caplog.records
        )


class TestOutboxRetryDedup:
    """A retry hop on a correlation whose terminal already locked the
    thread must NOT create a second thread."""

    async def test_completed_correlation_is_skipped(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        # Simulate the original terminal hop having already run.
        steps_state.pop_and_mark_completed(_CORRELATION_ID)
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        await consumer.handler(
            envelope=_envelope(message_history=[
                ModelRequest(parts=[UserPromptPart(content="retry prompt")]),
                ModelResponse(parts=[
                    TextPart(content="trying again"),
                    ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                ]),
            ]),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.send.assert_not_called()
        persona_sender.client._fake_message.create_thread.assert_not_called()


class TestFailureSwallowing:
    """Discord and rendering failures must not escape the consumer."""

    async def test_rate_limited_on_create_thread_is_swallowed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
    ) -> None:
        """``discord.RateLimited`` is a ``DiscordException`` but NOT an
        ``HTTPException`` — earlier code that only caught ``HTTPException``
        would let it propagate. Verify the broader catch handles it."""
        # RateLimited(retry_after) — discord.py's actual signature.
        persona_sender.client._fake_message.create_thread = AsyncMock(
            side_effect=discord.RateLimited(retry_after=1.0),
        )
        consumer = build_steps_consumer(
            persona_sender, _registry(), pending_wires, steps_state,
        )
        # Must not raise.
        await consumer.handler(
            envelope=_envelope(message_history=[
                ModelResponse(parts=[TextPart(content="hi")]),
            ]),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.send.assert_not_called()

    async def test_render_delta_exception_is_swallowed_and_cursor_advances(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        steps_state: StepsState,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If rendering a part raises (e.g. ToolCallPart with malformed
        args), the exception is logged and the cursor still advances so
        we don't loop on the bad message on every subsequent hop."""
        # ToolCallPart with non-JSON-parseable string args. args_as_json_str
        # is fine on a str but args_as_dict raises; if we ever change
        # _render_tool_call_part to dict-form rendering, this guard fires.
        # For now, inject the exception by monkey-patching the rendering
        # of a known-good tool call.
        import calfkit_organization.bridge.steps as steps_mod

        original = steps_mod._render_tool_call_part

        def _raise(_part: Any) -> str:
            raise RuntimeError("synthetic render failure")

        steps_mod._render_tool_call_part = _raise
        try:
            consumer = build_steps_consumer(
                persona_sender, _registry(), pending_wires, steps_state,
            )
            history = [
                ModelResponse(parts=[
                    ToolCallPart(tool_name="t", args={}, tool_call_id="x"),
                ]),
            ]
            with caplog.at_level(logging.ERROR):
                await consumer.handler(
                    envelope=_envelope(message_history=history),
                    correlation_id=_CORRELATION_ID,
                    headers=_headers(),
                    broker=broker,
                )
            # No posts; cursor advanced past the bad message.
            persona_sender.send.assert_not_called()
            entry = steps_state.get(_CORRELATION_ID)
            assert entry is not None
            assert entry.history_cursor == len(history)
            assert any(
                "_render_delta raised" in r.message for r in caplog.records
            )
        finally:
            steps_mod._render_tool_call_part = original
