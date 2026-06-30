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
from typing import Any, Protocol

from calfkit._vendor.pydantic_ai.messages import ModelMessage
from calfkit.exceptions import NodeFaultError

from calfcord.agents.thinking import build_model_settings_union
from calfcord.bridge.a2a_dispatch import A2ACall, A2ADispatcher, A2AProjection
from calfcord.bridge.persona_resolve import persona_for
from calfcord.bridge.step_events import StepEvent, normalize_run_event
from calfcord.discord.persona import Persona

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
    """

    content: str
    mention_ids: tuple[str, ...]
    author_label: str
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


class ReplyPoster(Protocol):
    async def post_reply(self, req: MentionRequest, persona: Persona, text: str) -> None: ...
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
        handle = await self._client.agent(target).start(
            req.content,
            message_history=history,
            deps=deps,
            author=req.author_label,
            model_settings=build_model_settings_union(self._overrides.effort_for(target)),
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

        await self._post_terminal(req, handle, dispatcher, target)

    async def _post_terminal(self, req: MentionRequest, handle: Any, dispatcher: A2ADispatcher, target: str) -> None:
        # No timeout: per spec §5.2 the bridge awaits the terminal unbounded (C5
        # drops app-side timeout policing; a durable run may legitimately pause).
        # A fault still surfaces — calfkit maps RunFailed → NodeFaultError here.
        try:
            result = await handle.result()
        except NodeFaultError as exc:
            # A genuine peer/agent fault faults the whole run (D-2): any consult
            # still open never got a reply, so synthesize an A2A failure note for
            # each, then post a user-facing error (best-effort persona from the
            # faulting node when the report names it).
            for call in dispatcher.dangling():
                await self._a2a.project_fault(call)
            origin = getattr(getattr(exc, "report", None), "origin_node_id", None)
            logger.warning("agent run faulted target=%s origin=%s", target, origin)
            await self._reply.post_notice(req, _agent_error_text(origin))
            return
        # Emitter-driven persona: the node that actually replied (after a handoff,
        # the peer) stamped the terminal, so this is handoff-correct with no
        # special casing.
        persona = persona_for(result.emitter_node_id or target)
        await self._reply.post_reply(req, persona, result.output)
