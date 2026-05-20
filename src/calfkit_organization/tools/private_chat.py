"""``private_chat`` — A2A tool for agent-to-agent communication over calfkit.

Routes a request from one agent to another via calfkit RPC and projects
the exchange to a deterministic ``a2a-{x}-{y}`` Discord channel for human
audit. Kafka is the system of record; Discord is the projection.

Flow per invocation:
    1. Validate caller and target identities against the agent registry.
    2. Build a *forwarded* :class:`WireMessage` — a shallow clone of the
       caller's originating wire with ``slash_target=target_agent_id``,
       ``kind="slash"``, and the new ``content``. This makes the target's
       existing ``addressed_to_me`` gate accept the call without any gate
       changes.
    3. Resolve (or lazily create) the pair's
       ``a2a-{x}-{y}`` channel via :class:`A2AChannelResolver`.
    4. Post the request projection as the caller's persona (best-effort).
    5. ``Client.execute_node`` against ``agent.{target}.in`` with deps
       ``{"discord": forwarded_wire, "caller_agent_id": <caller>}``.
       Default 60s timeout — fail-fast on no consumer or timeout.
    6. Post the response projection as the target's persona (best-effort).
    7. Return the target's text output to the caller's LLM.

The module exposes both the bare async function ``private_chat`` (so
tests can call it directly without going through calfkit's dispatch) and
``private_chat_tool``, the :class:`ToolNodeDef` produced by ``agent_tool``
that the registry and ``calfkit-tools`` runner wire up.

Runtime dependencies (calfkit client, persona sender, channel resolver,
agent registry) are injected via :func:`init` at process startup. The
module-level singletons are populated only in the ``calfkit-tools``
runner — agent processes import this module solely for the
``ToolNodeDef`` schema and never call the function body, so the unset
state is benign there.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from calfkit.client import Client
from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool

from calfkit_organization.agents.peer_roster import build_temp_instructions
from calfkit_organization.bridge.egress import A2AChannelResolver
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireMessage
from calfkit_organization.discord.persona import DiscordPersonaSender, Persona

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
"""Default per-call timeout for the target's response. Overridable via
:func:`init`. Fail-fast on timeout per A2A design — the caller's LLM can
adapt or re-issue."""

_AGENT_INBOX_TOPIC_TEMPLATE = "agent.{agent_id}.in"
"""Must match the template the agent factory subscribes on
(``calfkit_organization.agents.factory._AGENT_INBOX_TOPIC_TEMPLATE``).
Duplicated here rather than imported to keep the tool free of an
agent-side dependency."""

# Module-level injected singletons. Populated only by the calfkit-tools
# runner's startup via init(). Tests overwrite via monkeypatch.
_client: Client | None = None
_persona_sender: DiscordPersonaSender | None = None
_resolver: A2AChannelResolver | None = None
_registry: AgentRegistry | None = None
_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def init(
    *,
    client: Client,
    persona_sender: DiscordPersonaSender,
    resolver: A2AChannelResolver,
    registry: AgentRegistry,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Inject the runtime dependencies the tool body uses.

    Called exactly once at ``calfkit-tools`` startup before the Worker
    starts consuming. Calling again replaces the singletons — useful for
    tests, surprising in production. Not thread-safe; assumes a single
    asyncio event loop, which is what calfkit's Worker provides.
    """
    global _client, _persona_sender, _resolver, _registry, _timeout_seconds
    _client = client
    _persona_sender = persona_sender
    _resolver = resolver
    _registry = registry
    _timeout_seconds = timeout_seconds


async def private_chat(
    ctx: ToolContext,
    target_agent_id: str,
    content: str,
) -> str:
    """Send a private message to another agent and get their reply.

    Use this to collaborate one-on-one with a peer agent — delegate a
    sub-task, ask a focused question, or get input only they can
    provide. The exchange is also posted to a shared audit channel so
    userss observing the organization can see agents collaborate.

    The peer receives ``content`` as a fresh user prompt and does not
    see your prior conversations. Make the message self-contained:
    include any context, constraints, or examples the peer needs to
    answer well.

    Args:
        target_agent_id: The peer agent's id (e.g. ``"scribe"``). Must
            be a registered agent and must not be your own id.
        content: The message text to send to the peer.

    Returns:
        The peer's text reply. May be empty if the peer produced no output.

        On operational issues the tool returns an error message
        starting with ``error:`` so you can adapt (retry, pick a
        different agent, or fall back). Possible errors:

        * ``error: unknown agent '<name>'; known agents: ...`` —
          ``target_agent_id`` is not registered. Fix the id and retry.
        * ``error: agent '<self>' cannot privately chat with itself`` —
          pick a different agent.
        * ``error: target '<name>' did not reply within Ns`` — the peer
          did not respond in time. Try again or pick another agent.
    """
    if _client is None or _persona_sender is None or _resolver is None or _registry is None:
        raise RuntimeError("private_chat tool not initialized; the calfkit-tools runner must call init() at startup")

    caller_agent_id = ctx.agent_name
    if caller_agent_id is None:
        # ctx.agent_name is set from the inbound x-calf-emitter header.
        # If it's missing, calfkit's dispatch was bypassed somehow.
        raise RuntimeError("private_chat invoked without emitter_node_id; cannot identify caller")

    if caller_agent_id == target_agent_id:
        return f"error: agent {caller_agent_id!r} cannot privately chat with itself"

    target_spec = _registry.by_id(target_agent_id)
    if target_spec is None:
        known = ", ".join(sorted(s.agent_id for s in _registry.all()))
        return f"error: unknown agent {target_agent_id!r}; known agents: {known}"

    caller_spec = _registry.by_id(caller_agent_id)
    if caller_spec is None:
        # Caller is supposed to be a registered agent (only they can invoke
        # tools). A missing registry entry is an infrastructure bug, not LLM
        # input — raise so it surfaces in logs rather than silently degrading
        # the projection to no-persona.
        raise RuntimeError(f"caller {caller_agent_id!r} is not in the registry; cannot resolve persona")

    incoming_wire_dict = ctx.deps.provided_deps.get("discord")
    if not isinstance(incoming_wire_dict, dict):
        raise RuntimeError(
            "private_chat invoked without deps['discord']; the bridge ingress is "
            "expected to populate this key before any agent runs"
        )
    incoming_wire = WireMessage.model_validate(incoming_wire_dict)

    # Forward a mutated wire to the target: slash_target points at them
    # (so addressed_to_me_gate accepts), kind=slash (so the gate's slash
    # branch is taken), content is the A2A payload. Channel, original
    # author, and message_id stay as the original Discord context — the
    # caller_agent_id key is the canonical "who actually invoked this"
    # signal for downstream code.
    forwarded_wire = incoming_wire.model_copy(
        update={
            "slash_target": target_agent_id,
            "kind": "slash",
            "content": content,
        }
    )

    # Channel resolution is NOT best-effort: without an audit channel we
    # don't even have a place to put projections, and the auditing
    # invariant is part of the design intent. If this fails (e.g. the
    # bot lacks Manage Channels), the operator must see it.
    a2a_channel_id = await _resolver.resolve_or_create(caller_agent_id, target_agent_id)

    caller_persona = Persona(name=caller_spec.display_name, avatar_url=caller_spec.avatar_url)
    target_persona = Persona(name=target_spec.display_name, avatar_url=target_spec.avatar_url)

    await _post_projection(
        caller_persona,
        a2a_channel_id,
        content,
        caller=caller_agent_id,
        target=target_agent_id,
        correlation_id=None,
    )

    target_topic = _AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id=target_agent_id)
    logger.info(
        "private_chat invoking caller=%s target=%s topic=%s timeout=%.1fs",
        caller_agent_id,
        target_agent_id,
        target_topic,
        _timeout_seconds,
    )
    try:
        result = await _client.execute_node(
            user_prompt=content,
            topic=target_topic,
            deps={
                "discord": forwarded_wire.model_dump(mode="json"),
                "caller_agent_id": caller_agent_id,
            },
            output_type=str,
            timeout=_timeout_seconds,
            temp_instructions=build_temp_instructions(_registry, target_agent_id),
        )
    except asyncio.TimeoutError:
        # Returning as a string (rather than raising) is deliberate: if we
        # raise, the tool's ReturnCall never fires and the calling agent's
        # execute_node also times out — two timeouts for one logical
        # failure. An error string lets the LLM see the failure and adapt.
        logger.warning(
            "private_chat timeout caller=%s target=%s topic=%s timeout=%.1fs",
            caller_agent_id,
            target_agent_id,
            target_topic,
            _timeout_seconds,
        )
        return f"error: target {target_agent_id!r} did not reply within {_timeout_seconds:.0f}s"
    response_text = result.output if result.output is not None else ""

    await _post_projection(
        target_persona,
        a2a_channel_id,
        response_text,
        caller=caller_agent_id,
        target=target_agent_id,
        correlation_id=result.correlation_id,
    )

    logger.info(
        "private_chat completed caller=%s target=%s correlation_id=%s response_len=%d",
        caller_agent_id,
        target_agent_id,
        result.correlation_id,
        len(response_text),
    )
    return response_text


async def _post_projection(
    persona: Persona,
    channel_id: int,
    content: str,
    *,
    caller: str,
    target: str,
    correlation_id: str | None,
) -> None:
    """Post a projection message; retry once on Discord errors, then log + accept the gap.

    Projections are an audit trail. The Kafka exchange is the system of
    record, so a transient Discord failure must never abort the A2A turn.

    Exception scope: only :class:`discord.DiscordException` (the library's
    own family) is caught — that covers HTTP errors, rate-limits, gateway
    issues, etc. ``RuntimeError`` (sender not started) and ``TypeError``
    (channel id not a text channel) escape because they indicate the
    projection sub-system is misconfigured, not transiently down — those
    deserve to surface as the originating bug rather than be swallowed.

    ``caller``/``target``/``correlation_id`` are logged on failure so an
    operator finding an audit gap on a particular pair channel can
    correlate it back to the specific A2A turn.
    """
    assert _persona_sender is not None  # guarded by the caller
    # Empty content is legal (some agents may legitimately reply ""), but
    # Discord rejects it. Substitute a visible placeholder so the audit log
    # makes sense rather than silently dropping the projection entry.
    payload = content if content else "(empty response)"
    for attempt in (1, 2):
        try:
            await _persona_sender.send(persona, channel_id=channel_id, content=payload)
            return
        except discord.DiscordException:
            if attempt < 2:
                logger.warning(
                    "projection attempt=%d failed persona=%s channel=%s caller=%s target=%s correlation_id=%s; retrying",
                    attempt,
                    persona.name,
                    channel_id,
                    caller,
                    target,
                    correlation_id,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "projection failed persona=%s channel=%s caller=%s target=%s correlation_id=%s; accepting audit gap",
                    persona.name,
                    channel_id,
                    caller,
                    target,
                    correlation_id,
                    exc_info=True,
                )


# Calfkit's ``@agent_tool`` decorator wraps the bare async function in a
# ``ToolNodeDef`` whose subscribe/publish topics derive from the function
# name (``tool.private_chat.input`` / ``tool.private_chat.output``).
# Applied as a regular call rather than the ``@agent_tool`` decorator
# form so the bare function above stays directly importable (and unit-
# testable) under its real name.
private_chat_tool: ToolNodeDef = agent_tool(private_chat)
