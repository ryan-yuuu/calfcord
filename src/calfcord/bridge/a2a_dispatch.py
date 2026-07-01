"""Stateful classifier that splits A2A activity from live progress on a run's
stream (D-1/D-2).

A native ``message_agent`` consult is a ``tool_call`` step (name
``"message_agent"``); its reply is a ``tool_result`` whose ``tool_call_id``
matches. The reply CANNOT be classified by its own fields — on the happy path
its ``emitter``/``name`` is the *peer*, and a rejected consult's is the
*caller* — so the dispatcher records each consult's ``tool_call_id`` and routes
the matching result to A2A. This is reliable because the whole run shares one
``correlation_id`` (single partition → request-before-reply order) and the
handle stream is lossless and ordered.

Peer identity for the projected thread always comes from the *request*'s
``args["name"]`` (the one source stable across success and rejection), recorded
in :class:`A2ACall`.
"""

from __future__ import annotations

from dataclasses import dataclass

from calfcord.bridge.step_events import StepEvent

_MESSAGE_AGENT = "message_agent"


@dataclass(frozen=True)
class A2ACall:
    """A recorded ``message_agent`` consult still awaiting its reply."""

    tool_call_id: str
    correlation_id: str
    caller: str
    peer: str
    message: str


@dataclass(frozen=True)
class A2ARequest:
    """Render the consult request into the per-``correlation_id`` thread under
    the caller's persona."""

    correlation_id: str
    tool_call_id: str
    caller: str
    peer: str
    message: str


@dataclass(frozen=True)
class A2AReply:
    """Render the peer's reply under the peer's persona (happy path)."""

    correlation_id: str
    tool_call_id: str
    caller: str
    peer: str
    text: str


@dataclass(frozen=True)
class A2AReject:
    """Render a rejected consult (peer offline / cycle / self) as a system note,
    not a peer post (``is_error`` result, ``emitter==caller``)."""

    correlation_id: str
    tool_call_id: str
    caller: str
    peer: str
    text: str


@dataclass(frozen=True)
class A2AHandoff:
    """Render a handoff note."""

    correlation_id: str
    emitter: str
    target: str
    reason: str


A2AProjection = A2ARequest | A2AReply | A2AReject | A2AHandoff


class A2ADispatcher:
    """Classify each :class:`StepEvent` as an A2A render instruction or
    ``None`` (live progress). One dispatcher per run — its open-consult state is
    that run's."""

    def __init__(self) -> None:
        self._open: dict[str, A2ACall] = {}

    def classify(self, step: StepEvent) -> A2AProjection | None:
        if step.kind == "handoff":
            return A2AHandoff(
                correlation_id=step.correlation_id,
                emitter=step.emitter,
                target=step.target or "",
                reason=step.reason or "",
            )
        if step.kind == "tool_call" and step.name == _MESSAGE_AGENT:
            args = step.args or {}
            call = A2ACall(
                tool_call_id=step.tool_call_id or "",
                correlation_id=step.correlation_id,
                caller=step.emitter,
                peer=str(args.get("name", "")),
                message=str(args.get("message", "")),
            )
            self._open[call.tool_call_id] = call
            return A2ARequest(
                correlation_id=call.correlation_id,
                tool_call_id=call.tool_call_id,
                caller=call.caller,
                peer=call.peer,
                message=call.message,
            )
        if step.kind == "tool_result" and step.tool_call_id is not None and step.tool_call_id in self._open:
            call = self._open.pop(step.tool_call_id)
            cls = A2AReject if step.is_error else A2AReply
            return cls(
                correlation_id=call.correlation_id,
                tool_call_id=call.tool_call_id,
                caller=call.caller,
                peer=call.peer,
                text=step.text,
            )
        return None

    def dangling(self) -> list[A2ACall]:
        """Consults with no reply yet — a faulted peer faults the whole run
        (RunFailed, no ``tool_result``), so on stream end the bridge synthesizes
        a failure note for each of these (D-2)."""
        return list(self._open.values())
