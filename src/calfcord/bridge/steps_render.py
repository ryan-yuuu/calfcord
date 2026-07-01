"""Pure step-render helpers — no Discord, no Kafka, no state (spec §5.1).

Two render surfaces share this module:

* **Live progress** — :func:`render_step_line` turns ONE normalized
  :class:`~calfcord.bridge.step_events.StepEvent` into one compact line (model
  text as prose, a ``tool_name(args)`` inline-code line, a short fenced ``⎿``
  result block). :func:`_tail_window` / :func:`_progress_content` shape an
  accumulated list of those lines into the transient progress message, bounded
  to Discord's per-message cap. The stateful lifecycle (post / debounced edit /
  delete) lives in :mod:`calfcord.bridge.progress`.
* **Full transcript** — :func:`_render_tree_blocks` projects a turn's
  ``message_history`` slice into the Claude-Code-style ``● tool(args)`` /
  ``⎿ result`` blocks behind the reply's ``⤵ steps`` button
  (:mod:`calfcord.bridge.steps_toggle`); its block COUNT is reused as the
  ``⤵ N steps`` label. This surface still operates on
  ``Sequence[ModelMessage]`` because it renders persisted deltas, and its
  output is byte-for-byte stable so stored transcripts keep rendering the same.

Everything here is pure: no I/O, no time, no mutable module state. That keeps
both surfaces trivially unit-testable and lets the live source change (it now
arrives as :class:`StepEvent` from the run ``stream()``, not a Kafka envelope)
without touching the renderers.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Final

from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

from calfcord.bridge.step_events import StepEvent

logger = logging.getLogger(__name__)

TRUNCATION_MARKER: Final[str] = "\n… (truncated)"

# --- Live progress body rendering -------------------------------------------
# The transient progress message shows the agent's ACTUAL work as it streams
# (model text, tool calls, tool returns) — not just a counter. It is edited in
# place, and Discord hard-caps message content at 2000 chars, so the body is a
# bounded *tail window*: the most recent lines that fit, with a marker when
# older steps are elided. The full, untruncated transcript remains on the
# reply's ``⤵ steps`` button after completion. These caps apply ONLY to this
# live view; the durable transcript / toggle renderer
# (:func:`_render_tree_blocks`) is untouched.

_DISCORD_MESSAGE_LIMIT: Final[int] = 2000
"""Discord's hard per-message content cap. The rendered progress body is
clamped under this so an ``edit_message`` never fails with a 400-too-long."""

_PROGRESS_BODY_MAX_CHARS: Final[int] = 1900
"""Tail-window budget for the progress body (the whole message — there is no
header line). Kept under :data:`_DISCORD_MESSAGE_LIMIT` to leave room for the
elision marker prepended when older lines are dropped; a final slice in
:func:`_progress_content` enforces the hard cap regardless."""

_LIVE_TEXT_MAX_CHARS: Final[int] = 1000
"""Per-part cap for a single model text part in the live body. Generous (it's
prose) but bounded so one long preamble can't dominate the window."""

_LIVE_TOOL_MAX_CHARS: Final[int] = 200
"""Per-part cap for a single ``tool_name(args)`` call line in the live body.
Tight on purpose — a tool call must never exceed one line, and short lines let
more recent steps stay visible in the tail window. The result line has its own
(multi-line) caps below."""

_LIVE_RETURN_MAX_LINES: Final[int] = 3
"""Max real lines kept from a tool result in the live body. The result is the
noisiest part; a handful of lines gives useful context while staying compact.
Beyond this the last kept line carries a ``… (truncated)`` marker — the full
result lives on the ⤵ transcript."""

_LIVE_RETURN_LINE_MAX_CHARS: Final[int] = 120
"""Per-line cap for a kept live-result line, so one very long line can't blow
the compact window (and also marks the result truncated)."""

_HIDDEN_STEPS_MARKER: Final[str] = "…  *(earlier steps hidden — full trace on the ⤵ button)*"
"""Prepended to the body when the tail window dropped older lines."""

_PROGRESS_DEBOUNCE_SECONDS: Final[float] = 1.0
"""Trailing-debounce window for progress-message edits. Coalesces a
burst of steps into one Discord ``edit_message`` call so a fast tool loop
doesn't hammer the per-channel webhook rate bucket (5 req / 2 s, shared
by co-tenant agents). The first renderable step posts immediately; only
subsequent edits are debounced. Consumed by :mod:`calfcord.bridge.progress`."""

# --- Full transcript tree renderer (the ⤵ steps expand view) ----------------
# Renders the turn's steps as a Claude-Code-style trace: model text as prose,
# each tool call as ``● tool(args)`` with its result nested under ``⎿``. Unlike
# the live view this is the FULL trace — NO per-part truncation; the only bound
# is Discord's message cap, beyond which steps_toggle attaches the whole render
# as a steps.md file. One string per visual block (a prose block, or a
# call+return pair); the block COUNT is reused by the reply poster as the
# ``⤵ N steps`` label, so a tool call and its result count as ONE step.

_TREE_CALL_MARKER: Final[str] = "●"
_TREE_RETURN_MARKER: Final[str] = "⎿"

_TRIPLE_BACKTICK_RUN: Final[re.Pattern[str]] = re.compile(r"`{3,}")


# --- Shared low-level text helpers ------------------------------------------


def _truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` with a visible marker.

    Returns ``text`` unchanged when it already fits.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _truncate_inline(text: str, max_chars: int) -> str:
    """Single-line truncate with a no-newline ellipsis.

    Unlike :func:`_truncate` (whose marker starts with ``\\n``), this keeps the
    result on one line so it is safe to wrap in Discord inline code.
    """
    if len(text) <= max_chars:
        return text
    ellipsis = "…"
    return text[: max_chars - len(ellipsis)] + ellipsis


def _collapse_ws(text: str) -> str:
    """Collapse all runs of whitespace (incl. newlines) to single spaces."""
    return " ".join(text.split())


def _inline_code(inner: str) -> str:
    """Wrap ``inner`` in Discord inline code, neutralizing backticks.

    Inline code cannot contain a backtick (it would close the span early), so
    any backtick in tool args/results is swapped for an apostrophe. This is a
    live preview; the exact bytes are preserved in the durable transcript.
    """
    return f"`{inner.replace('`', chr(39))}`"


def _fence_safe(content: str) -> str:
    """Neutralize runs of 3+ backticks so embedded fences can't break out.

    Discord closes a ``` code block at the next run of three-or-more
    backticks regardless of the opening fence's length, so a triple-backtick
    inside tool output would otherwise terminate the block early and spill the
    remainder as raw markdown. Weaving a zero-width space between the backticks
    of any 3+ run leaves the text visually identical while ensuring no raw
    ``` survives to close the fence. Single/double backticks are left untouched
    — they render literally inside a block. (The only cost: a steps.md copied
    from output that itself contained ``` carries invisible zero-width spaces.)
    """
    return _TRIPLE_BACKTICK_RUN.sub(lambda m: "\u200b".join("`" * len(m.group())), content)


def _fenced(content: str) -> str:
    """Wrap ``content`` in a code fence, neutralizing any inner ``` first."""
    return f"```\n{_fence_safe(content)}\n```"


def _format_call_args(args: dict[str, Any], *, collapse: bool = True) -> str:
    """Render tool-call args as keyword form: ``k=<json-value>, …``.

    ``args`` is the already-coerced argument dict — the live path gets it off
    :attr:`StepEvent.args` (the step seam normalized it) and the tree path gets
    it from :func:`_tool_call_args`, so both guarantee a dict and the old
    non-dict / unserializable fallback ladder is gone (an unparseable arg is
    coerced to ``{}`` upstream and renders as ``name()``). A flat object renders
    as ``city="Tokyo", n=5`` — each value JSON-encoded, so strings keep their
    quotes and booleans read as ``true``/``false``; nested values use compact
    separators to keep the line tight.

    ``collapse`` (default ``True``) folds runs of real whitespace to single
    spaces for the compact live preview. The full transcript tree passes
    ``collapse=False`` to keep byte fidelity — JSON already escapes real
    newlines, so the signature stays one line either way, but inner spacing in
    string values is preserved.
    """
    if not args:
        return ""
    pairs: list[str] = []
    for key, value in args.items():
        try:
            rendered_value = json.dumps(value, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            rendered_value = str(value)
        pairs.append(f"{key}={rendered_value}")
    joined = ", ".join(pairs)
    return _collapse_ws(joined) if collapse else joined


def _tool_call_args(call: ToolCallPart) -> dict[str, Any]:
    """Best-effort argument dict for a transcript ``ToolCallPart`` (never raises).

    The tree renderer projects persisted ``message_history`` (no step seam in
    front of it), so it coerces a tool call's raw args here the same way the
    seam does for the live path: a non-object or unparseable arg becomes ``{}``
    (rendered as ``name()``) rather than raising and losing the WHOLE stored
    transcript over one malformed call. ``args_as_dict`` is pydantic-ai's
    canonical accessor — it returns ``{}`` for empty args and asserts on a
    bare list/scalar — so well-formed object args render byte-for-byte as before.
    """
    try:
        args = call.args_as_dict()
    except Exception:
        # A bare list/scalar makes args_as_dict assert; malformed JSON makes it
        # raise. Either way the args aren't an object → render as ``name()``.
        return {}
    return args if isinstance(args, dict) else {}


def _pluralize_steps(count: int) -> str:
    """Render ``N step(s)`` with correct singular/plural for ``count``."""
    return f"{count} step" if count == 1 else f"{count} steps"


# --- Full transcript tree renderer ------------------------------------------


def _render_text_part(part: TextPart) -> str | None:
    """Render a ``TextPart`` into a transcript block, or ``None`` to skip.

    Whitespace-only content is skipped — empty preambles are common
    when the model emits a tool call with no narrative.
    """
    text = part.content.strip()
    if not text:
        return None
    return text


def _tool_tree_block(call: ToolCallPart, ret: ToolReturnPart | None) -> str:
    """Render a tool call and its (optional) result as one fenced tree block.

    ``● tool(args)`` on the first line; when a matching return is present, its
    result is nested under ``⎿`` with continuation lines aligned beneath the
    first result character. Args use the keyword form WITHOUT whitespace
    collapsing — this is the full view, so byte fidelity is preserved (real
    newlines are already JSON-escaped, so the signature stays one line). No
    truncation: the only bound is the overall message cap enforced upstream.
    """
    sig = f"{call.tool_name}({_format_call_args(_tool_call_args(call), collapse=False)})"
    lines = [f"{_TREE_CALL_MARKER} {sig}"]
    if ret is not None:
        first, *rest = ret.model_response_str().split("\n")
        lines.append(f"  {_TREE_RETURN_MARKER}  {first}")
        lines.extend(f"     {line}" for line in rest)
    return _fenced("\n".join(lines))


def _return_tree_block(ret: ToolReturnPart) -> str:
    """Render an orphan tool return (no call with its id in the slice) standalone.

    Practically unreachable — a tool call and its return live in the same
    agent run, after the history cursor, so they're sliced together. Rendered
    defensively so an orphan return is never silently dropped, which would also
    skew the step count that gates the ⤵ button.
    """
    first, *rest = ret.model_response_str().split("\n")
    lines = [f"{_TREE_RETURN_MARKER}  {first}"]
    lines.extend(f"   {line}" for line in rest)
    return _fenced("\n".join(lines))


def _render_tree_blocks(messages: Sequence[ModelMessage]) -> list[str]:
    """Project the turn's ``message_history`` slice into full tree blocks.

    The VERBOSE/full renderer behind the reply's ``⤵ steps`` expand view
    (:mod:`calfcord.bridge.steps_toggle`) and the source of the
    reply poster's step COUNT (``len(...)`` → the ``⤵ N steps`` label). It is NOT
    used by the live progress message — that path renders one
    :class:`StepEvent` at a time via :func:`render_step_line`. Walks the delta
    in order, emitting one string per visual block:

    * a model ``TextPart`` → a prose block (whitespace-only skipped);
    * a ``ToolCallPart`` → ``● tool(args)`` with its matched ``ToolReturnPart``
      (looked up by ``tool_call_id``) nested under ``⎿`` — a call and its
      result are ONE block, so the step count credits a tool use once.

    Skips the same parts as the live renderer (``ThinkingPart``, ``FilePart``,
    ``BuiltinTool*Part``, ``UserPromptPart`` / ``SystemPromptPart``,
    ``RetryPromptPart`` — see the original v1 rationale).

    Pairing is purely by id and independent of message order: a return is
    folded into its call iff a call with that id exists anywhere in the slice;
    a return whose call is absent renders standalone (so nothing is dropped,
    and the orphan path can't double-render a return that arrives before its
    call). Output order follows message order. Duplicate ``tool_call_id``s
    don't occur in well-formed pydantic-ai history; on a collision the last
    return for an id wins.

    Caller wraps this in a try/except — ``model_response_str`` can raise on
    malformed payloads.
    """
    # Two index passes (order-independent): which ids have a call in the
    # slice, and the return for each id. A return is then an orphan iff its id
    # has no call here — decided without relying on walk order.
    call_ids: set[str] = set()
    returns_by_id: dict[str, ToolReturnPart] = {}
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    call_ids.add(part.tool_call_id)
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    returns_by_id[part.tool_call_id] = part

    out: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    rendered = _render_text_part(part)
                    if rendered is not None:
                        out.append(rendered)
                elif isinstance(part, ToolCallPart):
                    out.append(_tool_tree_block(part, returns_by_id.get(part.tool_call_id)))
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart) and part.tool_call_id not in call_ids:
                    out.append(_return_tree_block(part))
    return out


# --- Compact live renderer (Hybrid style: prose text + inline-code tools) ----


def _render_tool_result_line(text: str) -> str:
    """Compact fenced result: ``⎿`` first line + up to a few aligned more.

    Keeps the first :data:`_LIVE_RETURN_MAX_LINES` real lines of ``text`` (the
    seam already concatenated the result's text parts) so multi-line output
    stays readable in the live stream, but bounded. The two kinds of truncation
    are marked where they happen: a line longer than
    :data:`_LIVE_RETURN_LINE_MAX_CHARS` is cut with a trailing ``…`` ON THAT
    line, and when whole lines are dropped beyond the kept few a
    ``… (truncated)`` marker is appended to the last kept line. Wrapped in a
    fence (embedded ``` is neutralized) so a stray triple-backtick can't break
    the progress message; the full, untruncated result lives on the ⤵ transcript.
    """
    lines = text.split("\n")
    kept: list[str] = []
    for line in lines[:_LIVE_RETURN_MAX_LINES]:
        line = line.rstrip()
        if len(line) > _LIVE_RETURN_LINE_MAX_CHARS:
            # Mark the cut on the line that was actually cut, not elsewhere.
            line = line[: _LIVE_RETURN_LINE_MAX_CHARS - 1].rstrip() + "…"
        kept.append(line)
    if not kept:
        kept = [""]
    if len(lines) > _LIVE_RETURN_MAX_LINES:
        # Whole lines were dropped — flag it at the end of the block.
        kept[-1] = f"{kept[-1]} … (truncated)".strip()
    first, *rest = kept
    tree = [f"{_TREE_RETURN_MARKER} {first}"]
    tree.extend(f"  {line}" for line in rest)
    return _fenced("\n".join(tree))


def render_step_line(step: StepEvent) -> str | None:
    """Render ONE :class:`StepEvent` into a single compact live line.

    Returns ``None`` when the step has nothing to show (a whitespace-only
    ``agent_message`` preamble, or a ``handoff`` — which never reaches the live
    renderer anyway, since the A2A dispatcher claims it upstream). Otherwise:

    * ``agent_message`` → the stripped text as plain markdown prose, capped at
      :data:`_LIVE_TEXT_MAX_CHARS` (this is only ever a NON-terminal preamble:
      the terminal answer never arrives as a step — it rides ``handle.result()``,
      posted by the reply poster — so there is no flash/terminal special case);
    * ``tool_call`` → one inline-code ``tool_name(args)`` line capped at
      :data:`_LIVE_TOOL_MAX_CHARS`;
    * ``tool_result`` → a short fenced ``⎿`` block (see
      :func:`_render_tool_result_line`).
    """
    if step.kind == "agent_message":
        text = step.text.strip()
        if not text:
            return None
        return _truncate(text, _LIVE_TEXT_MAX_CHARS)
    if step.kind == "tool_call":
        call = f"{step.name}({_format_call_args(step.args or {})})"
        return _inline_code(_truncate_inline(call, _LIVE_TOOL_MAX_CHARS))
    if step.kind == "tool_result":
        return _render_tool_result_line(step.text)
    # handoff (or any future kind): claimed by the A2A dispatcher upstream, so
    # it never reaches on_step — nothing to render live.
    return None


def _tail_window(lines: Sequence[str], max_chars: int) -> str:
    """Join the most recent ``lines`` that fit in ``max_chars`` (newline-joined).

    Walks from the end, keeping lines until the next one would overflow the
    budget (the most recent line is always kept, even if oversized — the final
    clamp in :func:`_progress_content` guards the hard cap). When older lines
    were dropped, prepends :data:`_HIDDEN_STEPS_MARKER` so the elision is
    visible. Returns ``""`` for no lines.
    """
    if not lines:
        return ""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        cost = len(line) + 1  # the newline that will join it
        if kept and total + cost > max_chars:
            break
        kept.append(line)
        total += cost
    kept.reverse()
    if len(kept) < len(lines):
        return _HIDDEN_STEPS_MARKER + "\n" + "\n".join(kept)
    return "\n".join(kept)


def _progress_content(lines: Sequence[str]) -> str:
    """Render the transient progress message body: the live trace, nothing else.

    A tail-windowed compact render of the accumulated ``lines`` — the agent's
    actual work as it streams (model text, ``tool_name(args)``, ``⎿ result``),
    most-recent-fits-first. No header/counter line: the message IS the trace.
    Hard-clamped to :data:`_DISCORD_MESSAGE_LIMIT` so an in-place
    ``edit_message`` can never fail for length (the tail window keeps it well
    under; this slice is a final safety net).

    The message only ever posts/edits on a renderable step, so ``lines`` is
    non-empty whenever this is called; the empty-body fallback (a lone ``…``)
    is purely defensive — Discord rejects an empty-content message.
    """
    body = _tail_window(lines, _PROGRESS_BODY_MAX_CHARS)
    if not body:
        # Unreachable in practice — the message only posts/edits on a
        # renderable step, so ``lines`` is non-empty here. The lone "…"
        # keeps a future regression from sending empty content (Discord
        # rejects it with a 400) while surfacing the anomaly in the log.
        logger.warning("steps: progress content rendered empty (unexpected); sending a placeholder")
        return "…"
    return body[:_DISCORD_MESSAGE_LIMIT]
