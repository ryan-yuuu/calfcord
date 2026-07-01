"""Unit tests for the stateful A2A dispatcher (D-1/D-2).

A native ``message_agent`` consult is a ``tool_call`` StepEvent
(name=="message_agent"); its reply is a ``tool_result`` whose ``tool_call_id``
matches — it CANNOT be classified by its own fields (a happy reply's
emitter/name is the *peer*; a rejected one's is the caller). So the dispatcher
records each ``message_agent`` call's ``tool_call_id`` and pairs the matching
result. Reliable because the whole run shares one ``correlation_id`` (single
partition → request-before-reply order) and the handle stream is lossless+ordered.
"""

from __future__ import annotations

from calfcord.bridge.a2a_dispatch import (
    A2ADispatcher,
    A2AHandoff,
    A2AReject,
    A2AReply,
    A2ARequest,
)
from calfcord.bridge.step_events import StepEvent


def _call(
    tool_call_id: str, *, peer: str, message: str, caller: str = "alice", corr: str = "c1", depth: int = 1
) -> StepEvent:
    return StepEvent(
        kind="tool_call",
        correlation_id=corr,
        depth=depth,
        emitter=caller,
        tool_call_id=tool_call_id,
        name="message_agent",
        args={"name": peer, "message": message},
    )


def _result(
    tool_call_id: str,
    *,
    emitter: str,
    text: str,
    corr: str = "c1",
    depth: int = 1,
    is_error: bool = False,
    name: str | None = None,
) -> StepEvent:
    return StepEvent(
        kind="tool_result",
        correlation_id=corr,
        depth=depth,
        emitter=emitter,
        tool_call_id=tool_call_id,
        name=name,
        text=text,
        is_error=is_error,
    )


class TestA2ADispatcher:
    def test_consult_request_then_reply_pairs_by_tool_call_id(self) -> None:
        d = A2ADispatcher()
        req = d.classify(_call("t1", peer="scribe", message="summarize the doc"))
        assert isinstance(req, A2ARequest)
        assert (req.peer, req.message, req.caller, req.correlation_id) == ("scribe", "summarize the doc", "alice", "c1")
        rep = d.classify(_result("t1", emitter="scribe", text="here is the summary", name="scribe"))
        assert isinstance(rep, A2AReply)
        assert (rep.peer, rep.text, rep.tool_call_id) == ("scribe", "here is the summary", "t1")

    def test_non_message_agent_tool_call_is_live_progress(self) -> None:
        d = A2ADispatcher()
        step = StepEvent(
            kind="tool_call",
            correlation_id="c1",
            depth=1,
            emitter="a",
            tool_call_id="t9",
            name="search",
            args={"q": "x"},
        )
        assert d.classify(step) is None

    def test_unpaired_tool_result_is_live_progress(self) -> None:
        d = A2ADispatcher()
        assert d.classify(_result("never-a-consult", emitter="x", text="tool output")) is None

    def test_rejected_consult_renders_as_reject_with_peer_from_request(self) -> None:
        """An offline/cycle/self consult is a tool_result with is_error=True,
        emitter==caller, name=='message_agent' — render a system note, and take
        the peer identity from the recorded REQUEST args (stable across reject)."""
        d = A2ADispatcher()
        d.classify(_call("t2", peer="ghost", message="hi"))
        rej = d.classify(
            _result("t2", emitter="alice", text="error: agent 'ghost' is offline", is_error=True, name="message_agent")
        )
        assert isinstance(rej, A2AReject)
        assert rej.peer == "ghost"
        assert "offline" in rej.text

    def test_handoff_event_renders_as_handoff(self) -> None:
        d = A2ADispatcher()
        h = d.classify(
            StepEvent(
                kind="handoff", correlation_id="c1", depth=0, emitter="alice", target="scribe", reason="prose is yours"
            )
        )
        assert isinstance(h, A2AHandoff)
        assert (h.target, h.reason, h.emitter) == ("scribe", "prose is yours", "alice")

    def test_agent_message_step_is_live_progress(self) -> None:
        d = A2ADispatcher()
        assert (
            d.classify(StepEvent(kind="agent_message", correlation_id="c1", depth=0, emitter="alice", text="thinking…"))
            is None
        )

    def test_concurrent_consults_pair_independently(self) -> None:
        """A consults B and C in one hop (two open ids); interleaved replies must
        each pair to the right peer."""
        d = A2ADispatcher()
        d.classify(_call("tb", peer="scribe", message="x"))
        d.classify(_call("tc", peer="conan", message="y"))
        rc = d.classify(_result("tc", emitter="conan", text="from conan", name="conan"))
        rb = d.classify(_result("tb", emitter="scribe", text="from scribe", name="scribe"))
        assert isinstance(rc, A2AReply) and rc.peer == "conan" and rc.text == "from conan"
        assert isinstance(rb, A2AReply) and rb.peer == "scribe" and rb.text == "from scribe"

    def test_dangling_consults_exposed_for_fault_synthesis(self) -> None:
        """A faulted peer yields no tool_result (the run faults → RunFailed). The
        unanswered consult is surfaced so the bridge synthesizes a failure note (D-2)."""
        d = A2ADispatcher()
        d.classify(_call("t3", peer="scribe", message="x"))
        d.classify(_call("t4", peer="conan", message="y"))
        d.classify(_result("t3", emitter="scribe", text="done", name="scribe"))
        dangling = d.dangling()
        assert {c.peer for c in dangling} == {"conan"}

    def test_nested_consult_depth_gt_1_still_pairs(self) -> None:
        """A nested B→C consult (depth>1, same correlation_id) is observed too."""
        d = A2ADispatcher()
        d.classify(_call("tn", peer="codex", message="z", caller="conan", depth=2))
        rep = d.classify(_result("tn", emitter="codex", text="nested reply", depth=2, name="codex"))
        assert isinstance(rep, A2AReply) and rep.peer == "codex" and rep.caller == "conan"
