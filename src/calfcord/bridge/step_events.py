"""Normalized intermediate run-step event — the bridge's renderers and the A2A
dispatcher depend on THIS, never on calfkit's ``RunEvent`` union.

The single adapter :func:`normalize_run_event` is the only code that knows the
calfkit transport types, so the step source can change without touching the
renderers (spec §5.1, "swappable step-source seam").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from calfkit.client import RunEvent

StepKind = Literal["agent_message", "tool_call", "tool_result", "handoff"]


@dataclass(frozen=True)
class StepEvent:
    """One normalized intermediate step from a run's ``stream()``.

    Carries the union of fields the renderers need; which are populated depends
    on ``kind``. ``correlation_id`` + ``depth`` + ``emitter`` are always set
    (every calfkit step event carries them — steps for the whole run tree reach
    the root caller, attributed by ``emitter``/``depth``).
    """

    kind: StepKind
    correlation_id: str
    depth: int
    emitter: str
    text: str = ""
    """Rendered text — the concatenated ``TextPart`` content of an
    ``agent_message`` or a ``tool_result``."""
    tool_call_id: str | None = None
    name: str | None = None
    """``tool_call``: the tool name. ``tool_result``: the result emitter's name
    (the peer, on an A2A reply)."""
    args: dict[str, Any] | None = None
    """``tool_call`` arguments (normalized to a dict)."""
    is_error: bool = False
    """``tool_result``: the call raised / was rejected."""
    target: str | None = None
    """``handoff``: the agent control transfers to."""
    reason: str | None = None
    """``handoff``: the model's stated reason."""


def _render_text(parts: list[Any]) -> str:
    """Concatenate the ``TextPart`` text of a step's parts — the human-readable
    content the renderers show; non-text parts (files, data) are skipped."""
    return "".join(p.text for p in parts if getattr(p, "kind", None) == "text")


def _args_to_dict(args: str | dict[str, Any] | None) -> dict[str, Any]:
    """Normalize ``ToolCallEvent.args`` (a JSON ``str``, a dict, or ``None``)."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_run_event(event: RunEvent) -> StepEvent | None:
    """Adapt a calfkit ``RunEvent`` into a :class:`StepEvent`, or ``None`` for the
    terminals (``RunCompleted`` / ``RunFailed`` — handled by ``handle.result()``).

    The ONLY code that knows calfkit's step-event types; the renderers and the
    A2A dispatcher depend on :class:`StepEvent`, so the transport can change
    behind this one seam.
    """
    kind = getattr(event, "kind", None)
    if kind == "agent_message":
        return StepEvent(
            kind="agent_message",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            text=_render_text(event.parts),
        )
    if kind == "tool_call":
        return StepEvent(
            kind="tool_call",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            tool_call_id=event.tool_call_id,
            name=event.name,
            args=_args_to_dict(event.args),
        )
    if kind == "tool_result":
        return StepEvent(
            kind="tool_result",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            tool_call_id=event.tool_call_id,
            name=event.name,
            text=_render_text(event.parts),
            is_error=event.is_error,
        )
    if kind == "handoff":
        return StepEvent(
            kind="handoff",
            correlation_id=event.correlation_id,
            depth=event.depth,
            emitter=event.emitter,
            target=event.target,
            reason=event.reason,
        )
    return None
