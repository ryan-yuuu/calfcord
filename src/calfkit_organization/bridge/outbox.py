"""Discord outbox consumer — posts every agent reply landing on the outbox.

A long-lived calfkit :class:`ConsumerNodeDef` subscribed to
``discord.outbox`` in its own Kafka consumer group. Every agent
:class:`ReturnCall` landing on the outbox topic produces one invocation
of :func:`build_outbox_consumer`'s closure, so multi-agent flows
(ambient channel messages, team slashes) get all their replies posted
to Discord rather than just the first to win calfkit's reply-dispatcher
race (the dispatcher dedupes by ``correlation_id`` and would silently
drop every reply after the first — see
``calfkit.client.reply_dispatcher._ReplyDispatcher``).

How the wire is recovered: the consumer receives a
:class:`~calfkit.NodeResult`, which carries ``output``, ``state``,
``correlation_id``, ``emitter_node_id``, and ``emitter_node_kind`` —
but **not** ``Envelope.context.deps``. So the original
:class:`WireMessage` (which holds ``channel_id``, ``message_id``, and
the author info needed for the inline-reply UI) is not on the result.
We recover it from the bridge-local :class:`PendingWires` map that
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
exact idiom (see ``calfkit.nodes.consumer.ConsumerNodeDef``). Other
filtering (non-agent emitter, unknown agent_id, empty output) happens
inside the closure since those checks need :class:`NodeResult`
fields, not just ``ctx``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

import discord
from calfkit import ConsumerNodeDef, NodeResult
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from calfkit.client import Client
from calfkit.models import SessionRunContext

from calfkit_organization.agents.phonebook import (
    phonebook_from_registry,
    phonebook_to_deps,
)
from calfkit_organization.bridge.pending_wires import PendingEntry, PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireMessage
from calfkit_organization.discord.messages import SentMessage
from calfkit_organization.discord.persona import (
    DiscordPersonaSender,
    Persona,
    ReplyContext,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTBOX_TOPIC: Final[str] = "discord.outbox"
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

# --- Retry-with-feedback constants --------------------------------------------

NON_AGENT_FIXABLE_STATUSES: Final[frozenset[int]] = frozenset({
    401,   # unauthorized — bot token invalid
    403,   # forbidden — missing Manage Webhooks / View Channel / etc.
    404,   # not found — channel or webhook deleted
    429,   # rate limited — discord.py already retried internally
})
"""Discord HTTP statuses where retrying the agent with a revised reply
cannot possibly succeed. These are infrastructure / permission errors
that require operator action, not agent content adjustment. The outbox
logs WARN and drops the reply on these statuses (existing behavior
preserved).

5xx is NOT in this set — :func:`_handle_post_failure` handles it via
an explicit ``status >= 500`` branch (rather than membership) because
the inner :func:`_send_with_one_retry_on_outage` already smooths
transient 5xx with one extra attempt; only a *persistent* 5xx reaches
the no-agent-retry drop path.

:class:`discord.RateLimited` (which is NOT an HTTPException subclass
in discord.py — it inherits from :class:`discord.DiscordException`
directly) is also handled by :meth:`_handle_post_failure` even
though it has no HTTP status; see the catch block in
:meth:`_post_reply`."""

MAX_REPLY_RETRY_ATTEMPTS: Final[int] = 2
"""Number of LLM retries triggered after the original post failure.
Total LLM attempts before falling back to chunk-splitting =
1 (original) + MAX_REPLY_RETRY_ATTEMPTS = 3. Picked at 2 because:

* Discord 4xx errors (length, formatting) are usually self-correcting
  on attempt 2 once the LLM is told the problem.
* Each retry is a full LLM round-trip (~5-15s); 2 retries adds at most
  ~30s before chunked fallback. 3+ retries makes the user wait too
  long with no visible signal.
* Bounded LLM cost: pathological retry loops cost ~3x a normal
  invocation, never unbounded."""

CHUNK_SAFE_SIZE: Final[int] = 1990
"""Max chars per chunk in the chunk-split fallback. Discord's hard
content limit is 2000; the 10-char safety buffer absorbs the occasional
emoji / encoding surprise that tips a 1999-char string over the limit."""

_RETRY_REMINDER_OVERRIDES: dict[tuple[int, int], str] = {}
"""Per-(HTTP status, JSON error code) overrides for the retry-reminder
text. Empty by default — the generic template surfaces Discord's own
error message to the LLM, which modern frontier models reliably parse
and adapt to. Populate only when empirical evidence shows the LLM
needs more pointed guidance for a specific Discord error code.

Format: ``(status, code): "Custom reminder body."`` Both status and
code are concrete integers — no wildcard support in v1. If a future
empirical case needs "any code of this status," extend the lookup
in :func:`build_retry_reminder` then; YAGNI says don't add the
wildcard tier preemptively."""

_AGENT_INBOX_TOPIC_TEMPLATE: Final[str] = "agent.{agent_id}.in"
"""Per-agent inbox topic. The outbox's retry path publishes to this
topic to re-invoke the agent with a revised request. Must match
:data:`calfkit_organization.agents.factory._AGENT_INBOX_TOPIC_TEMPLATE`
and :data:`calfkit_organization.tools.private_chat._AGENT_INBOX_TOPIC_TEMPLATE`."""


def build_outbox_consumer(
    persona_sender: DiscordPersonaSender,
    registry: AgentRegistry,
    pending_wires: PendingWires,
    calfkit_client: Client,
    *,
    subscribe_topic: str = DEFAULT_OUTBOX_TOPIC,
    node_id: str = DEFAULT_CONSUMER_NODE_ID,
) -> ConsumerNodeDef[str]:
    """Construct the bridge's outbox consumer node.

    Args:
        persona_sender: The bridge's REST-only Discord client. Used to
            post the reply under the responding agent's persona via
            its per-channel webhook.
        registry: Roster of agents. Resolves
            ``NodeResult.emitter_node_id`` to a :class:`Persona`. An
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
        subscribe_topic: Topic the consumer listens on. Defaults to
            the project-wide ``discord.outbox``. Overridable for tests.
        node_id: Identifier the Worker uses as the Kafka consumer
            ``group_id``. Stable across restarts so offsets persist —
            two bridge processes running in parallel would load-balance
            the egress (one would handle each partition), which is not
            recommended but not catastrophic.

    Returns:
        A :class:`ConsumerNodeDef` ready to register on a
        :class:`~calfkit.Worker`. The Worker subscribes it with
        FastStream's default ``auto_offset_reset="latest"`` — the
        consumer ignores any backlog that pre-dates its boot, which
        matches the reply dispatcher's behavior.
    """

    def _final_output_parts_gate(ctx: SessionRunContext) -> bool:
        # Skip intermediate hops (mid-loop transitions, tool completions).
        # See ``calfkit.nodes.consumer.ConsumerNodeDef`` docstring.
        return bool(ctx.state.final_output_parts)

    async def _post_reply(result: NodeResult[str]) -> None:
        entry = pending_wires.get(result.correlation_id)
        if entry is None:
            # Foreign producer on the topic, or a reply landed after the
            # bridge restarted and lost the pending entry. DEBUG because
            # the latter is a normal-operations scenario.
            logger.debug(
                "outbox saw correlation_id=%s emitter=%s with no pending "
                "entry; skipping",
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

        persona = Persona(name=spec.display_name, avatar_url=spec.avatar_url)
        try:
            sent = await _send_with_one_retry_on_outage(
                persona_sender,
                persona=persona,
                channel_id=wire.channel_id,
                content=text,
                reply_to=ReplyContext.from_wire(wire),
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
            )
            return

        retry_attempt = pending_wires.get_retry_count(result.correlation_id)
        if retry_attempt > 0:
            logger.info(
                "agent retry succeeded after %d attempt(s) event_id=%s "
                "agent=%s reply_id=%s channel=%s",
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

    return ConsumerNodeDef[str](
        node_id=node_id,
        subscribe_topics=subscribe_topic,
        consume_fn=_post_reply,
        output_type=str,
        gates=[_final_output_parts_gate],
    )


async def _send_with_one_retry_on_outage(
    persona_sender: DiscordPersonaSender,
    *,
    persona: Persona,
    channel_id: int,
    content: str,
    reply_to: ReplyContext | None,
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

    """
    try:
        return await persona_sender.send(
            persona=persona,
            channel_id=channel_id,
            content=content,
            reply_to=reply_to,
        )
    except discord.DiscordServerError as e:
        logger.warning(
            "discord 5xx on persona post; retrying once after %.1fs status=%s: %s",
            _SERVER_ERROR_RETRY_DELAY_SECONDS, e.status, e,
        )

    # 5xx on first attempt; sleep + retry. Any failure on the retry
    # propagates to the caller for triage.
    await asyncio.sleep(_SERVER_ERROR_RETRY_DELAY_SECONDS)
    return await persona_sender.send(
        persona=persona,
        channel_id=channel_id,
        content=content,
        reply_to=reply_to,
    )



# --- Retry-with-feedback pure helpers -----------------------------------------


def build_retry_reminder(
    error: discord.HTTPException,
    failed_text: str,
) -> str:
    """Build the system-reminder-tagged user message for an agent retry.

    Generic by design: the LLM sees the literal Discord error text in
    a ``<system-reminder>`` block and is trusted to adapt. Modern
    frontier LLMs reliably parse Discord's own error strings (e.g.
    ``"Must be 2000 or fewer in length"``, ``"Cannot send an empty
    message"``, ``"Invalid embed URL"``) and adjust their next reply
    accordingly. No per-error-code customization is needed in v1; the
    override map slot at :data:`_RETRY_REMINDER_OVERRIDES` exists for
    future empirical cases where the generic message is insufficient.

    The ``<system-reminder>`` tag pattern is a convention frontier
    models trained with system-reminder-style data typically treat as
    out-of-band metadata even though it occupies a ``user``-role slot
    on the wire. The explicit "Do NOT mention this error" instruction
    inside the reminder body is the actual enforcement mechanism;
    the tag wrapper is the visual cue that helps the model recognize
    the convention.

    Args:
        error: The :class:`discord.HTTPException` raised by the
            persona-sender. The status, code, and body text are all
            surfaced to the LLM.
        failed_text: The exact reply text the agent emitted that
            Discord rejected. Used in the reminder to give the LLM
            length-context (``"length: 3187 chars"``) without
            duplicating the full failed content (which appears
            separately in the retry envelope's ``message_history``
            as a ``ModelResponse``).

    Returns:
        A string suitable to pass as ``user_prompt`` to
        :meth:`Client.invoke_node` for the retry envelope.
    """
    override = _RETRY_REMINDER_OVERRIDES.get((error.status, error.code))
    if override is not None:
        body = override
    else:
        # ``discord.HTTPException.text`` is the raw JSON-body text from
        # Discord (e.g. ``"Invalid Form Body\nIn content: Must be 2000
        # or fewer in length."``). Falls back to ``str(error)`` which
        # is discord.py's formatted ``"status: code: text"``.
        raw = error.text or str(error)
        body = (
            f"Your previous reply (length: {len(failed_text)} chars) was "
            f"rejected by Discord. The exact error:\n\n"
            f"  HTTP {error.status}: {raw}\n\n"
            f"Please respond again to the user's original question, "
            f"addressing the specific issue above. For example, if the "
            f"content was too long, be more concise; if it contained "
            f"banned formatting, rephrase without it."
        )
    return (
        "<system-reminder>\n"
        f"{body}\n\n"
        "This reminder is system-level — the user does NOT see it. "
        "Do NOT mention this error or that you are retrying.\n"
        "</system-reminder>"
    )


def _chunk_split(text: str, *, max_chars: int = CHUNK_SAFE_SIZE) -> list[str]:
    """Split ``text`` into pieces each ≤ ``max_chars`` for posting as
    consecutive Discord messages.

    Boundary search is greedy from the largest unit down: paragraph
    (``"\n\n"``) → line (``"\n"``) → sentence (``". "``) → word
    (``" "``) → hard cut. The search refuses to split earlier than
    ``max_chars // 2`` so we don't produce a tiny first chunk
    followed by a huge tail.

    Each chunk is right-stripped of trailing whitespace. The split
    preserves all non-boundary characters — joining chunks back with
    the boundary that produced each cut reconstructs (modulo
    boundary whitespace) the original text.

    Args:
        text: The full text to split. May be empty (returns ``[]``).
        max_chars: Maximum characters per chunk. Defaults to
            :data:`CHUNK_SAFE_SIZE` (1990) — Discord's 2000-char
            limit with a 10-char safety buffer.

    Returns:
        A list of chunks in original order. If ``text`` already fits,
        returns ``[text]``. An empty string returns ``[]``.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    min_split = max(1, max_chars // 2)

    while remaining:
        if len(remaining) <= max_chars:
            stripped = remaining.rstrip()
            if stripped:
                chunks.append(stripped)
            break

        candidate = remaining[:max_chars]
        cut_at = -1
        # Prefer larger structural boundaries.
        for separator in ("\n\n", "\n", ". ", " "):
            idx = candidate.rfind(separator)
            if idx >= min_split:
                cut_at = idx + len(separator)
                break

        if cut_at < 0:
            # No good boundary found; hard cut at max_chars.
            cut_at = max_chars

        chunk = remaining[:cut_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut_at:]

    return chunks


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
    """
    wire = entry.wire

    # Branch 1a: explicit non-retryable errors.
    #
    # :class:`discord.RateLimited` has no ``status`` attribute (it's a
    # rate-limit-exhausted exception raised by discord.py's internal
    # backoff, not an HTTP response). Handle it before any
    # ``error.status`` access.
    if isinstance(error, discord.RateLimited):
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s: "
            "discord.py rate-limit backoff exhausted (%s); operator "
            "should investigate burst traffic patterns",
            wire.channel_id, wire.event_id, agent_id, error,
        )
        return

    # All remaining paths inspect ``error.status``; narrow the type so
    # the rest of the function can read it safely.
    if not isinstance(error, discord.HTTPException):
        # Defensive: a ``DiscordException`` that's neither
        # ``RateLimited`` nor ``HTTPException`` is something discord.py
        # added since we last looked. Log + drop rather than guess.
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s: "
            "unrecognized discord exception type=%s (%s)",
            wire.channel_id, wire.event_id, agent_id,
            type(error).__name__, error,
        )
        return

    if error.status in NON_AGENT_FIXABLE_STATUSES:
        if error.status == 404:
            logger.warning(
                "outbox post failed channel_id=%s event_id=%s agent=%s: "
                "channel or webhook not found (%s); operator must check "
                "the channel exists",
                wire.channel_id, wire.event_id, agent_id, error,
            )
        elif error.status == 403:
            logger.warning(
                "outbox post failed channel_id=%s event_id=%s agent=%s: "
                "forbidden (%s); operator must verify Manage Webhooks "
                "permission",
                wire.channel_id, wire.event_id, agent_id, error,
            )
        else:
            logger.warning(
                "outbox post failed (not retryable) channel_id=%s "
                "event_id=%s agent=%s status=%s: %s",
                wire.channel_id, wire.event_id, agent_id, error.status, error,
            )
        return

    # Branch 1b: 5xx survived _send_with_one_retry_on_outage's
    # smoothing. Discord is down; agent retry can't help.
    if error.status >= 500:
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s "
            "status=%s: discord 5xx + extra retry exhausted (%s)",
            wire.channel_id, wire.event_id, agent_id, error.status, error,
        )
        return

    retry_count = pending_wires.get_retry_count(wire.event_id)

    # Branch 2: retry budget exhausted → chunk-split fallback.
    if retry_count >= MAX_REPLY_RETRY_ATTEMPTS:
        logger.warning(
            "retry budget exhausted attempt=%d max=%d; chunk-splitting "
            "reply event_id=%s agent=%s status=%s: %s",
            retry_count, MAX_REPLY_RETRY_ATTEMPTS,
            wire.event_id, agent_id, error.status, error,
        )
        await _post_chunked_fallback(persona_sender, persona, wire, failed_text)
        return

    # Branch 3: agent retry. Claim the attempt atomically; the LRU
    # could in principle evict the entry mid-retry-sequence under
    # pathological load, in which case fall back to chunk-split.
    new_attempt = pending_wires.increment_retry(wire.event_id)
    if new_attempt is None:
        logger.warning(
            "pending entry evicted before retry could be claimed; "
            "chunk-splitting event_id=%s agent=%s",
            wire.event_id, agent_id,
        )
        await _post_chunked_fallback(persona_sender, persona, wire, failed_text)
        return

    logger.info(
        "outbox post failed; triggering agent retry attempt=%d "
        "channel_id=%s event_id=%s agent=%s status=%s: %s",
        new_attempt, wire.channel_id, wire.event_id, agent_id,
        error.status, error,
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
            "retry publish failed event_id=%s agent=%s; "
            "falling back to chunk-split",
            wire.event_id, agent_id,
        )
        await _post_chunked_fallback(persona_sender, persona, wire, failed_text)


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

    retry_history: list[ModelMessage] = [
        *entry.message_history,
        ModelRequest(parts=[UserPromptPart(content=wire.content)]),
        ModelResponse(parts=[TextPart(content=failed_text)]),
    ]

    # Rebuild the phonebook so any A2A-tool-using agent's tools still
    # work on retry. The outbox already has the registry; rebuilding
    # the phonebook is cheap (~one list-comprehension over the
    # registry's entries).
    phonebook = phonebook_from_registry(registry)

    handle = await client.invoke_node(
        user_prompt=reminder,
        topic=_AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id=agent_id),
        correlation_id=wire.event_id,
        deps={
            "discord": wire.model_dump(mode="json"),
            "phonebook": phonebook_to_deps(phonebook),
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
                wire.event_id, agent_id, exc_info=True,
            )


async def _post_chunked_fallback(
    persona_sender: DiscordPersonaSender,
    persona: Persona,
    wire: WireMessage,
    text: str,
) -> None:
    """Final fallback: split ``text`` into ≤2000-char chunks and post
    each as the same persona.

    The first chunk uses Discord's inline-reply anchor so the chain
    appears as a "reply to" the user's original question; subsequent
    chunks are bare continuations from the same persona, which
    Discord renders as natural follow-ups directly below the first.

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
    chunks = _chunk_split(text)
    if not chunks:
        logger.warning(
            "chunk-split fallback received empty text; nothing to post "
            "event_id=%s",
            wire.event_id,
        )
        return

    total = len(chunks)
    failure_statuses: list[int | None] = []
    for i, chunk in enumerate(chunks):
        reply_to = ReplyContext.from_wire(wire) if i == 0 else None
        try:
            sent = await persona_sender.send(
                persona=persona,
                channel_id=wire.channel_id,
                content=chunk,
                reply_to=reply_to,
            )
            logger.info(
                "chunk-split posted chunk %d/%d event_id=%s reply_id=%s "
                "channel_id=%s",
                i + 1, total, wire.event_id, sent.id, wire.channel_id,
            )
        except discord.DiscordException as e:
            status = getattr(e, "status", None)
            failure_statuses.append(status)
            logger.error(
                "chunk-split failed chunk %d/%d event_id=%s channel_id=%s "
                "status=%s: %s",
                i + 1, total, wire.event_id, wire.channel_id, status, e,
            )

    if failure_statuses and len(failure_statuses) == total:
        # All chunks failed. Surface a single summary signal so the
        # operator doesn't have to manually aggregate N ERROR lines.
        # Use the most-common status; ties broken by first occurrence.
        dominant_status = max(
            set(failure_statuses), key=failure_statuses.count
        )
        logger.warning(
            "chunk-split delivered 0/%d chunks event_id=%s channel_id=%s "
            "dominant_status=%s; reply is fully lost — operator should "
            "verify channel permissions and webhook health",
            total, wire.event_id, wire.channel_id, dominant_status,
        )
