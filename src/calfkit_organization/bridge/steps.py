"""Discord steps consumer — posts every assistant agent's intermediate
hops into a per-invocation transcript thread.

A long-lived calfkit :class:`ConsumerNodeDef` subscribed to
:data:`~calfkit_organization.topics.AGENT_STEPS_TOPIC` (``agent.steps``)
in its own Kafka consumer group. Every assistant agent's handler hop —
``Call`` envelopes (tool dispatch), ``TailCall`` retries, the terminal
``ReturnCall`` — is mirrored to that topic by FastStream's
``@publisher`` decorator (see
:meth:`calfkit.worker.Worker.register_handlers` and the agent factory's
``publish_topic=AGENT_STEPS_TOPIC`` injection). The consumer walks each
hop's ``state.message_history`` delta and posts the new
:class:`TextPart` / :class:`ToolCallPart` / :class:`ToolReturnPart`
entries into a thread off the user's original Discord message.

Why this exists: the bridge's outbox consumer
(:func:`~calfkit_organization.bridge.outbox.build_outbox_consumer`)
gates on ``state.final_output_parts``, so it only ever posts the
agent's terminal reply. When the model emits text alongside tool calls,
that text rides on the same ``ModelResponse`` as the ``ToolCallPart``
but is never projected to ``final_output_parts`` (see
``calfkit/nodes/agent.py`` — the ``DeferredToolRequests`` branch
extends ``message_history`` but does not set ``final_output_parts``).
Without this consumer, the model's running commentary and the tool
calls themselves are invisible to the user.

How the wire is recovered: same pattern as the outbox.
:class:`NodeResult` carries ``state``, ``correlation_id``, and
``emitter_node_id`` but not the original inbound wire. The bridge's
:class:`~calfkit_organization.bridge.pending_wires.PendingWires` map
(populated by :class:`BridgeIngress` on the way in) gives us the
parent Discord ``channel_id`` / ``message_id`` to thread off, and
the pre-invocation ``message_history`` length to seed the
:attr:`StepsEntry.history_cursor` so the channel-history prefix
projected by :func:`~calfkit_organization.bridge.history.project_history`
does not get re-rendered as fresh steps (a bug class the
``initial_message_history_length`` field exists to close).

**Thread lifecycle.**

* **Create** — lazily, on the first hop that produces a renderable
  step. A pure-text first-turn reply (no tools, no preamble) skips
  thread creation entirely; the outbox path posts the final reply
  in the parent channel and that's the end of it.
* **Append** — every subsequent hop's delta is rendered and posted
  into the thread under the agent's normal persona.
* **Lock** — on the terminal hop (``state.final_output_parts`` is
  set), the thread is locked (``Thread.edit(locked=True,
  archived=False)``) so users cannot accidentally post in it after
  the transcript closes. ``archived=False`` is load-bearing: Discord
  auto-archives threads (60 minutes by default; see
  :data:`THREAD_AUTO_ARCHIVE_MINUTES`), and locking an already-archived
  thread leaves it read-only-by-auto-archive instead of locked. We
  always pass both so a long-running agent whose thread auto-archived
  mid-run still ends up explicitly locked.

**Terminal hop also renders the prior delta.** When the agent emits a
``ToolCall`` then a ``ToolReturn`` then a final ``TextPart`` in three
hops, the tool result lives in the terminal envelope's
``message_history`` delta (the new ``ModelRequest(ToolReturnPart)``
is appended in the same ``run()`` call that produces the final
``ModelResponse``). The consumer renders the delta *up to but not
including* the final ``ModelResponse`` before locking; the final
``ModelResponse`` is the answer text, which the outbox posts to the
parent channel.

**Assumption: users do not post in step threads.**
:class:`~calfkit_organization.bridge.history.ChannelHistoryFetcher`
reads from ``source_channel_id`` (the actual landing channel of an
inbound wire, including threads). If a user posts inside a step
thread, the fetcher pulls thread history and
:func:`~calfkit_organization.bridge.history.project_history` turns
every step post into a ``ModelResponse(TextPart(...))`` from the
agent (the persona's ``display_name`` matches the registered agent).
The LLM would see narrative descriptions of past tool calls without
the structured ``ToolCallPart`` / ``ToolReturnPart``, and the
response would degrade. We accept this as documented undefined
behavior; locking the thread on the terminal hop is the
defense-in-depth.

**Source-was-already-a-thread.** When the inbound wire originated
inside a Discord thread, the bridge's normalizer flattens
``wire.channel_id`` to the parent channel for Kafka topic routing
while ``wire.source_channel_id`` keeps the thread id. The consumer
detects this mismatch and skips thread creation entirely for the
correlation — Discord forbids creating a thread off a thread message,
and even if it didn't, the parent-channel ``fetch_message`` would 404
because the message lives in the thread, not the parent. Step
transcripts are disabled for thread-originated invocations in v1.

**Outbox retries.** The bridge's outbox path re-invokes the agent on
``agent.{aid}.in`` with the **same** ``correlation_id`` after a
Discord-post failure (see
:func:`~calfkit_organization.bridge.outbox._publish_retry`). Without a
completion guard, the retry's first hop would seed a fresh
:class:`StepsEntry` and create a second transcript thread off the same
parent message — the original is now locked-and-orphaned. The
consumer guards against this by checking
:meth:`StepsState.is_completed` before seeding; the terminal hop
marks the correlation completed even when no thread was ever created
(so retries of pure-text replies are also suppressed).

**Co-tenant peer envelopes — deferred seeding.** Every agent that
subscribes to the inbound channel topic flows through calfkit's
``handler()`` (``calfkit/nodes/base.py:268-278``). When a peer's
gates filter the envelope, the handler still returns
``Response(body=envelope_unchanged, headers=self._emitter_headers())``,
and FastStream's ``@publisher`` decorator mirrors that to
``agent.steps`` with the **peer's** emitter headers. If the steps
consumer eagerly created a :class:`StepsEntry` on first-arrival, the
peer's no-delta envelope would claim the entry under the peer's
persona before the real emitter's content-bearing hop arrived — and
the transcript would post under the wrong agent. The consumer
therefore defers entry creation until it has rendered content
(``_render_delta`` produced a non-empty list) or the hop is terminal.
Gated-out peer envelopes carry the inbound envelope unchanged, so
their delta is empty and they cannot claim the entry.

**Failure semantics.** Every Discord operation is wrapped in a
try/except that catches the common Discord error subclasses
(``NotFound``, ``Forbidden``, ``DiscordException`` — broader than
``HTTPException`` so the sibling ``RateLimited`` is also funneled
through). Unusual cases (``InvalidData`` etc.) fall through to the
:class:`ConsumerNodeDef` shell's swallow-and-log net.
:func:`_render_delta` is also wrapped because
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
load-bearing thread lock is the one casualty: any thread that was
created but whose terminal hop arrived during the down window stays
open. Operators can sweep these manually if needed.

**Partition-key requirement.**
:data:`AGENT_STEPS_TOPIC` MUST be configured with a single partition
(or every agent's hops must hash to the same partition by some other
means) until calfkit's publisher decorator carries the correlation-id
as a Kafka key. FastStream's ``@publisher`` decorator wraps the
calfkit handler's plain ``Response`` return without a key, so on a
multi-partition topic the hops for one ``correlation_id`` can
round-robin partitions and arrive out of order — cursor jumps swallow
deltas, and an intermediate hop arriving after a terminal hop would
create a second unlocked thread. The bridge's direct
:meth:`calfkit.Client.publish` calls do stamp the key (see
``calfkit/nodes/base.py``); the gap is only the publisher-decorator
mirror path that ``publish_topic=AGENT_STEPS_TOPIC`` activates.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Sequence
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

THREAD_AUTO_ARCHIVE_MINUTES: Final[int] = 60
"""Discord auto-archive duration for the transcript thread. 60 minutes
is well above expected agent turn duration; the alternates (``1440``,
``4320``, ``10080``) are valid for all guilds since Discord dropped
the boost-tier gating in 2022. Bump if agents routinely exceed an
hour and you want threads to stay un-archived until lock."""

THREAD_NAME_MAX_LEN: Final[int] = 100
"""Discord's hard limit on thread names."""

STEP_CONTENT_MAX_CHARS: Final[int] = 1500
"""Maximum inner content length we render for a single ``ToolCallPart``
args block or ``ToolReturnPart`` content block. Above this, the body
is truncated with an explicit indicator. Picked well below Discord's
2000-char message cap so the surrounding code fence + header always
fits in one message even after the tool name pushes the header longer."""

TRUNCATION_MARKER: Final[str] = "\n… (truncated)"

THREAD_HEADER: Final[str] = "_Step transcript — read-only._"
"""Soft visual cue posted as the first message in the thread. Lock-on-
completion is the load-bearing guard; this is just operator hint."""


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
    Skips:

    * ``ThinkingPart``, ``FilePart``, ``BuiltinTool*Part`` —
      out of scope for v1.
    * ``UserPromptPart`` / ``SystemPromptPart`` — invocation context,
      not the agent's "work"; the user prompt is already visible above
      the thread.
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


def _thread_name(display_name: str) -> str:
    """Build a thread name within Discord's 100-char cap.

    Falls back to a generic ``"agent steps"`` when the display name is
    empty or whitespace-only (Discord rejects empty thread names with
    a 400).
    """
    base = (display_name or "").strip() or "agent"
    raw = f"{base} steps"
    if len(raw) <= THREAD_NAME_MAX_LEN:
        return raw
    return raw[: THREAD_NAME_MAX_LEN - 1] + "…"


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
        persona_sender: The bridge's REST-only Discord client. Used to
            post step messages under the agent's persona via the
            per-channel webhook. The underlying ``persona_sender.client``
            is also used for thread create/lock REST calls — webhooks
            cannot create or modify threads themselves.
        registry: Roster of agents. Resolves
            ``NodeResult.emitter_node_id`` to a :class:`Persona`. An
            unknown emitter id is logged and skipped.
        pending_wires: Bridge-local store of in-flight inbound wires.
            We read the parent ``channel_id`` / ``message_id`` and the
            pre-invocation ``message_history`` length from here.
        steps_state: Per-correlation cursor + thread-id cache plus the
            "already-completed" set that suppresses outbox-retry hops.
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
    # Bounded log-dedup for thread create/lock Forbidden errors, sized
    # identically to history.py's _FORBIDDEN_LOG_DEDUP_MAX. Intentionally
    # duplicated rather than imported to keep steps.py independent of
    # history.py internals; keep numerically in sync if you retune.
    _forbidden_log_dedup: OrderedDict[int, None] = OrderedDict()
    _forbidden_log_dedup_max = 4096

    def _log_forbidden_once(channel_id: int, action: str) -> None:
        key = channel_id
        if key in _forbidden_log_dedup:
            _forbidden_log_dedup.move_to_end(key)
            return
        _forbidden_log_dedup[key] = None
        while len(_forbidden_log_dedup) > _forbidden_log_dedup_max:
            _forbidden_log_dedup.popitem(last=False)
        logger.warning(
            "channel_id=%d: Forbidden on %s; "
            "step transcript will be incomplete for this and future invocations "
            "until permission is granted. Grant the bot 'Create Public Threads' "
            "and 'Manage Threads' to enable.",
            channel_id,
            action,
        )

    async def _create_thread(
        entry: StepsEntry, display_name: str,
    ) -> int | None:
        """Create the transcript thread off the user's parent message.

        Returns the new thread id, or ``None`` on any Discord error.
        Catches ``DiscordException`` (broader than ``HTTPException``)
        so ``RateLimited`` is also funneled through and doesn't escape
        to the consumer shell.
        """
        client = persona_sender.client
        try:
            channel = await client.fetch_channel(entry.parent_channel_id)
        except discord.NotFound:
            logger.warning(
                "steps: parent channel_id=%d not found; cannot create thread",
                entry.parent_channel_id,
            )
            return None
        except discord.Forbidden:
            _log_forbidden_once(entry.parent_channel_id, "fetch_channel")
            return None
        except discord.DiscordException as e:
            logger.warning(
                "steps: fetch_channel failed channel_id=%d status=%s: %s",
                entry.parent_channel_id,
                getattr(e, "status", None),
                e,
            )
            return None

        if not isinstance(channel, discord.TextChannel):
            # Forum / Voice / Category / Thread parents can't host a
            # transcript thread. We dedup at WARNING so a misrouted
            # channel surfaces once for the operator.
            _log_forbidden_once(
                entry.parent_channel_id,
                f"parent channel is {type(channel).__name__}, not TextChannel",
            )
            return None

        try:
            message = await channel.fetch_message(entry.parent_message_id)
        except discord.NotFound:
            logger.warning(
                "steps: parent message_id=%d not found in channel=%d",
                entry.parent_message_id, entry.parent_channel_id,
            )
            return None
        except discord.Forbidden:
            _log_forbidden_once(entry.parent_channel_id, "fetch_message")
            return None
        except discord.DiscordException as e:
            logger.warning(
                "steps: fetch_message failed message_id=%d status=%s: %s",
                entry.parent_message_id,
                getattr(e, "status", None),
                e,
            )
            return None

        try:
            thread = await message.create_thread(
                name=_thread_name(display_name),
                auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
            )
        except discord.Forbidden:
            _log_forbidden_once(entry.parent_channel_id, "create_thread")
            return None
        except discord.DiscordException as e:
            logger.warning(
                "steps: create_thread failed message_id=%d status=%s: %s",
                entry.parent_message_id,
                getattr(e, "status", None),
                e,
            )
            return None

        logger.info(
            "steps: created thread_id=%d off message_id=%d channel_id=%d",
            thread.id, entry.parent_message_id, entry.parent_channel_id,
        )
        return thread.id

    async def _lock_thread(thread_id: int, channel_id: int) -> None:
        """Lock the transcript thread so users cannot post in it.

        Best-effort; failures are logged and swallowed. Catches
        ``DiscordException`` so ``RateLimited`` doesn't escape.
        """
        client = persona_sender.client
        try:
            thread = await client.fetch_channel(thread_id)
        except discord.NotFound:
            # Thread was deleted between create and lock. Operationally
            # uninteresting; DEBUG.
            logger.debug(
                "steps: lock fetch_channel thread_id=%d not found", thread_id,
            )
            return
        except discord.Forbidden:
            # Operator-actionable permission regression — split from
            # NotFound so it surfaces at WARNING via the dedup helper.
            _log_forbidden_once(channel_id, "fetch_channel (lock)")
            return
        except discord.DiscordException as e:
            logger.warning(
                "steps: lock fetch_channel thread_id=%d status=%s: %s",
                thread_id, getattr(e, "status", None), e,
            )
            return

        if not isinstance(thread, discord.Thread):
            logger.debug(
                "steps: thread_id=%d is %s, not a Thread; skipping lock",
                thread_id, type(thread).__name__,
            )
            return

        try:
            # archived=False is load-bearing: a thread that auto-archived
            # mid-run must be explicitly un-archived in the same call,
            # otherwise locking leaves it read-only-by-auto-archive
            # instead of locked. See the module docstring.
            await thread.edit(locked=True, archived=False)
            logger.info("steps: locked thread_id=%d", thread_id)
        except discord.Forbidden:
            _log_forbidden_once(channel_id, "thread.edit(locked=True)")
        except discord.DiscordException as e:
            logger.warning(
                "steps: lock thread_id=%d status=%s: %s",
                thread_id, getattr(e, "status", None), e,
            )

    async def _post_in_thread(entry: StepsEntry, content: str) -> None:
        """Post one rendered step under the agent's persona in the thread.

        Catches ``DiscordException`` (covers ``RateLimited`` too) and
        the documented sender errors (``RuntimeError`` if the sender
        was never started, ``TypeError`` if a wire pointed at a
        non-text channel). All failures are swallowed — they must not
        affect the final-reply path.
        """
        if entry.thread_id is None:
            # Defensive — caller guarantees the thread is created first.
            logger.error(
                "steps: _post_in_thread called with thread_id=None for channel=%d",
                entry.parent_channel_id,
            )
            return
        try:
            await persona_sender.send(
                persona=entry.persona,
                channel_id=entry.parent_channel_id,
                content=content,
                thread_id=entry.thread_id,
            )
        except (discord.DiscordException, RuntimeError, TypeError) as e:
            logger.warning(
                "steps: persona send failed thread_id=%d: %s: %s",
                entry.thread_id, type(e).__name__, e,
            )

    async def _consume(result: NodeResult[str]) -> None:
        correlation_id = result.correlation_id

        if result.emitter_node_kind != "agent" or not result.emitter_node_id:
            return

        # Outbox-retry dedup. ``is_completed`` is True for any correlation
        # whose terminal hop has already been processed by this consumer;
        # the outbox's _publish_retry path reuses the same correlation_id,
        # so without this guard the retry's first hop would seed a fresh
        # transcript thread (the original is now locked).
        if steps_state.is_completed(correlation_id):
            return

        is_terminal = bool(result.output_parts)

        # Resolve the cursor without seeding yet. Co-tenant agents on the
        # same ambient channel topic all publish to ``agent.steps``: peers
        # whose gates filter the inbound envelope still flow through
        # calfkit's handler (base.py:268-278), which returns the unchanged
        # envelope with the *peer's* emitter headers. FastStream then
        # mirrors that to ``agent.steps``. If we seeded on first-arrival,
        # the peer would claim the entry under its persona before the real
        # emitter's content-bearing hop arrived — and the transcript
        # would post under the wrong agent. Defer seeding until we
        # actually have rendered content, so a no-delta peer envelope
        # cannot poison the persona for the real emitter.
        entry = steps_state.get(correlation_id)
        cursor: int
        pending_for_seed = None
        if entry is None:
            pending_for_seed = pending_wires.get(correlation_id)
            if pending_for_seed is None:
                logger.debug(
                    "steps: no pending wire for correlation_id=%s; skipping hop",
                    correlation_id,
                )
                if is_terminal:
                    steps_state.pop_and_mark_completed(correlation_id)
                return
            wire = pending_for_seed.wire
            if (
                wire.source_channel_id is not None
                and wire.source_channel_id != wire.channel_id
            ):
                logger.debug(
                    "steps: wire originated in a thread "
                    "(channel=%d source=%d); step transcripts disabled "
                    "for this correlation",
                    wire.channel_id, wire.source_channel_id,
                )
                if is_terminal:
                    steps_state.pop_and_mark_completed(correlation_id)
                return
            cursor = pending_for_seed.initial_message_history_length
        else:
            cursor = entry.history_cursor

        history = result.message_history
        # On terminal hop, drop the trailing ModelResponse from the
        # delta — that's the final answer the outbox is about to post
        # to the parent channel; rendering it here would duplicate it
        # in the thread. Tool returns and any earlier intermediate
        # text in this same delta still render.
        new_messages = (
            history[cursor:-1]
            if is_terminal and history
            else history[cursor:]
        )

        try:
            rendered = _render_delta(new_messages)
        except Exception:
            # Most likely ``ToolCallPart.args_as_json_str`` on a
            # malformed payload. Log and treat as empty so the cursor
            # still advances past the bad message; otherwise we'd loop
            # on every subsequent hop.
            logger.exception(
                "steps: _render_delta raised on correlation_id=%s; "
                "skipping this hop's delta", correlation_id,
            )
            rendered = []

        # Seed only when the delta itself is non-empty (so there is
        # progress to track) OR this is the terminal hop (which must
        # mark completion). Gated-out peer envelopes pass the inbound
        # envelope unchanged, so their ``new_messages`` is empty and
        # they cannot claim the entry. Note we key on ``new_messages``
        # rather than ``rendered`` so that whitespace-only text and
        # rendering exceptions (where ``new_messages`` is non-empty
        # but ``rendered`` is empty) still seed the entry — otherwise
        # the next hop would re-walk and re-trip the same bad message.
        if entry is None and (new_messages or is_terminal):
            assert pending_for_seed is not None  # set above when entry is None
            spec = registry.by_id(result.emitter_node_id or "")
            if spec is None:
                logger.warning(
                    "steps: unknown emitter=%s correlation_id=%s",
                    result.emitter_node_id, correlation_id,
                )
                if is_terminal:
                    steps_state.pop_and_mark_completed(correlation_id)
                return
            entry = StepsEntry(
                parent_channel_id=pending_for_seed.wire.channel_id,
                parent_message_id=pending_for_seed.wire.message_id,
                persona=Persona(
                    name=spec.display_name, avatar_url=spec.avatar_url,
                ),
                history_cursor=cursor,
            )
            steps_state.put(correlation_id, entry)

        # Advance the cursor on the live entry (if one exists). For
        # peer-only no-delta envelopes the entry is still None here;
        # nothing to advance, and the next hop will recompute the cursor
        # from the pending wire's initial_message_history_length anyway.
        if entry is not None:
            entry.history_cursor = len(history)

        if entry is not None and rendered:
            await _ensure_thread_and_post(
                entry, result.emitter_node_id, rendered,
            )

        if is_terminal:
            # Pop + mark completed even when no thread was ever created,
            # so an outbox retry of this correlation doesn't seed one now.
            popped = steps_state.pop_and_mark_completed(correlation_id)
            if popped is not None and popped.thread_id is not None:
                await _lock_thread(
                    popped.thread_id, popped.parent_channel_id,
                )

    async def _ensure_thread_and_post(
        entry: StepsEntry,
        emitter_node_id: str,
        rendered: list[str],
    ) -> None:
        """Create the thread on first renderable content, then post all steps.

        On thread-create failure: the entry is left in place (no pop) so
        the cursor stays advanced; future hops can retry creation
        without re-walking already-processed deltas. ``_log_forbidden_once``
        prevents the retries from spamming logs.
        """
        if entry.thread_id is None:
            spec = registry.by_id(emitter_node_id)
            display_name = (
                spec.display_name if spec is not None else emitter_node_id
            )
            thread_id = await _create_thread(entry, display_name)
            if thread_id is None:
                return
            entry.thread_id = thread_id
            await _post_in_thread(entry, THREAD_HEADER)

        for step_content in rendered:
            await _post_in_thread(entry, step_content)

    # No gate — we want every hop. ConsumerNodeDef defaults to no gates,
    # which gives us the full transition stream on this topic.
    return ConsumerNodeDef[str](
        node_id=node_id,
        subscribe_topics=subscribe_topic,
        consume_fn=_consume,
        output_type=str,
    )
