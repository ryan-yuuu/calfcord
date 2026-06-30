"""The bridge's per-``@mention`` orchestration (spec §5.2).

Replaces the old publish-to-Kafka → outbox-consumer round trip with the calfkit
caller surface. For each ``@mention`` the handler:

1. resolves the target against the live mesh roster (R-A2 fail-fast);
2. starts the agent by name on the caller surface (``client.agent(name).start``);
3. drains the run's ``stream()`` — splitting native A2A activity (consults +
   handoffs) from live progress via the stateful :class:`A2ADispatcher`;
4. awaits the terminal ``result()`` and posts it under the **responding** agent's
   persona (emitter-driven, so a handoff posts the peer's persona for free).

The collaborators (history, overrides, the A2A projector, the progress renderer,
the reply poster) are injected so this orchestration is unit-testable against a
``FakeHandle`` with no Kafka or Discord.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from calfkit._vendor.pydantic_ai.messages import ModelMessage
from calfkit.exceptions import NodeFaultError

from calfcord.agents.thinking import build_model_settings_union
from calfcord.bridge.a2a_dispatch import A2ACall, A2ADispatcher, A2AProjection
from calfcord.bridge.persona_resolve import persona_for
from calfcord.bridge.step_events import StepEvent, normalize_run_event
from calfcord.discord.persona import Persona
from calfcord.discord.retry_feedback import (
    MAX_REPLY_RETRY_ATTEMPTS,
    build_retry_history,
    build_retry_reminder,
)

logger = logging.getLogger(__name__)

_ROSTER_UNAVAILABLE = "I can't reach the agent roster right now — please try again in a moment."


def _none_online_text(mention_ids: tuple[str, ...]) -> str:
    names = ", ".join(f"`@{m}`" for m in mention_ids)
    return f"No agent matching {names} is online right now."


def _agent_error_text(origin: str | None) -> str:
    who = f"`{origin}`" if origin else "The agent"
    return f"{who} hit an error handling that message. Please try again."


@dataclass(frozen=True)
class MentionRequest:
    """A normalized inbound ``@mention`` — what the Discord gateway hands the
    handler.

    ``mention_ids`` are the parsed ``@<id>`` tokens in order; ``wire`` is the raw
    Discord context the agent reads off ``deps["discord"]``; ``reply_target`` is
    the opaque discord.py object the reply / notice posts against.

    ``message_id`` is the triggering Discord message id — the history-fetch anchor
    (``before=``) and the transcript-replay join key. ``source_channel_id`` is the
    un-flattened channel the message landed in (the thread itself, for history
    fetching); ``channel_id`` is the flattened parent (the webhook host).
    """

    content: str
    mention_ids: tuple[str, ...]
    author_label: str
    message_id: int
    source_channel_id: int
    channel_id: int
    wire: dict[str, Any]
    reply_target: Any


class HistoryProvider(Protocol):
    async def message_history(self, req: MentionRequest) -> list[ModelMessage]: ...


class OverrideProvider(Protocol):
    def effort_for(self, agent_id: str) -> str | None: ...


class A2AProjectorLike(Protocol):
    async def project(self, projection: A2AProjection) -> None: ...
    async def project_fault(self, call: A2ACall) -> None: ...


class ProgressRenderer(Protocol):
    async def on_step(self, step: StepEvent, req: MentionRequest) -> None: ...
    async def finish(self, correlation_id: str) -> None: ...


@dataclass(frozen=True)
class ReplyOutcome:
    """The result of a reply-post attempt — drives the retry-with-feedback loop.

    ``"ok"`` posted (or the reply was empty and dropped — nothing to retry);
    ``"dropped"`` an infra failure the agent can't fix (auth/permission/rate-limit
    or a persistent 5xx — logged + abandoned, no retry); ``"retry"`` a Discord
    rejection the agent can plausibly fix (e.g. too long), carrying the rejecting
    ``error`` and the ``failed_text`` for the corrective retry envelope.
    """

    status: Literal["ok", "dropped", "retry"]
    error: Any = None
    failed_text: str = ""


class ReplyPoster(Protocol):
    async def post_reply(
        self, req: MentionRequest, persona: Persona, result: Any, *, initial_len: int, correlation_id: str
    ) -> ReplyOutcome: ...
    async def post_chunked(
        self, req: MentionRequest, persona: Persona, result: Any, *, initial_len: int, correlation_id: str
    ) -> None: ...
    async def post_notice(self, req: MentionRequest, text: str) -> None: ...


class MentionHandler:
    """Orchestrates one ``@mention`` end to end on the caller surface."""

    def __init__(
        self,
        *,
        client: Any,
        roster: Any,
        history: HistoryProvider,
        overrides: OverrideProvider,
        a2a: A2AProjectorLike,
        progress: ProgressRenderer,
        reply: ReplyPoster,
        memory_deps: Any = dict,
    ) -> None:
        self._client = client
        self._roster = roster
        self._history = history
        self._overrides = overrides
        self._a2a = a2a
        self._progress = progress
        self._reply = reply
        self._memory_deps = memory_deps

    async def handle(self, req: MentionRequest) -> None:
        online = self._roster.online()
        if online is None:
            # Mesh unavailable — we cannot tell who is online, so fail fast
            # rather than route blindly (R-A2). reader_dead stays here until the
            # bridge restarts; the roster already alerted.
            await self._reply.post_notice(req, _ROSTER_UNAVAILABLE)
            return
        target = next((m for m in req.mention_ids if m in online), None)
        if target is None:
            if req.mention_ids:
                # Mentioned an agent that is not online right now.
                await self._reply.post_notice(req, _none_online_text(req.mention_ids))
            # else: no @mention at all → ambient → unanswered (C2): do nothing.
            return

        history = await self._history.message_history(req)
        deps = {"discord": req.wire, **self._memory_deps()}
        # Compute the C11 effort override once and reuse it on every retry.
        model_settings = build_model_settings_union(self._overrides.effort_for(target))
        handle = await self._client.agent(target).start(
            req.content,
            message_history=history,
            deps=deps,
            author=req.author_label,
            model_settings=model_settings,
        )

        dispatcher = A2ADispatcher()
        try:
            async for event in handle.stream():
                step = normalize_run_event(event)
                if step is None:
                    continue  # terminal — handled by result() below
                projection = dispatcher.classify(step)
                if projection is not None:
                    await self._a2a.project(projection)
                else:
                    await self._progress.on_step(step, req)
        finally:
            await self._progress.finish(handle.correlation_id)

        await self._deliver(req, handle, dispatcher, target, history, deps, model_settings)

    async def _deliver(
        self,
        req: MentionRequest,
        handle: Any,
        dispatcher: A2ADispatcher,
        target: str,
        history: list[ModelMessage],
        deps: dict[str, Any],
        model_settings: dict[str, Any] | None,
    ) -> None:
        """Post the agent's reply, with retry-with-feedback (spec §9).

        Re-homes the old outbox re-publish as an in-process loop: when Discord
        rejects a reply for a reason the agent can fix (e.g. too long), re-invoke
        the agent with a corrective ``<system-reminder>`` + the failed attempt in
        ``message_history`` (same ``deps``/``author``/``model_settings``), bounded
        by ``MAX_REPLY_RETRY_ATTEMPTS``; on exhaustion fall back to chunk-splitting
        the last attempt. The retry re-invocation is "quiet" — it awaits
        ``result()`` only (no second progress/A2A drain), matching the old
        blocking-RPC retry. The original run's ``correlation_id`` keys the
        transcript across attempts so retries upsert one row.
        """
        result = await self._await_terminal(req, handle, dispatcher, target)
        if result is None:
            return  # faulted — notice already posted

        attempt_history = history
        attempts = 0
        while True:
            persona = persona_for(result.emitter_node_id or target)
            outcome = await self._reply.post_reply(
                req, persona, result, initial_len=len(attempt_history), correlation_id=handle.correlation_id
            )
            if outcome.status != "retry":
                return  # "ok" or "dropped" — nothing more to do
            if attempts >= MAX_REPLY_RETRY_ATTEMPTS:
                # Budget exhausted: post the last attempt chunk-split rather than
                # losing the reply entirely.
                await self._reply.post_chunked(
                    req, persona, result, initial_len=len(attempt_history), correlation_id=handle.correlation_id
                )
                return
            attempts += 1
            retry_history = build_retry_history(
                original_history=attempt_history,
                original_user_prompt=req.content,
                failed_text=outcome.failed_text,
            )
            reminder = build_retry_reminder(outcome.error, outcome.failed_text)
            try:
                retry_handle = await self._client.agent(target).start(
                    reminder,
                    message_history=retry_history,
                    deps=deps,
                    author=req.author_label,
                    model_settings=model_settings,
                )
                result = await retry_handle.result()
            except NodeFaultError as exc:
                origin = getattr(getattr(exc, "report", None), "origin_node_id", None)
                logger.warning("agent retry faulted target=%s origin=%s", target, origin)
                await self._reply.post_notice(req, _agent_error_text(origin))
                return
            attempt_history = retry_history

    async def _await_terminal(
        self, req: MentionRequest, handle: Any, dispatcher: A2ADispatcher, target: str
    ) -> Any | None:
        """Await the run's terminal, or ``None`` after handling a fault.

        No timeout: per spec §5.2 the bridge awaits the terminal unbounded (C5
        drops app-side timeout policing; a durable run may legitimately pause). A
        genuine peer/agent fault faults the whole run (D-2) — calfkit maps
        ``RunFailed`` → :class:`NodeFaultError`; any consult still open never got a
        reply, so synthesize an A2A failure note for each, then post a user-facing
        error (best-effort persona from the faulting node when the report names it).
        """
        try:
            return await handle.result()
        except NodeFaultError as exc:
            for call in dispatcher.dangling():
                await self._a2a.project_fault(call)
            origin = getattr(getattr(exc, "report", None), "origin_node_id", None)
            logger.warning("agent run faulted target=%s origin=%s", target, origin)
            await self._reply.post_notice(req, _agent_error_text(origin))
            return None
