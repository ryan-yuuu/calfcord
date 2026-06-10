"""Unit tests for first-reply detection (§4.6 / §12.2).

The init wizard's live finish watches ``discord.outbox`` for the FIRST reply
emitted by the agent it just started, to prove the org is live end-to-end. The
replying agent's identity is NOT in the envelope JSON — it rides the Kafka
headers (``x-calf-emitter`` / ``x-calf-emitter-kind``) and is recoverable only
through a calfkit consumer handler's ``_stamp_transport`` (mirrors the bridge's
own outbox consumer). So the detector is a :class:`ConsumerNode`, and these
tests drive its ``handler`` directly with synthetic envelopes — exercising the
gate, the emitter-kind / emitter-id match, and the empty-output skip without a
broker, FastStream, or an LLM.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from calfkit.models import State, TextPart
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    SessionRunContext,
    WorkflowState,
)

from calfcord.control_plane import first_reply as first_reply_mod
from calfcord.control_plane.first_reply import make_first_reply_node, wait_for_first_reply

_CORRELATION_ID = "evt-1"
_TARGET_AGENT = "assistant"


def _envelope(*, final_text: str | None = "Hi there!") -> Envelope:
    """Build a synthetic envelope mimicking an agent's ``ReturnCall`` publish.

    ``final_text=None`` produces an envelope with no ``final_output_parts``
    (an intermediate hop the gate must skip). Anything else is wrapped in a
    single ``TextPart``, matching how an assistant agent emits its final reply.
    """
    state = State()
    if final_text is not None:
        state.final_output_parts = [TextPart(text=final_text)]
    call_stack = CallFrameStack()
    # ``Envelope`` requires a non-empty ``WorkflowState`` to validate; the
    # consumer never reads the call stack itself.
    call_stack.push(
        CallFrame(
            target_topic="discord.outbox",
            callback_topic="discord.outbox",
        )
    )
    return Envelope(
        internal_workflow_state=WorkflowState(call_stack=call_stack),
        context=SessionRunContext(state=state, deps={}),
    )


def _headers(
    *,
    emitter: str | None = _TARGET_AGENT,
    emitter_kind: str | None = "agent",
) -> dict[str, Any]:
    h: dict[str, Any] = {}
    if emitter is not None:
        h["x-calf-emitter"] = emitter
    if emitter_kind is not None:
        h["x-calf-emitter-kind"] = emitter_kind
    return h


class _Broker:
    """Minimal stand-in: the consumer handler only needs an object to pass to
    its no-op gate evaluation; it never publishes."""


async def _drive(
    node: Any,
    *,
    envelope: Envelope | None = None,
    headers: dict[str, Any] | None = None,
    correlation_id: str = _CORRELATION_ID,
) -> None:
    await node.handler(
        envelope=envelope if envelope is not None else _envelope(),
        correlation_id=correlation_id,
        headers=headers if headers is not None else _headers(),
        broker=_Broker(),
    )


async def test_matching_agent_reply_fires_callback() -> None:
    matched: list[str] = []

    node = make_first_reply_node(_TARGET_AGENT, on_match=lambda: matched.append("hit"))
    await _drive(node)

    assert matched == ["hit"]


async def test_callback_receives_correlation_id() -> None:
    """The detector surfaces the correlation_id so a caller could tie the live
    reply back to the inbound wire's ``event_id`` (the bridge's own gate idiom)."""
    seen: list[str] = []

    node = make_first_reply_node(
        _TARGET_AGENT, on_match=lambda: None, on_match_correlation=seen.append
    )
    await _drive(node, correlation_id="evt-live-42")

    assert seen == ["evt-live-42"]


async def test_non_agent_emitter_does_not_fire() -> None:
    """A tool / client emitter on the topic is not an agent reply."""
    matched: list[str] = []

    node = make_first_reply_node(_TARGET_AGENT, on_match=lambda: matched.append("hit"))
    await _drive(node, headers=_headers(emitter_kind="tool"))

    assert matched == []


async def test_wrong_agent_id_does_not_fire() -> None:
    """A DIFFERENT agent replying (multi-agent org) must not satisfy a watch
    scoped to the agent the wizard just started."""
    matched: list[str] = []

    node = make_first_reply_node(_TARGET_AGENT, on_match=lambda: matched.append("hit"))
    await _drive(node, headers=_headers(emitter="scheduler"))

    assert matched == []


async def test_missing_emitter_header_does_not_fire() -> None:
    """A non-calfkit producer (no emitter header) is not a match."""
    matched: list[str] = []

    node = make_first_reply_node(_TARGET_AGENT, on_match=lambda: matched.append("hit"))
    await _drive(node, headers=_headers(emitter=None, emitter_kind=None))

    assert matched == []


async def test_intermediate_hop_is_gated_out() -> None:
    """An envelope with empty ``final_output_parts`` (a mid-loop / tool hop) is
    filtered by the same ``final_output_parts`` gate the bridge outbox uses, so
    the callback never fires on an intermediate hop even from the right agent."""
    matched: list[str] = []

    node = make_first_reply_node(_TARGET_AGENT, on_match=lambda: matched.append("hit"))
    await _drive(node, envelope=_envelope(final_text=None))

    assert matched == []


async def test_callback_fires_once_per_matching_envelope() -> None:
    """Two matching replies (e.g. a retry) each invoke the callback; the
    one-shot semantics (stop after first) live in the orchestrator, not the
    node, keeping the node a pure stateless matcher."""
    matched: list[str] = []

    node = make_first_reply_node(_TARGET_AGENT, on_match=lambda: matched.append("hit"))
    await _drive(node)
    await _drive(node)

    assert matched == ["hit", "hit"]


# --------------------------------------------------------------------------- #
# Orchestrator: wait_for_first_reply trusts the MANAGED Worker's provisioning
# --------------------------------------------------------------------------- #


class _FakeClient:
    """Injected transient client: ``wait_for_first_reply`` passes it to ``Worker``
    but never touches it directly when a client is injected (it owns no close)."""


async def test_wait_for_first_reply_trusts_managed_provisioning_no_extra_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher uses a MANAGED Worker whose ``start()`` ensurer already
    provisions the node's subscribe topic (``discord.outbox``), so the
    orchestrator must NOT also call the standalone ``provision_extra_topics`` —
    matching how the tools/router/agents managed runners trust managed
    provisioning. A redundant explicit provision would be the regression this
    guards against."""
    # The standalone provisioner must not be referenced at all: the managed
    # Worker.start() ensurer is the sole provisioning path for the subscribe
    # topic. Re-importing it into this module (to call it explicitly again) is
    # exactly the regression this guards against.
    assert not hasattr(first_reply_mod, "provision_extra_topics")

    captured: dict[str, Any] = {}

    def _capture_node(agent_id: str, *, on_match: Any, **_: Any) -> object:
        # Capture the orchestrator's match callback so the fake Worker can fire it
        # the moment its (managed, provisioning) start joins the group — mimicking
        # the reply landing without reaching into the real node's internals.
        captured["on_match"] = on_match
        return object()

    class _FakeWorker:
        """Managed Worker stand-in: ``start()`` is the surface whose startup
        ensurer provisions the node's subscribe topic; here it just fires the
        captured match so the wait returns ``True`` before its timeout."""

        started = False

        def __init__(self, client: Any, nodes: list[Any]) -> None:
            pass

        async def start(self) -> None:
            _FakeWorker.started = True
            captured["on_match"]()

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(first_reply_mod, "make_first_reply_node", _capture_node)
    monkeypatch.setattr(first_reply_mod, "Worker", _FakeWorker)

    detected = await wait_for_first_reply(
        "localhost:9092", agent_id=_TARGET_AGENT, timeout_s=5.0, client=_FakeClient()
    )

    assert detected is True
    assert _FakeWorker.started is True  # the managed start (which provisions) ran


async def test_wait_for_first_reply_sets_ready_after_group_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait_for_first_reply`` signals an injected ``ready`` event AFTER
    ``worker.start()`` joins the consumer group, BEFORE blocking on the match.

    The init wizard relies on this signal to prompt the human only once the
    ``latest``-offset watcher is actually listening — otherwise a fast human
    posts ``@agent hello`` before the group joins and the reply is missed. We
    drive a Worker that joins (start succeeds) but never sees a matching reply,
    so the wait times out; ``ready`` must STILL be set (readiness is the join,
    not the reply)."""
    ready = asyncio.Event()

    def _capture_node(agent_id: str, *, on_match: Any, **_: Any) -> object:
        return object()

    join_order: list[str] = []

    class _FakeWorker:
        def __init__(self, client: Any, nodes: list[Any]) -> None:
            pass

        async def start(self) -> None:
            # Joined the group; readiness must not yet be signalled by us — the
            # orchestrator owns setting it right after this returns.
            join_order.append("started")

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(first_reply_mod, "make_first_reply_node", _capture_node)
    monkeypatch.setattr(first_reply_mod, "Worker", _FakeWorker)

    detected = await wait_for_first_reply(
        "localhost:9092",
        agent_id=_TARGET_AGENT,
        timeout_s=0.05,  # no reply arrives → clean, bounded timeout
        client=_FakeClient(),
        ready=ready,
    )

    assert detected is False  # nobody replied; bounded timeout, never hangs
    assert ready.is_set()  # readiness was signalled despite the timeout
    assert join_order == ["started"]
