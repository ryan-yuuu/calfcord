"""Discord steps consumer — streams every assistant agent's intermediate
hops into a single transient in-channel "progress" message.

A long-lived calfkit :class:`ConsumerNodeDef` subscribed to
:data:`~calfkit_organization.topics.AGENT_STEPS_TOPIC` (``agent.steps``)
in its own Kafka consumer group. Every assistant agent's handler hop —
``Call`` envelopes (tool dispatch), ``TailCall`` retries, the terminal
``ReturnCall`` — is mirrored to that topic by FastStream's
``@publisher`` decorator (see
:meth:`calfkit.worker.Worker.register_handlers` and the agent factory's
``publish_topic=AGENT_STEPS_TOPIC`` injection). The consumer walks each
hop's ``state.message_history`` delta, counts the new
:class:`TextPart` / :class:`ToolCallPart` / :class:`ToolReturnPart`
entries, and reflects the running total in one compact progress message
posted under the agent's persona in the user's original channel.

Why this exists: the bridge's outbox consumer
(:func:`~calfkit_organization.bridge.outbox.build_outbox_consumer`)
gates on ``state.final_output_parts``, so it only ever posts the
agent's terminal reply. When the model emits text alongside tool calls,
that text rides on the same ``ModelResponse`` as the ``ToolCallPart``
but is never projected to ``final_output_parts`` (see
``calfkit/nodes/agent.py`` — the ``DeferredToolRequests`` branch
extends ``message_history`` but does not set ``final_output_parts``).
Without this consumer, the model's running commentary and the tool
calls themselves are invisible to the user while the agent works.

How the wire is recovered: same pattern as the outbox.
:class:`NodeResult` carries ``state``, ``correlation_id``, and
``emitter_node_id`` but not the original inbound wire. The bridge's
:class:`~calfkit_organization.bridge.pending_wires.PendingWires` map
(populated by :class:`BridgeIngress` on the way in) gives us the
parent Discord ``channel_id`` / ``message_id`` to post against, and
the pre-invocation ``message_history`` length to seed the
:attr:`StepsEntry.history_cursor` so the channel-history prefix
projected by :func:`~calfkit_organization.bridge.history.project_history`
does not get re-counted as fresh steps (a bug class the
``initial_message_history_length`` field exists to close).

**Progress-message lifecycle.**

* **Post** — lazily, on the first hop that produces a renderable step.
  A pure-text first-turn reply (no tools, no preamble) never posts a
  progress message; the outbox path posts the final reply in the parent
  channel and that's the end of it.
* **Edit (debounced)** — every subsequent hop that yields renderable
  content bumps the entry's :attr:`StepsEntry.step_count` and schedules a
  trailing, debounced edit (:data:`_PROGRESS_DEBOUNCE_SECONDS`). At most
  one edit task is pending per entry; bumps that land while it sleeps are
  picked up because the task reads the count at fire time. The edit keeps
  the message's components/embeds (none here) and only rewrites the text.
* **Delete** — on the terminal hop (``state.final_output_parts`` is set),
  any pending debounce edit is cancelled and the progress message is
  deleted. The outbox's final reply (which, in a later phase, carries the
  expand toggle) supersedes it, so the transient counter leaves no
  permanent residue in the channel.

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
while ``wire.source_channel_id`` keeps the thread id. The consumer
detects this mismatch and skips the progress surface entirely for the
correlation — the parent-channel webhook would post the counter in the
wrong place relative to where the user is actually talking. Step
progress is disabled for thread-originated invocations in v1.

**Outbox retries.** The bridge's outbox path re-invokes the agent on
``agent.{aid}.in`` with the **same** ``correlation_id`` after a
Discord-post failure (see
:func:`~calfkit_organization.bridge.outbox._publish_retry`). Without a
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
final-reply path. :func:`_render_delta` is also wrapped because
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
import logging
from collections.abc import Awaitable, Sequence
from typing import Final

import discord
from calfkit import ConsumerNodeDef, NodeResult
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.steps_state import StepsEntry, StepsState
from calfkit_organization.discord.persona import (
    DiscordPersonaSender,
    Persona,
)
from calfkit_organization.topics import AGENT_STEPS_TOPIC

logger = logging.getLogger(__name__)

DEFAULT_STEPS_CONSUMER_NODE_ID: Final[str] = "discord-steps-sink"

STEP_CONTENT_MAX_CHARS: Final[int] = 1500
"""Maximum inner content length we render for a single ``ToolCallPart``
args block or ``ToolReturnPart`` content block when counting steps.
Above this, the body is truncated with an explicit indicator. Picked
well below Discord's 2000-char message cap. Retained from the thread
era because :func:`_render_delta` is shared with the (future) expand
view; the live progress message only consumes its ``len``."""

TRUNCATION_MARKER: Final[str] = "\n… (truncated)"

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


def _render_tool_call_part(part: ToolCallPart) -> str:
    """Render a ``ToolCallPart`` into a Discord-formatted code block."""
    args = _truncate(part.args_as_json_str(), STEP_CONTENT_MAX_CHARS)
    return f"**Calling `{part.tool_name}`**\n```json\n{args}\n```"


def _render_tool_return_part(part: ToolReturnPart) -> str:
    """Render a ``ToolReturnPart`` into a Discord-formatted code block."""
    body = _truncate(part.model_response_str(), STEP_CONTENT_MAX_CHARS)
    return f"**`{part.tool_name}` returned**\n```\n{body}\n```"


def _render_delta(messages: Sequence[ModelMessage]) -> list[str]:
    """Project the new ``message_history`` slice into post-ready strings.

    Walks the delta in order and emits one string per renderable part.
    The live progress sink only consumes ``len(...)`` of the result (the
    step count); the strings themselves feed the future expand view.
    Skips:

    * ``ThinkingPart``, ``FilePart``, ``BuiltinTool*Part`` —
      out of scope for v1.
    * ``UserPromptPart`` / ``SystemPromptPart`` — invocation context,
      not the agent's "work"; the user prompt is already visible above
      the progress message.
    * ``RetryPromptPart`` — v1 simplification. These are pydantic-ai's
      framework-level retry feedback (e.g. tool-arg validation failure)
      and would actually help debug agent loops, but rendering them
      requires distinguishing pydantic_ai's auto-retries from genuine
      model-side errors — deferred to a follow-up.

    Caller wraps this in a try/except — ``args_as_json_str`` can raise
    on malformed args.
    """
    out: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    rendered = _render_text_part(part)
                    if rendered is not None:
                        out.append(rendered)
                elif isinstance(part, ToolCallPart):
                    out.append(_render_tool_call_part(part))
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    out.append(_render_tool_return_part(part))
    return out


def _pluralize_steps(count: int) -> str:
    """Render ``N step(s)`` with correct singular/plural for ``count``."""
    return f"{count} step" if count == 1 else f"{count} steps"


def _progress_content(step_count: int) -> str:
    """Render the transient progress message body for ``step_count`` steps.

    Pluralizes ``step`` so a single step reads naturally.
    """
    return f"⚙ running… {_pluralize_steps(step_count)}"


async def _best_effort_progress[T](
    coro: Awaitable[T], *, action: str, key_label: str, key_value: int
) -> T | None:
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
    subscribe_topic: str = AGENT_STEPS_TOPIC,
    node_id: str = DEFAULT_STEPS_CONSUMER_NODE_ID,
) -> ConsumerNodeDef[str]:
    """Construct the bridge's steps consumer node.

    Args:
        persona_sender: The bridge's REST-only Discord client. Posts,
            edits, and deletes the transient progress message under the
            agent's persona via the per-channel webhook
            (:meth:`~calfkit_organization.discord.persona.DiscordPersonaSender.send`
            / ``edit_message`` / ``delete_message``).
        registry: Roster of agents. Resolves
            ``NodeResult.emitter_node_id`` to a :class:`Persona`. An
            unknown emitter id is logged and skipped.
        pending_wires: Bridge-local store of in-flight inbound wires.
            We read the parent ``channel_id`` / ``message_id`` and the
            pre-invocation ``message_history`` length from here.
        steps_state: Per-correlation cursor + progress-message-id cache
            plus the "already-completed" set that suppresses outbox-retry
            hops.
        subscribe_topic: Defaults to :data:`AGENT_STEPS_TOPIC`. Override
            for tests.
        node_id: Stable identifier; the Worker uses it as the Kafka
            consumer ``group_id`` **unless** the Worker is constructed
            with an explicit ``group_id`` override (which the bridge's
            does not).

    Returns:
        A :class:`ConsumerNodeDef` ready to register on a
        :class:`~calfkit.Worker`.
    """

    async def _post_progress(entry: StepsEntry, persona: Persona, step_count: int) -> None:
        """Post the transient progress message for the first renderable hop.

        Stores ``sent.id`` on the entry as ``progress_message_id``. Plain
        send: no ``reply_to``, no ``thread_id``, no ``extra_buttons``.
        Best-effort — any Discord failure is swallowed so it can't break
        the final-reply path. On failure the id stays ``None`` so the next
        renderable hop retries the post.
        """
        sent = await _best_effort_progress(
            persona_sender.send(
                persona=persona,
                channel_id=entry.parent_channel_id,
                content=_progress_content(step_count),
            ),
            action="post",
            key_label="channel_id",
            key_value=entry.parent_channel_id,
        )
        if sent is not None:
            entry.progress_message_id = sent.id

    async def _edit_progress(entry: StepsEntry) -> None:
        """Edit the progress message to the entry's CURRENT ``step_count``.

        Reads ``step_count`` / ``progress_message_id`` at call time so a
        debounced fire reflects every bump that landed while it slept.
        Best-effort; a deleted message (``NotFound``) is ignored at DEBUG.
        """
        message_id = entry.progress_message_id
        if message_id is None:
            return
        await _best_effort_progress(
            persona_sender.edit_message(
                entry.parent_channel_id,
                message_id,
                content=_progress_content(entry.step_count),
            ),
            action="edit",
            key_label="message_id",
            key_value=message_id,
        )

    def _schedule_debounced_edit(entry: StepsEntry) -> None:
        """Ensure exactly one trailing-debounce edit task is pending.

        If the entry already has a live (not-done) debounce task, return —
        that task will read the latest ``step_count`` when it fires, so the
        bump this hop just made is picked up for free. Otherwise spawn one
        that sleeps :data:`_PROGRESS_DEBOUNCE_SECONDS` then edits.
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
            persona_sender.delete_message(entry.parent_channel_id, message_id),
            action="delete",
            key_label="message_id",
            key_value=message_id,
        )

    async def _sink(entry: StepsEntry, persona: Persona, rendered_count: int) -> None:
        """Reflect ``rendered_count`` new steps into the progress message.

        On the first renderable hop (``progress_message_id is None``) posts
        the message under ``persona``; on later hops bumps ``step_count``
        and schedules a debounced edit. ``rendered_count`` is the number of
        parts the just-processed delta produced (always >= 1 here).
        """
        entry.step_count += rendered_count
        if entry.progress_message_id is None:
            await _post_progress(entry, persona, entry.step_count)
        else:
            _schedule_debounced_edit(entry)

    async def _consume(result: NodeResult[str]) -> None:
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
            if wire.source_channel_id is not None and wire.source_channel_id != wire.channel_id:
                logger.debug(
                    "steps: wire originated in a thread "
                    "(channel=%d source=%d); step progress disabled "
                    "for this correlation",
                    wire.channel_id,
                    wire.source_channel_id,
                )
                if is_terminal:
                    steps_state.pop_and_mark_completed(correlation_id)
                return
            entry = StepsEntry(
                parent_channel_id=wire.channel_id,
                parent_message_id=wire.message_id,
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

        try:
            rendered = _render_delta(new_messages)
        except Exception:
            # ToolCallPart.args_as_json_str can raise on malformed
            # payloads; advancing the cursor still happens below so
            # the next hop doesn't re-trip the same bad message.
            logger.exception(
                "steps: _render_delta raised on correlation_id=%s; skipping this hop's delta",
                correlation_id,
            )
            rendered = []

        if new_messages:
            entry.history_cursor = len(history)

        if rendered:
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
                await _sink(entry, persona, len(rendered))

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
    return ConsumerNodeDef[str](
        node_id=node_id,
        subscribe_topics=subscribe_topic,
        consume_fn=_consume,
        output_type=str,
    )
