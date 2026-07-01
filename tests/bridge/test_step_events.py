"""Unit tests for the RunEvent → StepEvent seam (``normalize_run_event``)."""

from __future__ import annotations

from calfkit.client import (
    AgentMessageEvent,
    HandoffEvent,
    RunCompleted,
    RunFailed,
    ToolCallEvent,
    ToolResultEvent,
)
from calfkit.models.error_report import ErrorReport
from calfkit.models.payload import TextPart

from calfcord.bridge.step_events import StepEvent, normalize_run_event


def test_tool_call_normalizes_with_dict_args() -> None:
    e = ToolCallEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter="alice",
        tool_call_id="t1",
        name="message_agent",
        args={"name": "scribe", "message": "hi"},
    )
    assert normalize_run_event(e) == StepEvent(
        kind="tool_call",
        correlation_id="c1",
        depth=1,
        emitter="alice",
        tool_call_id="t1",
        name="message_agent",
        args={"name": "scribe", "message": "hi"},
    )


def test_tool_call_normalizes_json_string_args() -> None:
    e = ToolCallEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter="alice",
        tool_call_id="t1",
        name="message_agent",
        args='{"name": "scribe", "message": "hi"}',
    )
    s = normalize_run_event(e)
    assert s is not None and s.args == {"name": "scribe", "message": "hi"}


def test_tool_result_renders_text() -> None:
    e = ToolResultEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter="scribe",
        tool_call_id="t1",
        name="scribe",
        parts=[TextPart(text="the summary")],
        is_error=False,
    )
    s = normalize_run_event(e)
    assert s is not None
    assert (s.kind, s.text, s.tool_call_id, s.is_error) == ("tool_result", "the summary", "t1", False)


def test_agent_message_concatenates_text_parts() -> None:
    e = AgentMessageEvent(
        correlation_id="c1",
        depth=0,
        frame_id="f",
        emitter="alice",
        parts=[TextPart(text="think "), TextPart(text="hard")],
    )
    s = normalize_run_event(e)
    assert s is not None and s.kind == "agent_message" and s.text == "think hard"


def test_handoff_normalizes() -> None:
    e = HandoffEvent(correlation_id="c1", depth=0, frame_id="f", emitter="alice", target="scribe", reason="yours")
    s = normalize_run_event(e)
    assert s is not None and s.kind == "handoff" and s.target == "scribe" and s.reason == "yours"


def test_run_completed_returns_none() -> None:
    """The terminal RunCompleted carries no ``kind`` — the seam must return None so
    the handler drain skips it (the answer arrives via ``result()``). A refactor to
    direct ``event.kind`` access would AttributeError on every terminal and crash
    the whole drain, posting no reply."""
    e = RunCompleted(output="done", correlation_id="c1", agent="scribe", _envelope=None)
    assert normalize_run_event(e) is None


def test_run_failed_returns_none() -> None:
    e = RunFailed(report=ErrorReport(error_type="calf.test.boom"), correlation_id="c1")
    assert normalize_run_event(e) is None
