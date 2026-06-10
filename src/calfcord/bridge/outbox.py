"""Discord outbox consumer — posts every agent reply landing on the outbox.

A long-lived calfkit :class:`ConsumerNode` subscribed to
``discord.outbox`` in its own Kafka consumer group. Every agent
:class:`ReturnCall` landing on the outbox topic produces one invocation
of :func:`build_outbox_consumer`'s closure, so multi-agent flows
(ambient channel messages, team slashes) get all their replies posted
to Discord rather than just the first to win calfkit's reply-dispatcher
race (the dispatcher dedupes by ``correlation_id`` and would silently
drop every reply after the first — see
``calfkit.client.reply_dispatcher._ReplyDispatcher``).

How the wire is recovered: the consumer receives a
:class:`~calfkit.ConsumerContext`, which carries ``output``, ``state``,
``correlation_id``, ``emitter_node_id``, ``emitter_node_kind``, and the
inbound producer ``deps`` (so the original :class:`WireMessage` IS
reachable as ``deps["discord"]``). What it does NOT carry is the
bridge-computed per-invocation context — the ``message_history``
snapshot, its this-turn cursor, and the ``temp_instructions`` /
``model_settings`` needed to rebuild a faithful retry envelope when a
Discord post fails. We keep all of that (wire included, for one
consistent lookup) in the bridge-local :class:`PendingWires` map that
:class:`BridgeIngress` populates on the way in. The map and the
consumer share a process; this works as long as both live in the
bridge daemon.

Co-existence with the calfkit reply dispatcher: the bridge's
:class:`~calfkit.Client` is connected with
``reply_topic="discord.outbox"`` so the dispatcher's subscriber and
this consumer's subscriber sit in different consumer groups on the
same topic. Kafka multicasts each envelope to both. The dispatcher's
"no pending future" WARNING is therefore expected on every agent reply
(no caller has registered a future); it's noise from a benign code
path, not a defect.

Gate semantics: a single ``final_output_parts`` non-emptiness gate
filters out intermediate hops (tool completions, mid-loop state
transitions) — calfkit's consumer-node docstring recommends this
exact idiom (see ``calfkit.nodes.consumer.ConsumerNode``). Other
filtering (non-agent emitter, unknown agent_id, empty output) happens
inside the closure since those checks need :class:`ConsumerContext`
fields, not just ``ctx``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

import discord
from calfkit import ConsumerNode
from calfkit._vendor.pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from calfkit.client import Client
from calfkit.models import ConsumerContext, SessionRunContext

from calfcord.agents.memory import memory_prompt_deps_for_registry
from calfcord.agents.phonebook import (
    phonebook_from_registry,
    phonebook_to_deps,
)
from calfcord.bridge.pending_wires import PendingEntry, PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.steps import _render_tree_blocks
from calfcord.bridge.steps_toggle import build_toggle_button
from calfcord.bridge.transcripts import TranscriptRow, TranscriptStoreLike
from calfcord.bridge.wire import WireMessage
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import (
    DiscordPersonaSender,
    Persona,
    ReplyContext,
)
from calfcord.discord.retry_feedback import (
    MAX_REPLY_RETRY_ATTEMPTS,
    NON_AGENT_FIXABLE_STATUSES,  # noqa: F401  re-export pinned by TestRetryFeedbackSharedSymbols
    build_retry_history,
    build_retry_reminder,
    chunk_split,
    classify_error,
)
from calfcord.topics import DISCORD_OUTBOX_TOPIC

logger = logging.getLogger(__name__)

DEFAULT_OUTBOX_TOPIC: Final[str] = DISCORD_OUTBOX_TOPIC
DEFAULT_CONSUMER_NODE_ID: Final[str] = "discord-outbox-sink"

# Backoff between our one extra retry attempt. discord.py already does 5
# internal retries for 429/5xx with its own escalating sleep (see
# ``discord.webhook.async_.AsyncWebhookAdapter.request``); our second pass
# is best-effort cleanup for the case where its budget was exhausted by a
# longer-than-usual burst, and usually won't succeed against a multi-hour
# outage. Kept short on purpose: the Worker's default ``max_workers=1``
# means a long retry stalls the entire outbox queue, which directly
# undermines the multi-agent burst case this consumer was added to handle.
_SERVER_ERROR_RETRY_DELAY_SECONDS: Final[float] = 2.0

_AGENT_INBOX_TOPIC_TEMPLATE: Final[str] = "agent.{agent_id}.in"
"""Per-agent inbox topic. The outbox's retry path publishes to this
topic to re-invoke the agent with a revised request. Must match
:data:`calfcord.agents.factory._AGENT_INBOX_TOPIC_TEMPLATE`
and :data:`calfcord.tools.builtin.private_chat._AGENT_INBOX_TOPIC_TEMPLATE`."""


def build_outbox_consumer(
    persona_sender: DiscordPersonaSender,
    registry: AgentRegistry,
    pending_wires: PendingWires,
    calfkit_client: Client,
    *,
    transcript_store: TranscriptStoreLike,
    subscribe_topic: str = DEFAULT_OUTBOX_TOPIC,
    node_id: str = DEFAULT_CONSUMER_NODE_ID,
) -> ConsumerNode[str]:
    """Construct the bridge's outbox consumer node.

    Args:
        persona_sender: The bridge's REST-only Discord client. Used to
            post the reply under the responding agent's persona via
            its per-channel webhook.
        registry: Roster of agents. Resolves
            ``ConsumerContext.emitter_node_id`` to a :class:`Persona`. An
            unknown emitter id is logged and skipped (defensive — the
            bridge is the only producer to ``discord.channel.*.in``,
            so this should only fire if an agent's ``node_id`` drifts
            from its ``.md`` ``name``).
        pending_wires: Bridge-local store of in-flight entries; see the
            module docstring.
        calfkit_client: The bridge's calfkit Client. Used by the
            retry-with-feedback path to publish a revised invocation
            envelope to ``agent.{aid}.in`` when a Discord post fails
            with an agent-fixable error (length, formatting). The
            outbox's "fire-and-forget cancel" of the retry's
            ``InvocationHandle._future`` matches the existing
            ingress pattern.
        transcript_store: Bridge-local SQLite store and the SOLE
            transcript writer. On the terminal hop, when the turn used
            tools (the structured slice ``message_history[initial_len:-1]``
            renders to at least one step) AND the store is ``enabled``,
            the consumer attaches the expand toggle to the reply and —
            *after* a successful post — writes the complete transcript row
            keyed on ``correlation_id``. Pure-text turns write no row. When
            the store failed to open (a :class:`NullTranscriptStore` with
            ``enabled=False``), neither the toggle nor the write happens —
            so users never get a dead button with no row behind it.
        subscribe_topic: Topic the consumer listens on. Defaults to
            the project-wide ``discord.outbox``. Overridable for tests.
        node_id: Identifier the Worker uses as the Kafka consumer
            ``group_id``. Stable across restarts so offsets persist —
            two bridge processes running in parallel would load-balance
            the egress (one would handle each partition), which is not
            recommended but not catastrophic.

    Returns:
        A :class:`ConsumerNode` ready to register on a
        :class:`~calfkit.Worker`. The Worker subscribes it with
        FastStream's default ``auto_offset_reset="latest"`` — the
        consumer ignores any backlog that pre-dates its boot, which
        matches the reply dispatcher's behavior.
    """

    def _final_output_parts_gate(ctx: SessionRunContext) -> bool:
        # Skip intermediate hops (mid-loop transitions, tool completions).
        # See ``calfkit.nodes.consumer.ConsumerNode`` docstring.
        return bool(ctx.state.final_output_parts)

    async def _post_reply(result: ConsumerContext[str]) -> None:
        entry = pending_wires.get(result.correlation_id)
        if entry is None:
            # Foreign producer on the topic, or a reply landed after the
            # bridge restarted and lost the pending entry. DEBUG because
            # the latter is a normal-operations scenario.
            logger.debug(
                "outbox saw correlation_id=%s emitter=%s with no pending entry; skipping",
                result.correlation_id,
                result.emitter_node_id,
            )
            return
        wire = entry.wire

        if result.emitter_node_kind != "agent" or not result.emitter_node_id:
            logger.warning(
                "non-agent emitter on outbox event_id=%s id=%s kind=%s",
                wire.event_id,
                result.emitter_node_id,
                result.emitter_node_kind,
            )
            return

        spec = registry.by_id(result.emitter_node_id)
        if spec is None:
            logger.warning(
                "unknown agent emitter=%s event_id=%s",
                result.emitter_node_id,
                wire.event_id,
            )
            return

        text = (result.output or "").strip()
        if not text:
            logger.info(
                "agent %s returned empty output event_id=%s; skipping post",
                result.emitter_node_id,
                wire.event_id,
            )
            return

        # Structured slice of THIS turn (the cumulative terminal
        # message_history minus the channel-history prefix and minus the
        # final answer ModelResponse). When it renders to at least one
        # step, the turn used tools / emitted interim text → attach the
        # expand toggle and (after a successful post) write the durable
        # transcript. A pure-text turn renders to an empty list and
        # behaves exactly as before (no toggle, no row). The
        # ``transcript_store.enabled`` gate suppresses both when the store
        # failed to open (a NullTranscriptStore) — no dead button users
        # could click with no row behind it.
        delta = _turn_delta(result, entry)
        rendered = _render_step_count(delta, wire)
        extra_buttons: list[discord.ui.Button[Any]] | None = (
            [build_toggle_button(rendered)] if rendered and transcript_store.enabled else None
        )

        persona = Persona(name=spec.display_name, avatar_url=spec.avatar_url)
        # When the triggering event originated in a thread, post the reply
        # into that thread; the webhook still hosts on the parent
        # ``wire.channel_id``. ``None`` for a top-level message ⇒ posts to
        # the channel as before. See :attr:`WireMessage.thread_id`.
        thread_id = wire.thread_id
        try:
            sent = await _send_with_one_retry_on_outage(
                persona_sender,
                persona=persona,
                channel_id=wire.channel_id,
                content=text,
                reply_to=ReplyContext.from_wire(wire),
                extra_buttons=extra_buttons,
                thread_id=thread_id,
            )
        except discord.DiscordException as e:
            # Catch ``DiscordException`` (not just ``HTTPException``) so
            # :class:`discord.RateLimited` — which is a ``DiscordException``
            # but NOT an ``HTTPException`` subclass per discord.py's
            # ``errors.py:158`` — is also funneled through the failure
            # handler instead of crashing into the calfkit consumer
            # framework. The handler treats :class:`RateLimited` as
            # non-retryable (analogous to a 429) since rate-limit
            # backoff isn't agent-fixable.
            await _handle_post_failure(
                error=e,
                entry=entry,
                agent_id=result.emitter_node_id,
                persona=persona,
                failed_text=text,
                client=calfkit_client,
                persona_sender=persona_sender,
                pending_wires=pending_wires,
                registry=registry,
                transcript_store=transcript_store,
                correlation_id=result.correlation_id,
                turn_delta=delta if rendered and transcript_store.enabled else None,
            )
            return
        except (TypeError, RuntimeError) as e:
            # ``DiscordPersonaSender.send`` raises two NON-Discord,
            # operator-actionable errors that ``except DiscordException``
            # above does not catch: ``TypeError`` when ``wire.channel_id``
            # resolves to a non-text channel (webhooks need a parent text
            # channel — see ``persona._fetch_text_channel``) and
            # ``RuntimeError`` when the sender was never started. Neither is
            # agent-fixable or transient, and ``classify_error`` only
            # understands Discord exceptions, so route them to a loud drop
            # here rather than letting a raw traceback escape into the
            # calfkit consumer — which would forfeit this consumer's
            # structured per-failure logging and its best-effort
            # never-raises contract, and (depending on redelivery) risk a
            # re-consume that double-posts. No transcript row is written
            # (the post never landed), so the (never-posted) reply's toggle
            # has nothing to read — the documented missing-row degradation.
            logger.error(
                "outbox post failed channel_id=%s event_id=%s agent=%s: "
                "non-retryable sender error type=%s (%s); dropping",
                wire.channel_id,
                wire.event_id,
                result.emitter_node_id,
                type(e).__name__,
                e,
            )
            return

        # Successful post. If the turn used tools (and the store is
        # enabled), persist the transcript row keyed on correlation_id
        # (idempotent on outbox retries). This is the single success
        # point — a turn that dropped / stayed retry-pending writes no
        # row, so the toggle on its (never-posted) reply has nothing to
        # read, which is the documented degradation. A disabled store
        # (NullTranscriptStore) writes nothing and got no toggle above.
        if rendered and transcript_store.enabled:
            await _write_transcript(
                transcript_store,
                correlation_id=result.correlation_id,
                wire=wire,
                agent_id=result.emitter_node_id,
                final_message_id=sent.id,
                delta=delta,
            )

        retry_attempt = pending_wires.get_retry_count(result.correlation_id)
        if retry_attempt > 0:
            logger.info(
                "agent retry succeeded after %d attempt(s) event_id=%s agent=%s reply_id=%s channel=%s",
                retry_attempt,
                wire.event_id,
                result.emitter_node_id,
                sent.id,
                wire.channel_id,
            )
        else:
            logger.info(
                "posted reply event_id=%s agent=%s reply_id=%s channel=%s",
                wire.event_id,
                result.emitter_node_id,
                sent.id,
                wire.channel_id,
            )

    return ConsumerNode[str](
        node_id=node_id,
        subscribe_topics=subscribe_topic,
        consume_fn=_post_reply,
        agent_output_type=str,
        gates=[_final_output_parts_gate],
    )


async def _send_with_one_retry_on_outage(
    persona_sender: DiscordPersonaSender,
    *,
    persona: Persona,
    channel_id: int,
    content: str,
    reply_to: ReplyContext | None,
    extra_buttons: list[discord.ui.Button[Any]] | None = None,
    thread_id: int | None = None,
) -> SentMessage:
    """Send via the persona webhook with one extra attempt on 5xx.

    Re-raises every :class:`discord.HTTPException` after exhausting
    the (one-extra-attempt) 5xx smoothing budget. The caller is
    responsible for triage:

    * :func:`_post_reply` catches the re-raised exception and routes
      to :func:`_handle_post_failure`, which decides between agent
      retry, chunk-split fallback, or operator-actionable drop based
      on the HTTP status.
    * :func:`_post_chunked_fallback` calls this for each chunk and
      handles per-chunk failure independently.

    Retry policy (unchanged from the previous behavior):
        - First-attempt :class:`discord.DiscordServerError` (5xx) →
          short delay, exactly one extra attempt.
        - Any other first-attempt error → no extra attempt; re-raised
          immediately.
        - Second-attempt failure (whatever kind) → re-raised.

    ``extra_buttons`` (e.g. the step-transcript expand toggle) are
    forwarded verbatim on both attempts so a 5xx-then-success reply
    still carries the toggle. ``thread_id`` (when the event came from a
    thread) is likewise forwarded on both attempts.
    """
    try:
        return await persona_sender.send(
            persona=persona,
            channel_id=channel_id,
            content=content,
            reply_to=reply_to,
            extra_buttons=extra_buttons,
            thread_id=thread_id,
        )
    except discord.DiscordServerError as e:
        logger.warning(
            "discord 5xx on persona post; retrying once after %.1fs status=%s: %s",
            _SERVER_ERROR_RETRY_DELAY_SECONDS,
            e.status,
            e,
        )

    # 5xx on first attempt; sleep + retry. Any failure on the retry
    # propagates to the caller for triage.
    await asyncio.sleep(_SERVER_ERROR_RETRY_DELAY_SECONDS)
    return await persona_sender.send(
        persona=persona,
        channel_id=channel_id,
        content=content,
        reply_to=reply_to,
        extra_buttons=extra_buttons,
        thread_id=thread_id,
    )


def _turn_delta(result: ConsumerContext[str], entry: PendingEntry) -> list[ModelMessage]:
    """Return THIS turn's structured slice of the cumulative history.

    The terminal ``message_history`` is cumulative (append-only). The
    turn's transcript is everything after the channel-history prefix
    (``entry.initial_message_history_length``) and before the trailing
    final-answer ``ModelResponse`` — i.e. ``[initial_len:-1]``. The
    outbox posts that final answer's text to the channel; the slice is
    the tool calls / returns / interim text the agent produced getting
    there. Mirrors the steps consumer's terminal-hop slice so the toggle
    and the live counter render the same parts.

    Returns ``[]`` for a pure-text turn (history is just the prefix plus
    the final answer, so the slice is empty).
    """
    history = result.message_history
    initial_len = entry.initial_message_history_length
    if not history:
        return []
    return list(history[initial_len:-1])


def _render_step_count(delta: list[ModelMessage], wire: WireMessage) -> int:
    """Count the renderable step blocks in ``delta``, defensively.

    Wraps :func:`~calfcord.bridge.steps._render_tree_blocks`
    (which can raise — ``ToolCallPart.args_as_json_str`` blows up on malformed
    args) so a single bad turn never crashes the reply post. A tool call and
    its result render as ONE block, so the count credits a tool use once. On
    failure the turn is treated as having zero steps: the reply still posts,
    just without the toggle or a transcript row (degraded, not fatal). Mirrors
    the steps consumer's identical guard.
    """
    try:
        return len(_render_tree_blocks(delta))
    except Exception:
        logger.exception(
            "outbox _render_tree_blocks raised computing step count event_id=%s; "
            "posting reply without toggle/transcript",
            wire.event_id,
        )
        return 0


async def _write_transcript(
    transcript_store: TranscriptStoreLike,
    *,
    correlation_id: str,
    wire: WireMessage,
    agent_id: str,
    final_message_id: int,
    delta: list[ModelMessage],
) -> None:
    """Persist the completed turn's transcript row (the SOLE writer).

    Called only after a successful reply post, with a non-empty
    ``delta``. Serializes the slice with pydantic-ai's
    ``ModelMessagesTypeAdapter`` and upserts keyed on ``correlation_id``
    (idempotent across outbox retries). ``conversation_key`` is the
    thread-aware source channel (``wire.source_channel_id`` when the wire
    originated in a thread, else the parent ``wire.channel_id``).

    Best-effort: a store failure must not crash the consumer or undo the
    already-posted reply, so it is logged and swallowed.
    """
    try:
        delta_json = ModelMessagesTypeAdapter.dump_json(delta).decode()
        await transcript_store.write_turn(
            TranscriptRow(
                correlation_id=correlation_id,
                conversation_key=str(wire.source_channel_id or wire.channel_id),
                agent_id=str(agent_id),
                final_message_id=str(final_message_id),
                delta_json=delta_json,
                created_at=int(time.time()),
            )
        )
    except Exception:
        # Never let a transcript-write failure escape into the calfkit
        # consumer framework — the reply is already posted; the steps
        # toggle on it would simply read no row (the documented
        # missing-row degradation).
        logger.exception(
            "outbox failed to write transcript correlation_id=%s reply_id=%s; step toggle will have no row to expand",
            correlation_id,
            final_message_id,
        )


# --- Retry-with-feedback path -------------------------------------------------


async def _handle_post_failure(
    *,
    error: discord.DiscordException,
    entry: PendingEntry,
    agent_id: str,
    persona: Persona,
    failed_text: str,
    client: Client,
    persona_sender: DiscordPersonaSender,
    pending_wires: PendingWires,
    registry: AgentRegistry,
    transcript_store: TranscriptStoreLike,
    correlation_id: str,
    turn_delta: list[ModelMessage] | None,
) -> None:
    """Triage a Discord post failure into one of four branches:

    1a. **Non-retryable HTTP status** (in :data:`NON_AGENT_FIXABLE_STATUSES`)
        or **:class:`discord.RateLimited`** (which is a
        ``DiscordException`` but not an ``HTTPException`` subclass):
        operator-actionable WARN, drop. The agent can't fix permission
        / channel-gone / rate-limit by re-thinking its reply.
    1b. **5xx that survived** :func:`_send_with_one_retry_on_outage`'s
        smoothing: log + drop. Discord is down; agent retry can't help.
    2.  **Retry budget exhausted** (count from
        :meth:`PendingWires.get_retry_count` >=
        :data:`MAX_REPLY_RETRY_ATTEMPTS`): the LLM has had its chances;
        fall back to :func:`_post_chunked_fallback` so the user still
        receives the content (segmented).
    3.  **Agent retry**: claim the next attempt via
        :meth:`PendingWires.increment_retry`, then publish a retry
        envelope to ``agent.{aid}.in`` via :func:`_publish_retry`.
        The agent's next ReturnCall returns through ``discord.outbox``
        with the same correlation_id, lands here again, and the cycle
        continues until success or budget exhaustion.

    Best-effort never-raises. The contract is "the outbox never
    crashes on a Discord error" — operators get logs, the user gets
    something eventually (a successful reply, a chunked sequence, or
    a logged drop), but no exception escapes upward in the normal
    case. :class:`asyncio.CancelledError` (subclass of
    :class:`BaseException`, not :class:`Exception`) propagates by
    design — a bridge shutdown should not be blocked by retry
    fallback logic.

    ``turn_delta`` is the turn's structured slice when it used tools
    (else ``None``). It is forwarded only to :func:`_post_chunked_fallback`:
    when the fallback actually delivers, it attaches the expand toggle to
    the FIRST chunk and writes the transcript with that chunk's id.
    """
    wire = entry.wire
    decision = classify_error(error)

    if decision == "drop":
        # Per-status operator-actionable logging: the most common drop
        # cases (404 channel-gone, 403 missing-permission) get a hint
        # about what the operator must check; everything else gets a
        # generic status+error line so the operator still has enough to
        # diagnose. ``classify_error`` is the policy gate; the logging
        # cases below are presentation.
        if isinstance(error, discord.RateLimited):
            logger.warning(
                "outbox post failed channel_id=%s event_id=%s agent=%s: "
                "discord.py rate-limit backoff exhausted (%s); operator "
                "should investigate burst traffic patterns",
                wire.channel_id,
                wire.event_id,
                agent_id,
                error,
            )
        elif not isinstance(error, discord.HTTPException):
            # A ``DiscordException`` that's neither ``RateLimited`` nor
            # ``HTTPException`` is something discord.py added since we
            # last looked.
            logger.warning(
                "outbox post failed channel_id=%s event_id=%s agent=%s: unrecognized discord exception type=%s (%s)",
                wire.channel_id,
                wire.event_id,
                agent_id,
                type(error).__name__,
                error,
            )
        elif error.status == 404:
            logger.warning(
                "outbox post failed channel_id=%s event_id=%s agent=%s: "
                "channel or webhook not found (%s); operator must check "
                "the channel exists",
                wire.channel_id,
                wire.event_id,
                agent_id,
                error,
            )
        elif error.status == 403:
            logger.warning(
                "outbox post failed channel_id=%s event_id=%s agent=%s: "
                "forbidden (%s); operator must verify Manage Webhooks "
                "permission",
                wire.channel_id,
                wire.event_id,
                agent_id,
                error,
            )
        else:
            logger.warning(
                "outbox post failed (not retryable) channel_id=%s event_id=%s agent=%s status=%s: %s",
                wire.channel_id,
                wire.event_id,
                agent_id,
                error.status,
                error,
            )
        return

    # ``classify_error`` only returns ``"transient"`` / ``"agent_fixable"``
    # for ``HTTPException`` (RateLimited and bare DiscordException both
    # short-circuit to ``"drop"`` above), so ``error.status`` is safe to
    # access from here. Defensive runtime check (NOT ``assert``, which
    # would be stripped under ``python -O``): if a future change to
    # ``classify_error`` ever violates the invariant, surface an
    # operator-actionable ERROR and drop the message rather than
    # crashing the consumer with an :class:`AttributeError` on the
    # ``.status`` access below — the outbox's "best-effort never-raises"
    # contract (this function's docstring) outweighs the cost of a
    # missed retry.
    if not isinstance(error, discord.HTTPException):
        logger.error(
            "classify_error invariant violated: returned %r for non-HTTPException "
            "type=%s on channel_id=%s event_id=%s agent=%s; dropping",
            decision,
            type(error).__name__,
            wire.channel_id,
            wire.event_id,
            agent_id,
        )
        return

    if decision == "transient":
        # 5xx survived _send_with_one_retry_on_outage's smoothing.
        # Discord is down; agent retry can't help.
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s status=%s: discord 5xx + extra retry exhausted (%s)",
            wire.channel_id,
            wire.event_id,
            agent_id,
            error.status,
            error,
        )
        return

    # decision == "agent_fixable"
    retry_count = pending_wires.get_retry_count(wire.event_id)

    # Branch 2: retry budget exhausted → chunk-split fallback.
    if retry_count >= MAX_REPLY_RETRY_ATTEMPTS:
        logger.warning(
            "retry budget exhausted attempt=%d max=%d; chunk-splitting reply event_id=%s agent=%s status=%s: %s",
            retry_count,
            MAX_REPLY_RETRY_ATTEMPTS,
            wire.event_id,
            agent_id,
            error.status,
            error,
        )
        await _post_chunked_fallback(
            persona_sender,
            persona,
            wire,
            failed_text,
            transcript_store=transcript_store,
            correlation_id=correlation_id,
            agent_id=agent_id,
            turn_delta=turn_delta,
        )
        return

    # Branch 3: agent retry. Claim the attempt atomically; the LRU
    # could in principle evict the entry mid-retry-sequence under
    # pathological load, in which case fall back to chunk-split.
    new_attempt = pending_wires.increment_retry(wire.event_id)
    if new_attempt is None:
        logger.warning(
            "pending entry evicted before retry could be claimed; chunk-splitting event_id=%s agent=%s",
            wire.event_id,
            agent_id,
        )
        await _post_chunked_fallback(
            persona_sender,
            persona,
            wire,
            failed_text,
            transcript_store=transcript_store,
            correlation_id=correlation_id,
            agent_id=agent_id,
            turn_delta=turn_delta,
        )
        return

    logger.info(
        "outbox post failed; triggering agent retry attempt=%d channel_id=%s event_id=%s agent=%s status=%s: %s",
        new_attempt,
        wire.channel_id,
        wire.event_id,
        agent_id,
        error.status,
        error,
    )

    try:
        await _publish_retry(client, registry, entry, agent_id, failed_text, error)
    except Exception:
        # ``except Exception`` (not ``BaseException``) so
        # ``asyncio.CancelledError`` propagates — a shutdown mid-publish
        # is an intentional teardown, not a retry-publish failure.
        # Kafka errors, registry mismatches, and bugs in
        # ``_publish_retry`` itself all funnel here; we log + chunk-
        # split. The counter has already advanced (we claimed before
        # publish) so a future retry attempt for the same wire would
        # correctly see the higher count if one ever fired.
        logger.exception(
            "retry publish failed event_id=%s agent=%s; falling back to chunk-split",
            wire.event_id,
            agent_id,
        )
        await _post_chunked_fallback(
            persona_sender,
            persona,
            wire,
            failed_text,
            transcript_store=transcript_store,
            correlation_id=correlation_id,
            agent_id=agent_id,
            turn_delta=turn_delta,
        )


async def _publish_retry(
    client: Client,
    registry: AgentRegistry,
    entry: PendingEntry,
    agent_id: str,
    failed_text: str,
    error: discord.HTTPException,
) -> None:
    """Publish the retry envelope to ``agent.{aid}.in``.

    The retry's ``message_history`` is constructed as:

        original_message_history          # channel-context projection
        + ModelRequest(original_prompt)   # the user's question
        + ModelResponse(failed_text)      # the LLM's rejected attempt

    The new ``user_prompt`` is the ``<system-reminder>``-tagged
    feedback (see :func:`build_retry_reminder`). Pydantic-ai's
    ``_clean_message_history`` will merge any adjacent same-role
    parts before the provider mapper sees the list, so the LLM gets
    a well-formed alternating conversation ending in the system-
    reminder user turn.

    The retry preserves the original invocation's
    ``temp_instructions`` (peer roster for A2A) AND
    ``model_settings`` (per-call thinking_effort etc.) so a
    user-configured high-effort run doesn't silently degrade to
    the model client's default on the retry attempt — a real
    quality regression we'd otherwise inflict on every retry.

    The ``correlation_id`` matches the original wire's ``event_id``
    so the eventual successful reply lands on the same
    :class:`PendingWires` entry and posts to Discord as an inline
    reply to the original user message — the user sees one reply
    anchored to their question regardless of retries.

    The handle's future is cancelled (fire-and-forget) matching the
    :class:`BridgeIngress` pattern; the eventual reply is observed
    by the outbox consumer in a different consumer group. The
    cancel is guarded against a hypothetical future where
    ``InvocationHandle`` makes ``_future`` lazy/optional — a missing
    attribute is logged at DEBUG and swallowed because the publish
    itself already succeeded; raising here would cause the caller's
    chunk-split fallback to double-post (the retry envelope already
    in flight + the chunked copy from the fallback).
    """
    wire = entry.wire
    reminder = build_retry_reminder(error, failed_text)
    retry_history = build_retry_history(
        original_history=entry.message_history,
        original_user_prompt=wire.content,
        failed_text=failed_text,
    )

    # Rebuild the phonebook so any A2A-tool-using agent's tools still
    # work on retry. The outbox already has the registry; rebuilding
    # the phonebook is cheap (~one list-comprehension over the
    # registry's entries).
    phonebook = phonebook_from_registry(registry)

    # Re-ship the memory-prompt template on the retry, exactly as the ingress
    # does on the first attempt (see ``BridgeIngress._memory_prompt_deps``).
    # Without this a memory-enabled agent would run the corrective turn with no
    # memory block — its instructions hook would find no template in ``deps`` and
    # silently return None, dropping its memory context on precisely the turn it
    # is being asked to fix a reply. This retry path is rare (only on an
    # LLM-fixable Discord post failure), so a load failure is logged per
    # occurrence rather than deduped to a one-shot like the hot ingress path.
    try:
        memory_deps = memory_prompt_deps_for_registry(registry.all())
    except ValueError:
        logger.error(
            "failed to load the memory prompt for retry of agent=%s; the retry "
            "will run without its memory block",
            agent_id,
            exc_info=True,
        )
        memory_deps = {}

    handle = await client.invoke_node(
        user_prompt=reminder,
        topic=_AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id=agent_id),
        correlation_id=wire.event_id,
        deps={
            "discord": wire.model_dump(mode="json"),
            "phonebook": phonebook_to_deps(phonebook),
            **memory_deps,
        },
        output_type=str,
        temp_instructions=entry.temp_instructions,
        message_history=retry_history,
        model_settings=entry.model_settings,
    )
    # Guard against an InvocationHandle without a ``_future``. The
    # cancel is a leak-prevention optimization, not a correctness
    # invariant — the bridge's outbox consumer (a separate consumer
    # group) observes the eventual reply regardless. Don't let an
    # AttributeError here misroute control flow to the
    # ``except Exception`` in :func:`_handle_post_failure`'s caller,
    # which would chunk-split a reply whose retry has already been
    # published.
    future = getattr(handle, "_future", None)
    if future is not None:
        try:
            future.cancel()
        except Exception:
            logger.debug(
                "handle._future.cancel() raised on retry publish "
                "event_id=%s agent=%s; pending-future leak possible "
                "but publish already succeeded",
                wire.event_id,
                agent_id,
                exc_info=True,
            )


async def _post_chunked_fallback(
    persona_sender: DiscordPersonaSender,
    persona: Persona,
    wire: WireMessage,
    text: str,
    *,
    transcript_store: TranscriptStoreLike,
    correlation_id: str,
    agent_id: str,
    turn_delta: list[ModelMessage] | None,
) -> None:
    """Final fallback: split ``text`` into ≤2000-char chunks and post
    each as the same persona.

    The first chunk uses Discord's inline-reply anchor so the chain
    appears as a "reply to" the user's original question; subsequent
    chunks are bare continuations from the same persona, which
    Discord renders as natural follow-ups directly below the first.

    When ``turn_delta`` is non-empty (the turn used tools), the expand
    toggle is attached to the FIRST chunk only and — if that first chunk
    posts successfully — the transcript row is written against the first
    chunk's id, so the toggle has a row to expand. Subsequent chunks are
    unchanged. A first-chunk failure writes no row (no host message id).

    Per-chunk failures are logged independently so partial delivery
    is preserved (chunks 1 and 3 still post if chunk 2 fails). If
    *every* chunk fails — typically a systemic permission issue
    (the channel was deleted or the bot lost Manage Webhooks
    between the agent's run and the fallback) — an additional
    summary WARN is logged identifying the most-frequent status
    code so operators see one actionable line instead of having to
    aggregate N per-chunk ERRORs themselves.

    Catches :class:`discord.DiscordException` (broader than
    :class:`HTTPException`) so :class:`RateLimited` at the chunk
    layer doesn't bubble up either — the chunk fallback is the
    last resort; nothing useful can route around its failures.
    """
    chunks = chunk_split(text)
    if not chunks:
        logger.warning(
            "chunk-split fallback received empty text; nothing to post event_id=%s",
            wire.event_id,
        )
        return

    # Toggle rides the first chunk only when the turn used tools AND the
    # store is enabled (a disabled NullTranscriptStore gets no toggle and
    # no row — same gate as the main path). The step count is recomputed
    # via the same defensive guard as the main path so a malformed-args
    # turn can't crash the fallback.
    write_transcript = bool(turn_delta) and transcript_store.enabled
    first_chunk_buttons: list[discord.ui.Button[Any]] | None = (
        [build_toggle_button(_render_step_count(turn_delta, wire))] if write_transcript else None
    )

    # Post each chunk into the originating thread when present (parent
    # otherwise); the webhook hosts on the parent ``wire.channel_id`` either
    # way. See :attr:`WireMessage.thread_id`.
    thread_id = wire.thread_id
    total = len(chunks)
    failure_statuses: list[int | None] = []
    for i, chunk in enumerate(chunks):
        reply_to = ReplyContext.from_wire(wire) if i == 0 else None
        extra_buttons = first_chunk_buttons if i == 0 else None
        try:
            sent = await persona_sender.send(
                persona=persona,
                channel_id=wire.channel_id,
                content=chunk,
                reply_to=reply_to,
                extra_buttons=extra_buttons,
                thread_id=thread_id,
            )
            logger.info(
                "chunk-split posted chunk %d/%d event_id=%s reply_id=%s channel_id=%s",
                i + 1,
                total,
                wire.event_id,
                sent.id,
                wire.channel_id,
            )
            if i == 0 and write_transcript and turn_delta:
                # First chunk delivered and the turn used tools (store
                # enabled): persist the transcript against this chunk's id
                # so its toggle can expand.
                await _write_transcript(
                    transcript_store,
                    correlation_id=correlation_id,
                    wire=wire,
                    agent_id=agent_id,
                    final_message_id=sent.id,
                    delta=turn_delta,
                )
        except (discord.DiscordException, TypeError, RuntimeError) as e:
            # Also catch the NON-Discord ``TypeError`` / ``RuntimeError``
            # ``persona_sender.send`` can raise (non-text channel, sender
            # not started). This is the last-resort path, so a per-chunk
            # failure of ANY recognized kind must be recorded and the loop
            # must continue — otherwise one bad chunk aborts the rest and
            # the "all chunks failed" summary below never fires, defeating
            # the independent-partial-delivery guarantee. Still
            # ``Exception``-bounded (never ``BaseException``) so a shutdown
            # ``CancelledError`` propagates. ``getattr(..., "status", None)``
            # yields ``None`` for the non-HTTP errors, which the summary
            # tolerates.
            status = getattr(e, "status", None)
            failure_statuses.append(status)
            logger.error(
                "chunk-split failed chunk %d/%d event_id=%s channel_id=%s status=%s: %s",
                i + 1,
                total,
                wire.event_id,
                wire.channel_id,
                status,
                e,
            )

    if failure_statuses and len(failure_statuses) == total:
        # All chunks failed. Surface a single summary signal so the
        # operator doesn't have to manually aggregate N ERROR lines.
        # Use the most-common status; ties broken by first occurrence.
        dominant_status = max(set(failure_statuses), key=failure_statuses.count)
        logger.warning(
            "chunk-split delivered 0/%d chunks event_id=%s channel_id=%s "
            "dominant_status=%s; reply is fully lost — operator should "
            "verify channel permissions and webhook health",
            total,
            wire.event_id,
            wire.channel_id,
            dominant_status,
        )
