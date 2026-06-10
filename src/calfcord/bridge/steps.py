"""Discord steps consumer — streams every assistant agent's intermediate
hops into a single transient in-channel "progress" message.

A long-lived calfkit :class:`ConsumerNode` subscribed to
:data:`~calfcord.topics.AGENT_STEPS_TOPIC` (``agent.steps``)
in its own Kafka consumer group. Every assistant agent's handler hop —
``Call`` envelopes (tool dispatch), ``TailCall`` retries, the terminal
``ReturnCall`` — is mirrored to that topic by FastStream's
``@publisher`` decorator (see
:meth:`calfkit.worker.Worker.register_handlers` and the agent factory's
``publish_topic=AGENT_STEPS_TOPIC`` injection). The consumer walks each
hop's ``state.message_history`` delta and renders the new
:class:`TextPart` / :class:`ToolCallPart` / :class:`ToolReturnPart`
entries into one transient progress message posted under the agent's
persona in the user's original channel — streaming the agent's ACTUAL
work as it happens: model text as prose, tool calls as
``tool_name(args)`` lines, and tool returns as a short fenced ``⎿ result``
block (the first few lines) — the
message IS the trace, with no header/counter line. It is a bounded *tail
window* (the most recent lines that fit Discord's 2000-char cap); the full
untruncated transcript stays on the reply's ``⤵ steps`` button.

Why this exists: the bridge's outbox consumer
(:func:`~calfcord.bridge.outbox.build_outbox_consumer`)
gates on ``state.final_output_parts``, so it only ever posts the
agent's terminal reply. When the model emits text alongside tool calls,
that text rides on the same ``ModelResponse`` as the ``ToolCallPart``
but is never projected to ``final_output_parts`` (see
``calfkit/nodes/agent.py`` — the ``DeferredToolRequests`` branch
extends ``message_history`` but does not set ``final_output_parts``).
Without this consumer, the model's running commentary and the tool
calls themselves are invisible to the user while the agent works.

How the wire is recovered: same pattern as the outbox.
:class:`ConsumerContext` carries ``state``, ``correlation_id``, and
``emitter_node_id`` but not the original inbound wire. The bridge's
:class:`~calfcord.bridge.pending_wires.PendingWires` map
(populated by :class:`BridgeIngress` on the way in) gives us the
parent Discord ``channel_id`` / ``message_id`` to post against, and
the pre-invocation ``message_history`` length to seed the
:attr:`StepsEntry.history_cursor` so the channel-history prefix
projected by :func:`~calfcord.bridge.history.project_history`
does not get re-counted as fresh steps (a bug class the
``initial_message_history_length`` field exists to close).

**Progress-message lifecycle.**

* **Post** — lazily, on the first hop that produces a renderable step.
  A pure-text first-turn reply (no tools, no preamble) never posts a
  progress message; the outbox path posts the final reply in the parent
  channel and that's the end of it.
* **Edit (debounced)** — every subsequent hop that yields renderable
  content appends to the entry's :attr:`StepsEntry.rendered_lines` and
  schedules a trailing, debounced edit (:data:`_PROGRESS_DEBOUNCE_SECONDS`).
  At most one edit task is pending per entry; lines that land while it sleeps
  are picked up because the task re-renders the entry at fire time. The edit
  keeps the message's components/embeds (none here) and only rewrites the text.
* **Delete** — on the terminal hop (``state.final_output_parts`` is set),
  any pending debounce edit is cancelled and the progress message is
  deleted. The outbox's final reply (which carries the ``⤵ steps`` toggle)
  supersedes it, so the transient progress message leaves no permanent
  residue in the channel.

No database access happens here: this consumer is pure live-UI. The
durable transcript is written by the outbox consumer on the terminal hop
(a later phase) — out of scope for this module.

**Terminal hop also counts the prior delta.** When the agent emits a
``ToolCall`` then a ``ToolReturn`` then a final ``TextPart`` in three
hops, the tool result lives in the terminal envelope's
``message_history`` delta (the new ``ModelRequest(ToolReturnPart)``
is appended in the same ``run()`` call that produces the final
``ModelResponse``). The consumer counts the delta *up to but not
including* the final ``ModelResponse``; that final ``ModelResponse`` is
the answer text, which the outbox posts to the parent channel.

**Source-was-already-a-thread.** When the inbound wire originated
inside a Discord thread, the bridge's normalizer flattens
``wire.channel_id`` to the parent channel for Kafka topic routing
while ``wire.source_channel_id`` keeps the thread id (exposed as
:attr:`~calfcord.bridge.wire.WireMessage.thread_id`). The
consumer seeds :attr:`StepsEntry.thread_id` from it and posts/edits/
deletes the progress message *inside* that thread — the persona webhook
still hosts on the flattened parent ``channel_id``, but the
``thread_id`` argument routes the message into the thread, so progress
streams where the user is actually talking. Identical behavior to a
top-level channel; ``thread_id`` is simply ``None`` there.

**Outbox retries.** The bridge's outbox path re-invokes the agent on
``agent.{aid}.in`` with the **same** ``correlation_id`` after a
Discord-post failure (see
:func:`~calfcord.bridge.outbox._publish_retry`). Without a
completion guard, the retry's first hop would seed a fresh
:class:`StepsEntry` and post a second progress message off the same
parent channel — the original was already deleted. The consumer guards
against this by checking :meth:`StepsState.is_completed` before seeding;
the terminal hop marks the correlation completed even when no progress
message was ever posted (so retries of pure-text replies are also
suppressed).

**Co-tenant peer envelopes — persona resolved per hop.** Every agent
subscribed to the inbound channel topic flows through calfkit's
``handler()`` (``calfkit/nodes/base.py:268-278``), including peers
whose gates filtered the envelope — those still return
``Response(body=envelope_unchanged, headers=self._emitter_headers())``
and FastStream's ``@publisher`` decorator mirrors them to
``agent.steps`` with the *peer's* emitter headers. The consumer
sidesteps the "which agent owns this entry" question by not caching
persona on :class:`StepsEntry` at all: each post/edit resolves persona
from ``result.emitter_node_id`` at post time, matching the outbox's
pattern. A peer envelope with an empty message-history delta produces
no count change and therefore no persona writes; any entry it seeds is
just channel/message/cursor scaffolding that the real emitter's first
content-bearing hop reuses. The real emitter's hops post under their
own identity. As a secondary benefit the consumer skips no-delta
non-terminal envelopes before rendering, which removes most of the
gated-out peer cost.

**Failure semantics.** Every Discord operation is wrapped in a
try/except that catches the common Discord error subclasses
(``NotFound`` → already-gone, ignored at DEBUG; ``Forbidden`` and
``DiscordException`` — broader than ``HTTPException`` so the sibling
``RateLimited`` is also funneled through — warned and swallowed). A
Discord failure on the progress surface must never affect the
final-reply path. :func:`_render_live_delta` is also wrapped because
:meth:`ToolCallPart.args_as_json_str` can raise on malformed args; an
unhandled exception there would otherwise loop because the cursor
advances after rendering and the same bad message would be re-walked
on the next hop.

**State loss on restart.** :class:`StepsState` is process-local.
A bridge restart strands every in-flight entry; the next hop after
restart finds no entry, logs DEBUG, and skips. The agent's final
reply still posts (the outbox path is independent and re-derives the
wire from :class:`PendingWires`, which has the same restart
vulnerability — accepted v1 trade-off shared across both paths). The
terminal-hop delete is the one casualty: a progress message whose
terminal hop arrived during the down window lingers in the channel
(cosmetic; operators can delete it manually).

**Partition-key requirement.**
:data:`AGENT_STEPS_TOPIC` MUST be configured with a single partition
(or every agent's hops must hash to the same partition by some other
means) until calfkit's publisher decorator carries the correlation-id
as a Kafka key. FastStream's ``@publisher`` decorator wraps the
calfkit handler's plain ``Response`` return without a key, so on a
multi-partition topic the hops for one ``correlation_id`` can
round-robin partitions and arrive out of order — cursor jumps swallow
deltas, and an intermediate hop arriving after a terminal hop would
post a second un-deleted progress message. The bridge's direct
:meth:`calfkit.Client.publish` calls do stamp the key (see
``calfkit/nodes/base.py``); the gap is only the publisher-decorator
mirror path that ``publish_topic=AGENT_STEPS_TOPIC`` activates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Sequence
from typing import Final

import discord
from calfkit import ConsumerNode
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from calfkit.models import ConsumerContext

from calfcord.bridge.pending_wires import PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.steps_state import StepsEntry, StepsState
from calfcord.discord.persona import (
    DiscordPersonaSender,
    Persona,
)
from calfcord.discord.typing import TypingNotifier
from calfcord.topics import AGENT_STEPS_TOPIC

logger = logging.getLogger(__name__)

DEFAULT_STEPS_CONSUMER_NODE_ID: Final[str] = "discord-steps-sink"

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
"""Per-part cap for a single model TextPart in the live body. Generous (it's
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
burst of hops into one Discord ``edit_message`` call so a fast tool loop
doesn't hammer the per-channel webhook rate bucket (5 req / 2 s, shared
by co-tenant agents). The first renderable hop posts immediately; only
subsequent edits are debounced."""


def _truncate(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ``max_chars`` with a visible marker.

    Returns ``text`` unchanged when it already fits.
    """
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _render_text_part(part: TextPart) -> str | None:
    """Render a ``TextPart`` into the message body to post, or ``None`` to skip.

    Whitespace-only content is skipped — empty preambles are common
    when the model emits a tool call with no narrative.
    """
    text = part.content.strip()
    if not text:
        return None
    return text


# --- Full transcript tree renderer (the ⤵ steps expand view) ----------------
# Renders the turn's steps as a Claude-Code-style trace: model text as prose,
# each tool call as ``● tool(args)`` with its result nested under ``⎿``. Unlike
# the live view this is the FULL trace — NO per-part truncation; the only bound
# is Discord's message cap, beyond which steps_toggle attaches the whole render
# as a steps.md file. One string per visual block (a prose block, or a
# call+return pair); the block COUNT is reused by the outbox as the
# ``⤵ N steps`` label, so a tool call and its result count as ONE step.

_TREE_CALL_MARKER: Final[str] = "●"
_TREE_RETURN_MARKER: Final[str] = "⎿"

_TRIPLE_BACKTICK_RUN: Final[re.Pattern[str]] = re.compile(r"`{3,}")


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


def _tool_tree_block(call: ToolCallPart, ret: ToolReturnPart | None) -> str:
    """Render a tool call and its (optional) result as one fenced tree block.

    ``● tool(args)`` on the first line; when a matching return is present, its
    result is nested under ``⎿`` with continuation lines aligned beneath the
    first result character. Args use the keyword form WITHOUT whitespace
    collapsing — this is the full view, so byte fidelity is preserved (real
    newlines are already JSON-escaped, so the signature stays one line). No
    truncation: the only bound is the overall message cap enforced upstream.
    """
    sig = f"{call.tool_name}({_format_call_args(call, collapse=False)})"
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
    outbox's step COUNT (``len(...)`` → the ``⤵ N steps`` label). It is NOT
    used by the live progress message — that path uses the compact
    :func:`_render_live_delta`. Walks the delta in order, emitting one string
    per visual block:

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

    Caller wraps this in a try/except — ``args_as_json_str`` /
    ``model_response_str`` can raise on malformed payloads.
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


def _format_call_args(part: ToolCallPart, *, collapse: bool = True) -> str:
    """Render a tool call's args as keyword form: ``k=<json-value>, …``.

    Uses ``args_as_dict`` so a flat object renders as ``city="Tokyo", n=5``
    (each value JSON-encoded, so strings keep their quotes and booleans read
    as ``true``/``false``). Args that aren't a JSON object (a bare list or
    scalar) fall back to the compact JSON string.

    ``collapse`` (default ``True``) folds runs of real whitespace to single
    spaces for the compact live preview. The full transcript tree passes
    ``collapse=False`` to keep byte fidelity — JSON already escapes real
    newlines, so the signature stays one line either way, but inner spacing in
    string values is preserved.

    NEVER raises: a deeply-malformed args object that even ``args_as_json_str``
    can't serialize renders as ``…`` so the live preview (and the durable
    transcript, which also routes through here) shows *something* rather than
    losing the whole turn. That total failure IS logged so the dropped args are
    diagnosable — historically the transcript path raised here and degraded
    loudly via the outbox guard; the swallow keeps the rest of the trace, but
    must not be silent about the loss.
    """
    try:
        args = part.args_as_dict()
    except Exception:
        # args isn't a JSON object (a bare list/scalar makes args_as_dict
        # assert; malformed JSON makes it raise).
        args = None
    if not isinstance(args, dict):
        # Either the parse failed above, OR — under ``python -O``, where the
        # ``assert isinstance(..., dict)`` inside args_as_dict is stripped — a
        # non-object (e.g. a list) slipped through and would blow up the
        # ``.items()`` loop below. Fall back to the raw JSON string, itself
        # guarded since args_as_json_str can raise (PydanticSerializationError)
        # on a non-serializable args object. A live preview must NEVER raise
        # out of here: the whole point is to render *something*.
        try:
            raw = part.args_as_json_str()
        except Exception:
            # Both arg accessors failed — the args are unrenderable. Render a
            # placeholder but log so this isn't a SILENT data loss in the
            # durable transcript (the full view routes here too).
            logger.exception(
                "steps: tool-call args unrenderable for tool=%s call_id=%s; "
                "rendering '…' (args omitted from this step)",
                part.tool_name,
                part.tool_call_id,
            )
            return "…"
        return _collapse_ws(raw) if collapse else raw
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


def _render_live_tool_call_part(part: ToolCallPart) -> str:
    """``\\`tool_name(args)\\``` — one inline-code line."""
    call = f"{part.tool_name}({_format_call_args(part)})"
    return _inline_code(_truncate_inline(call, _LIVE_TOOL_MAX_CHARS))


def _render_live_tool_return_part(part: ToolReturnPart) -> str:
    """Compact fenced result: ``⎿`` first line + up to a few aligned more.

    Keeps the first :data:`_LIVE_RETURN_MAX_LINES` real lines of the result so
    multi-line output stays readable in the live stream, but bounded. The two
    kinds of truncation are marked where they happen: a line longer than
    :data:`_LIVE_RETURN_LINE_MAX_CHARS` is cut with a trailing ``…`` ON THAT
    line, and when whole lines are dropped beyond the kept few a
    ``… (truncated)`` marker is appended to the last kept line. Wrapped in a
    fence (embedded ``` is neutralized) so a stray triple-backtick can't break
    the progress message; the full, untruncated result lives on the ⤵ transcript.
    """
    lines = part.model_response_str().split("\n")
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


def _render_live_delta(messages: Sequence[ModelMessage]) -> list[str]:
    """Compact per-part render for the live progress body.

    Same part-selection as :func:`_render_tree_blocks` (text + tool calls from
    ``ModelResponse``; tool returns from ``ModelRequest``; everything else
    skipped) but in the compact live format: model text as plain markdown
    prose, a tool call as one inline-code ``tool_name(args)`` line, a tool
    return as a short fenced ``⎿`` block. One string per rendered part, in
    order. The caller appends these to :attr:`StepsEntry.rendered_lines`.

    Wrapped by the caller in try/except — ``args_as_json_str`` can raise on
    malformed args, exactly as for :func:`_render_tree_blocks`.
    """
    out: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    rendered = _render_text_part(part)
                    if rendered is not None:
                        out.append(_truncate(rendered, _LIVE_TEXT_MAX_CHARS))
                elif isinstance(part, ToolCallPart):
                    out.append(_render_live_tool_call_part(part))
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    out.append(_render_live_tool_return_part(part))
    return out


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


def _pluralize_steps(count: int) -> str:
    """Render ``N step(s)`` with correct singular/plural for ``count``."""
    return f"{count} step" if count == 1 else f"{count} steps"


def _progress_content(entry: StepsEntry) -> str:
    """Render the transient progress message: the live trace, nothing else.

    A tail-windowed compact render of the entry's
    :attr:`~calfcord.bridge.steps_state.StepsEntry.rendered_lines`
    — the agent's actual work as it streams (model text, ``tool_name(args)``,
    ``⎿ result``), most-recent-fits-first. No header/counter line: the message
    IS the trace. Hard-clamped to :data:`_DISCORD_MESSAGE_LIMIT` so an in-place
    ``edit_message`` can never fail for length (the tail window keeps it well
    under; this slice is a final safety net).

    The message only ever posts/edits on a renderable hop, so
    ``rendered_lines`` is non-empty whenever this is called; the empty-body
    fallback (a lone ``…``) is purely defensive — Discord rejects an
    empty-content message.
    """
    body = _tail_window(entry.rendered_lines, _PROGRESS_BODY_MAX_CHARS)
    if not body:
        # Unreachable in practice — the message only posts/edits on a
        # renderable hop, so rendered_lines is non-empty here. The lone "…"
        # keeps a future regression from sending empty content (Discord
        # rejects it with a 400) while surfacing the anomaly in the log.
        logger.warning("steps: progress content rendered empty (unexpected); sending a placeholder")
        return "…"
    return body[:_DISCORD_MESSAGE_LIMIT]


async def _best_effort_progress[T](coro: Awaitable[T], *, action: str, key_label: str, key_value: int) -> T | None:
    """Await a best-effort progress-message Discord call, swallowing the
    usual failures so a transient/gone message can never crash the steps
    consumer. Returns the call's result, or None if it failed."""
    try:
        return await coro
    except discord.NotFound:
        logger.debug("steps: progress %s hit NotFound %s=%d (already gone)", action, key_label, key_value)
    except discord.Forbidden:
        logger.warning("steps: progress %s Forbidden %s=%d", action, key_label, key_value)
    except discord.DiscordException as e:
        logger.warning(
            "steps: progress %s failed %s=%d status=%s: %s",
            action,
            key_label,
            key_value,
            getattr(e, "status", None),
            e,
        )
    return None


def build_steps_consumer(
    persona_sender: DiscordPersonaSender,
    registry: AgentRegistry,
    pending_wires: PendingWires,
    steps_state: StepsState,
    *,
    typing_notifier: TypingNotifier | None = None,
    subscribe_topic: str = AGENT_STEPS_TOPIC,
    node_id: str = DEFAULT_STEPS_CONSUMER_NODE_ID,
) -> ConsumerNode[str]:
    """Construct the bridge's steps consumer node.

    Args:
        persona_sender: The bridge's REST-only Discord client. Posts,
            edits, and deletes the transient progress message under the
            agent's persona via the per-channel webhook
            (:meth:`~calfcord.discord.persona.DiscordPersonaSender.send`
            / ``edit_message`` / ``delete_message``).
        registry: Roster of agents. Resolves
            ``ConsumerContext.emitter_node_id`` to a :class:`Persona`. An
            unknown emitter id is logged and skipped.
        pending_wires: Bridge-local store of in-flight inbound wires.
            We read the parent ``channel_id`` / ``message_id`` and the
            pre-invocation ``message_history`` length from here.
        steps_state: Per-correlation cursor + progress-message-id cache
            plus the "already-completed" set that suppresses outbox-retry
            hops.
        typing_notifier: Optional best-effort typing-indicator firer. When
            provided, a Discord typing indicator is fired (fire-and-forget)
            on each non-terminal hop carrying new work, in the channel/thread
            the conversation lives in. ``None`` disables typing — the default
            keeps existing callers/tests untouched; the bridge wires a real
            one in production.
        subscribe_topic: Defaults to :data:`AGENT_STEPS_TOPIC`. Override
            for tests.
        node_id: Stable identifier; the Worker uses it as the Kafka
            consumer ``group_id`` **unless** the Worker is constructed
            with an explicit ``group_id`` override (which the bridge's
            does not).

    Returns:
        A :class:`ConsumerNode` ready to register on a
        :class:`~calfkit.Worker`.
    """

    async def _post_progress(entry: StepsEntry, persona: Persona) -> None:
        """Post the transient progress message for the first renderable hop.

        Renders the entry's current trace (the first hop's lines are already
        accumulated). Stores ``sent.id`` on the entry as
        ``progress_message_id``. No ``reply_to`` and no ``extra_buttons``;
        routes into the thread via ``thread_id`` when the triggering event
        originated in one (``None`` for a top-level channel ⇒ posts to
        ``parent_channel_id``). Best-effort — any Discord failure is swallowed
        so it can't break the final-reply path. On failure the id stays
        ``None`` so the next renderable hop retries the post.
        """
        sent = await _best_effort_progress(
            persona_sender.send(
                persona=persona,
                channel_id=entry.parent_channel_id,
                content=_progress_content(entry),
                thread_id=entry.thread_id,
            ),
            action="post",
            key_label="channel_id",
            key_value=entry.parent_channel_id,
        )
        if sent is not None:
            entry.progress_message_id = sent.id

    async def _edit_progress(entry: StepsEntry) -> None:
        """Edit the progress message to the entry's CURRENT trace.

        Re-renders from ``entry.rendered_lines`` at call time, so a debounced
        fire reflects every line appended while it slept. Best-effort; a
        deleted message (``NotFound``) is ignored at DEBUG.
        """
        message_id = entry.progress_message_id
        if message_id is None:
            return
        await _best_effort_progress(
            persona_sender.edit_message(
                entry.parent_channel_id,
                message_id,
                content=_progress_content(entry),
                thread_id=entry.thread_id,
            ),
            action="edit",
            key_label="message_id",
            key_value=message_id,
        )

    def _schedule_debounced_edit(entry: StepsEntry) -> None:
        """Ensure exactly one trailing-debounce edit task is pending.

        If the entry already has a live (not-done) debounce task, return —
        that task re-renders ``entry.rendered_lines`` when it fires, so the
        lines this hop just appended are picked up for free. Otherwise spawn
        one that sleeps :data:`_PROGRESS_DEBOUNCE_SECONDS` then edits.
        """
        existing = entry.debounce_task
        if existing is not None and not existing.done():
            return

        async def _run() -> None:
            await asyncio.sleep(_PROGRESS_DEBOUNCE_SECONDS)
            await _edit_progress(entry)

        entry.debounce_task = asyncio.create_task(_run())

    async def _cancel_debounce(entry: StepsEntry) -> None:
        """Cancel and await the entry's pending debounce task, if any.

        Suppresses ``CancelledError`` so a mid-sleep cancel is silent.
        Awaiting guarantees the task is fully torn down before the
        terminal hop deletes the progress message — no late edit can race
        the delete.
        """
        task = entry.debounce_task
        if task is None:
            return
        entry.debounce_task = None
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _delete_progress(entry: StepsEntry) -> None:
        """Delete the transient progress message on the terminal hop.

        Only acts when a progress message was actually posted. Best-effort;
        an already-deleted message (``NotFound``) is ignored at DEBUG.
        """
        message_id = entry.progress_message_id
        if message_id is None:
            return
        await _best_effort_progress(
            persona_sender.delete_message(entry.parent_channel_id, message_id, thread_id=entry.thread_id),
            action="delete",
            key_label="message_id",
            key_value=message_id,
        )

    async def _sink(entry: StepsEntry, persona: Persona) -> None:
        """Reflect the just-appended steps into the progress message.

        The caller has already appended this hop's rendered strings to
        ``entry.rendered_lines``; this only decides post-vs-edit. On the first
        renderable hop (``progress_message_id is None``) posts the message
        under ``persona``; on later hops schedules a debounced edit (which
        re-renders the body from the entry's lines at fire time).
        """
        if entry.progress_message_id is None:
            await _post_progress(entry, persona)
        else:
            _schedule_debounced_edit(entry)

    async def _consume(result: ConsumerContext[str]) -> None:
        correlation_id = result.correlation_id

        if result.emitter_node_kind != "agent" or not result.emitter_node_id:
            return
        if steps_state.is_completed(correlation_id):
            return

        is_terminal = bool(result.output_parts)

        entry = steps_state.get(correlation_id)
        if entry is None:
            pending = pending_wires.get(correlation_id)
            if pending is None:
                logger.debug(
                    "steps: no pending wire for correlation_id=%s; skipping hop",
                    correlation_id,
                )
                if is_terminal:
                    steps_state.pop_and_mark_completed(correlation_id)
                return
            wire = pending.wire
            entry = StepsEntry(
                parent_channel_id=wire.channel_id,
                parent_message_id=wire.message_id,
                thread_id=wire.thread_id,
                history_cursor=pending.initial_message_history_length,
            )
            steps_state.put(correlation_id, entry)

        history = result.message_history
        # Terminal hop drops the trailing ModelResponse — the outbox
        # posts its text to the parent channel; counting it here would
        # double-count the answer. Tool returns earlier in the same delta
        # still count.
        new_messages = (
            history[entry.history_cursor : -1] if is_terminal and history else history[entry.history_cursor :]
        )

        # No new content and not closing — gated-out peer mirror or
        # a publish hop the agent loop didn't grow history on.
        if not new_messages and not is_terminal:
            return

        # Fire a typing indicator for genuine, non-terminal work. Skipped on
        # the terminal hop (the outbox posts the answer there) and on empty
        # peer mirrors (filtered by the guard above). Fire-and-forget, so it
        # never blocks this serial consumer. Targets the thread the wire
        # originated in, else the parent channel — the surface the user is
        # reading. (Typing addresses that id directly, unlike the webhook
        # progress post, which addresses the parent and routes into the thread.)
        if typing_notifier is not None and new_messages and not is_terminal:
            typing_notifier.fire(entry.thread_id or entry.parent_channel_id)

        try:
            rendered = _render_live_delta(new_messages)
        except Exception:
            # ToolCallPart.args_as_json_str can raise on malformed payloads;
            # advancing the cursor still happens below so the next hop doesn't
            # re-trip the same bad message. The whole hop's delta is dropped
            # from the LIVE view (it stays recoverable via the ⤵ transcript,
            # which renders independently in the outbox); log how much was lost
            # so a gap in the live trace is diagnosable.
            logger.exception(
                "steps: _render_live_delta raised on correlation_id=%s; dropping this hop's "
                "delta of %d message(s) from the live view (recoverable via the ⤵ transcript) "
                "and advancing the cursor to avoid re-walking it",
                correlation_id,
                len(new_messages),
            )
            rendered = []

        if new_messages:
            entry.history_cursor = len(history)

        if rendered and not is_terminal:
            # Skip on the terminal hop: the progress message is deleted just
            # below, so rendering/posting the terminal delta into it is wasted
            # — and on a single-pass turn (the first renderable hop is also the
            # terminal one) posting then immediately deleting would flash a
            # message in the channel. The turn's final answer is posted by the
            # outbox, and the full steps live on the ⤵ transcript.
            spec = registry.by_id(result.emitter_node_id)
            if spec is None:
                logger.warning(
                    "steps: unknown emitter=%s correlation_id=%s; skipping progress update",
                    result.emitter_node_id,
                    correlation_id,
                )
            else:
                persona = Persona(
                    name=spec.display_name,
                    avatar_url=spec.avatar_url,
                )
                # Accumulate the compact lines BEFORE the sink so the post/edit
                # renders the up-to-date trace.
                entry.rendered_lines.extend(rendered)
                await _sink(entry, persona)

        if is_terminal:
            # entry is non-None here (fetched or just seeded above): the
            # no-entry terminal paths returned early). Cancel any pending
            # debounce edit and delete the progress message BEFORE popping,
            # then record completion to suppress outbox-retry hops.
            await _cancel_debounce(entry)
            await _delete_progress(entry)
            steps_state.pop_and_mark_completed(correlation_id)

    # No gate — we want every hop, including gated-out peer mirrors so
    # the cursor stays consistent across all co-tenants.
    return ConsumerNode[str](
        node_id=node_id,
        subscribe_topics=subscribe_topic,
        consume_fn=_consume,
        agent_output_type=str,
    )
