"""Post an agent's final reply to Discord under its persona (re-homed from outbox).

On the calfkit 0.12 caller surface the final reply is the return value of
``handle.result()`` (awaited by the :class:`~calfcord.bridge.mention_handler.MentionHandler`),
not a Kafka outbox message. This module re-homes the *posting* half of the old
``bridge/outbox.py`` — persona webhook send with 5xx smoothing, the step-transcript
expand toggle + durable transcript write, the chunk-split fallback — minus the
consumer / ``pending_wires`` / registry plumbing.

The retry-with-feedback *loop* lives in the handler (spec §9): :meth:`post_reply`
only attempts a single post and classifies the outcome (:class:`ReplyOutcome`)
so the handler can decide to re-invoke the agent, fall back to chunk-splitting,
or stop. ``post_notice`` posts a plain bridge-authored message (no persona) for
operator-facing notices (roster unavailable, no agent online, agent error).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

import discord
from calfkit._vendor.pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from calfcord.bridge.mention_handler import MentionRequest, ReplyOutcome
from calfcord.bridge.steps_render import _render_tree_blocks
from calfcord.bridge.steps_toggle import build_toggle_button
from calfcord.bridge.transcripts import TranscriptRow, TranscriptStoreLike
from calfcord.bridge.wire import WireMessage
from calfcord.discord.persona import DiscordPersonaSender, Persona, ReplyContext
from calfcord.discord.retry_feedback import chunk_split, classify_error

logger = logging.getLogger(__name__)

_SERVER_ERROR_RETRY_DELAY_SECONDS = 2.0
"""Delay before the one extra attempt on a first-try Discord 5xx (matches the
old outbox value; a single-worker poster can't afford a long sleep)."""

_OPERATOR_ACTIONABLE_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
"""Discord drop statuses that mean the bot is *misconfigured* — 401 (bad token)
and 403 (missing Manage Webhooks) — not transient. They silently break EVERY
reply, so they log at ERROR (surfacing in alerting); rate-limit / 404 / 5xx are
transient or environmental and stay at WARNING."""


def _turn_delta(result: Any, initial_len: int) -> list[ModelMessage]:
    """THIS turn's structured slice: the cumulative ``message_history`` minus the
    channel-history prefix (``initial_len``) and the trailing final-answer
    ``ModelResponse`` (``[initial_len:-1]``). Empty for a pure-text turn."""
    history = result.message_history
    if not history:
        return []
    return list(history[initial_len:-1])


def _render_step_count(delta: list[ModelMessage]) -> int:
    """Count renderable step blocks in ``delta``, defensively (a tool call + its
    result render as one block). ``_render_tree_blocks`` can raise on malformed
    args, so a failure degrades to zero steps — the reply still posts, just
    without the toggle or a transcript row."""
    try:
        return len(_render_tree_blocks(delta))
    except Exception:
        logger.exception("reply poster: step-count render raised; posting without toggle/transcript")
        return 0


class ReplyPoster:
    """Posts agent replies under their persona; classifies failures for the handler."""

    def __init__(self, persona_sender: DiscordPersonaSender, transcript_store: TranscriptStoreLike) -> None:
        self._personas = persona_sender
        self._store = transcript_store

    async def post_reply(
        self,
        req: MentionRequest,
        persona: Persona,
        result: Any,
        *,
        initial_len: int,
        correlation_id: str,
    ) -> ReplyOutcome:
        """Attempt a single post of ``result``'s final answer under ``persona``.

        Returns :class:`ReplyOutcome`: ``"ok"`` on success (or empty reply —
        nothing to post or retry); ``"dropped"`` for an infra failure the agent
        can't fix (auth/permission/rate-limit, persistent 5xx, or a non-Discord
        sender error); ``"retry"`` for a Discord rejection the agent can plausibly
        fix (carrying the error + the failed text for the handler's retry).
        """
        text = (result.output or "").strip()
        if not text:
            # Discord rejects an empty webhook send; there is nothing to post and
            # nothing the agent can fix, so treat it as a (no-op) success.
            return ReplyOutcome("ok")
        wire = req.wire  # already a validated WireMessage (built at the gateway)
        delta = _turn_delta(result, initial_len)
        rendered = _render_step_count(delta)
        write_transcript = bool(rendered) and self._store.enabled
        extra_buttons = [build_toggle_button(rendered)] if write_transcript else None
        try:
            sent = await _send_with_one_retry_on_outage(
                self._personas,
                persona=persona,
                channel_id=wire.channel_id,
                content=text,
                reply_to=ReplyContext.from_wire(wire),
                extra_buttons=extra_buttons,
                thread_id=wire.thread_id,
            )
        except discord.DiscordException as e:
            # ``DiscordException`` (not just ``HTTPException``) so RateLimited is
            # funneled to classify_error too (it treats it as a non-retryable drop).
            kind = classify_error(e)
            if kind == "agent_fixable":
                # classify_error only returns "agent_fixable" for an HTTPException
                # (a 4xx); assert it so the ReplyOutcome.error: HTTPException | None
                # contract the handler's build_retry_reminder relies on is explicit.
                assert isinstance(e, discord.HTTPException)
                return ReplyOutcome("retry", error=e, failed_text=text)
            # Auth/permission drops are operator-actionable misconfigurations that
            # silently break every reply -> ERROR (so alerting sees them); rate-limit
            # / 5xx are transient -> WARNING. Either way the handler surfaces an
            # operator notice via the native-reply path.
            status = getattr(e, "status", None)
            log = logger.error if status in _OPERATOR_ACTIONABLE_STATUSES else logger.warning
            log(
                "reply post failed channel_id=%s correlation_id=%s: %s reply (status=%s); dropping",
                wire.channel_id,
                correlation_id,
                kind,
                status,
                exc_info=True,
            )
            return ReplyOutcome("dropped")
        except (TypeError, RuntimeError) as e:
            # Non-Discord, operator-actionable sender errors (non-text channel /
            # sender not started) — not agent-fixable or transient. Keep the stack
            # (exc_info) so an unexpected RuntimeError source is diagnosable.
            logger.error(
                "reply post failed channel_id=%s correlation_id=%s: non-retryable sender error %s (%s); dropping",
                wire.channel_id,
                correlation_id,
                type(e).__name__,
                e,
                exc_info=True,
            )
            return ReplyOutcome("dropped")
        if write_transcript:
            await _write_transcript(
                self._store,
                correlation_id=correlation_id,
                wire=wire,
                agent_id=persona.name,
                final_message_id=sent.id,
                delta=delta,
            )
        return ReplyOutcome("ok")

    async def post_chunked(
        self,
        req: MentionRequest,
        persona: Persona,
        result: Any,
        *,
        initial_len: int,
        correlation_id: str,
    ) -> bool:
        """Final fallback (retries exhausted): split the reply into ≤2000-char
        chunks and post each under ``persona``. The first chunk carries the
        inline-reply anchor + (if the turn used tools) the expand toggle and the
        transcript row; later chunks are bare continuations. Per-chunk failures
        are logged independently so partial delivery survives.

        Returns ``True`` if at least one chunk posted (or there was nothing to
        post), ``False`` if the reply was fully lost (every chunk failed) so the
        handler can surface an operator notice rather than ghost the user."""
        wire = req.wire  # already a validated WireMessage (built at the gateway)
        text = (result.output or "").strip()
        chunks = chunk_split(text)
        if not chunks:
            # Nothing to deliver (empty reply); not a loss to surface — mirrors
            # post_reply treating an empty reply as a no-op success.
            logger.warning("chunk-split fallback received empty text correlation_id=%s", correlation_id)
            return True
        delta = _turn_delta(result, initial_len)
        rendered = _render_step_count(delta)
        write_transcript = bool(rendered) and self._store.enabled
        first_chunk_buttons = [build_toggle_button(rendered)] if write_transcript else None
        total = len(chunks)
        failures: list[int | None] = []
        for i, chunk in enumerate(chunks):
            try:
                sent = await self._personas.send(
                    persona=persona,
                    channel_id=wire.channel_id,
                    content=chunk,
                    reply_to=ReplyContext.from_wire(wire) if i == 0 else None,
                    extra_buttons=first_chunk_buttons if i == 0 else None,
                    thread_id=wire.thread_id,
                )
                if i == 0 and write_transcript:
                    await _write_transcript(
                        self._store,
                        correlation_id=correlation_id,
                        wire=wire,
                        agent_id=persona.name,
                        final_message_id=sent.id,
                        delta=delta,
                    )
            except (discord.DiscordException, TypeError, RuntimeError) as e:
                failures.append(getattr(e, "status", None))
                logger.error(
                    "chunk-split failed chunk %d/%d correlation_id=%s status=%s: %s",
                    i + 1,
                    total,
                    correlation_id,
                    getattr(e, "status", None),
                    e,
                )
        if failures and len(failures) == total:
            dominant = max(set(failures), key=failures.count)
            logger.error(
                "chunk-split delivered 0/%d chunks correlation_id=%s dominant_status=%s; reply fully lost",
                total,
                correlation_id,
                dominant,
            )
            return False
        return True

    async def post_notice(self, req: MentionRequest, text: str) -> None:
        """Post a plain operator-facing notice as an inline reply (no persona).

        Notices (roster unavailable, no agent online, agent error) are bridge-
        authored, not agent output, so they go via the triggering message's native
        inline reply rather than a persona webhook. Best-effort: a failure to post
        a notice must not escape into the handler."""
        try:
            await req.reply_target.reply(text)
        except discord.DiscordException:
            logger.warning("failed to post notice to channel_id=%s", req.channel_id, exc_info=True)


async def _send_with_one_retry_on_outage(
    persona_sender: DiscordPersonaSender,
    *,
    persona: Persona,
    channel_id: int,
    content: str,
    reply_to: ReplyContext | None,
    extra_buttons: list[Any] | None = None,
    thread_id: int | None = None,
) -> Any:
    """Send via the persona webhook with exactly one extra attempt on a first-try
    5xx (after a short delay). Any other first-try error, or a second-try error,
    is re-raised for the caller to triage."""
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
    await asyncio.sleep(_SERVER_ERROR_RETRY_DELAY_SECONDS)
    return await persona_sender.send(
        persona=persona,
        channel_id=channel_id,
        content=content,
        reply_to=reply_to,
        extra_buttons=extra_buttons,
        thread_id=thread_id,
    )


async def _write_transcript(
    transcript_store: TranscriptStoreLike,
    *,
    correlation_id: str,
    wire: WireMessage,
    agent_id: str,
    final_message_id: int,
    delta: list[ModelMessage],
) -> None:
    """Persist the completed turn's transcript row (best-effort, idempotent on
    ``correlation_id``). A store failure is logged and swallowed — the reply is
    already posted; the toggle simply finds no row (documented degradation)."""
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
        logger.exception(
            "failed to write transcript correlation_id=%s reply_id=%s; step toggle will have no row",
            correlation_id,
            final_message_id,
        )
