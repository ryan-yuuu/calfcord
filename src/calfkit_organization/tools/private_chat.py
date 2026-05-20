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
    4. Post the request projection as the caller's persona (best-effort —
       persistent transient failures are logged and the RPC still runs).
    5. ``Client.execute_node`` against ``agent.{target}.in`` with deps
       ``{"discord": forwarded_wire, "caller_agent_id": <caller>}``.
       Default 60s timeout — fail-fast on no consumer or timeout.
    6. Post the response projection as the target's persona. Unlike the
       request projection this is **not** best-effort: persistent failure
       raises ``RuntimeError`` so the calling LLM never sees a reply that
       wasn't projected to humans.
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
from pydantic import ValidationError

from calfkit_organization.agents.peer_roster import build_temp_instructions
from calfkit_organization.agents.phonebook import PhonebookEntry, phonebook_from_deps, phonebook_to_deps
from calfkit_organization.bridge.egress import A2AChannelResolver
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

_MAX_PROJECTION_ATTEMPTS = 2
"""Total attempts (initial + retries) for a single projection post.
``discord.py`` already does its own 5 internal retries with escalating
sleep for 429/5xx (see
``discord.webhook.async_.AsyncWebhookAdapter.request``); our second pass
is best-effort cleanup if that budget gets exhausted by a longer-than-
usual burst. More than two attempts here would stall the tool's worker
without improving the long-tail recovery odds."""

_PROJECTION_RETRY_DELAY_SECONDS = 2.0
"""Backoff between projection attempts. Mirrors
:data:`calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS`
— same shape of failure (discord.py's internal budget exhausted), same
constraint (a single-worker consumer can't afford a long sleep)."""

# Module-level injected singletons. Populated only by the calfkit-tools
# runner's startup via init(). Tests overwrite via monkeypatch.
#
# Note the absence of any AgentRegistry: the tool's deployment is decoupled
# from the bridge and cannot read agents/*.md. The bridge passes the
# canonical roster snapshot in deps["phonebook"] on every invocation;
# the tool reads it per-call from ctx.deps.provided_deps.
_client: Client | None = None
_persona_sender: DiscordPersonaSender | None = None
_resolver: A2AChannelResolver | None = None
_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def init(
    *,
    client: Client,
    persona_sender: DiscordPersonaSender,
    resolver: A2AChannelResolver,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Inject the runtime dependencies the tool body uses.

    Called exactly once at ``calfkit-tools`` startup before the Worker
    starts consuming. Calling again replaces the singletons — useful for
    tests, surprising in production. Not thread-safe; assumes a single
    asyncio event loop, which is what calfkit's Worker provides.
    """
    global _client, _persona_sender, _resolver, _timeout_seconds
    _client = client
    _persona_sender = persona_sender
    _resolver = resolver
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
    correlation_id = ctx.deps.correlation_id
    if _client is None or _persona_sender is None or _resolver is None:
        _raise_infra("tool not initialized; the calfkit-tools runner must call init() at startup",
                     correlation_id=correlation_id)

    caller_agent_id = ctx.agent_name
    if caller_agent_id is None:
        # ctx.agent_name is set from the inbound x-calf-emitter header.
        # If it's missing, calfkit's dispatch was bypassed somehow.
        _raise_infra("invoked without emitter_node_id; cannot identify caller",
                     correlation_id=correlation_id)

    if caller_agent_id == target_agent_id:
        # Log at INFO so operators tailing the tools log can spot LLMs that
        # repeatedly try invalid targets (a sign of a bad system prompt or a
        # hallucinating model). The LLM still sees the error string.
        logger.info(
            "private_chat returning recoverable error caller=%s target=%s reason=self-target correlation_id=%s",
            caller_agent_id,
            target_agent_id,
            correlation_id,
        )
        return f"error: agent {caller_agent_id!r} cannot privately chat with itself"

    # Parse the phonebook out of deps. The bridge ingress populates this
    # on every invocation; the agent's dispatch propagates it into the
    # tool's ctx unchanged. The tool's deployment cannot itself read
    # agents/*.md — this is the only source of identity information we
    # have. A missing OR malformed phonebook is normalized to RuntimeError
    # so all infra-bug signals are one exception type (the contract).
    phonebook_raw = ctx.deps.provided_deps.get("phonebook")
    if phonebook_raw is None:
        _raise_infra("invoked without deps['phonebook']; the bridge ingress is expected to populate this key on every publish",
                     correlation_id=correlation_id, caller=caller_agent_id, target=target_agent_id)
    try:
        phonebook = phonebook_from_deps(phonebook_raw)
    except (ValueError, ValidationError) as e:
        _raise_infra(f"received malformed deps['phonebook']: {e}",
                     correlation_id=correlation_id, caller=caller_agent_id, target=target_agent_id, cause=e)

    target_entry = _lookup(phonebook, target_agent_id)
    if target_entry is None:
        known = ", ".join(sorted(e.agent_id for e in phonebook))
        logger.info(
            "private_chat returning recoverable error caller=%s target=%s reason=unknown-target correlation_id=%s",
            caller_agent_id,
            target_agent_id,
            correlation_id,
        )
        return f"error: unknown agent {target_agent_id!r}; known agents: {known}"

    caller_entry = _lookup(phonebook, caller_agent_id)
    if caller_entry is None:
        # Caller is supposed to be a registered agent (only they can invoke
        # tools). A missing phonebook entry is an infrastructure bug, not
        # LLM input — raise so it surfaces in logs rather than silently
        # degrading the projection to no-persona.
        _raise_infra(f"caller {caller_agent_id!r} is not in the phonebook; cannot resolve persona",
                     correlation_id=correlation_id, caller=caller_agent_id, target=target_agent_id)

    incoming_wire_dict = ctx.deps.provided_deps.get("discord")
    if not isinstance(incoming_wire_dict, dict):
        _raise_infra("invoked without deps['discord']; the bridge ingress is expected to populate this key before any agent runs",
                     correlation_id=correlation_id, caller=caller_agent_id, target=target_agent_id)
    try:
        incoming_wire = WireMessage.model_validate(incoming_wire_dict)
    except ValidationError as e:
        _raise_infra(f"received malformed deps['discord']: {e}",
                     correlation_id=correlation_id, caller=caller_agent_id, target=target_agent_id, cause=e)

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
    # bot lacks Manage Channels), the operator must see it — log with
    # caller/target context at this layer because the resolver only
    # logs the success path.
    try:
        a2a_channel_id = await _resolver.resolve_or_create(caller_agent_id, target_agent_id)
    except discord.DiscordException:
        logger.error(
            "a2a channel resolution failed caller=%s target=%s correlation_id=%s",
            caller_agent_id,
            target_agent_id,
            correlation_id,
            exc_info=True,
        )
        raise

    caller_persona = Persona(name=caller_entry.display_name, avatar_url=caller_entry.avatar_url)
    target_persona = Persona(name=target_entry.display_name, avatar_url=target_entry.avatar_url)

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
                # Propagate the phonebook so the target (if it chains into
                # another private_chat) sees the same roster we did.
                "phonebook": phonebook_to_deps(phonebook),
            },
            output_type=str,
            timeout=_timeout_seconds,
            temp_instructions=build_temp_instructions(phonebook, target_agent_id),
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
    except Exception as e:
        # Catch ``Exception`` (not ``BaseException``) so ``asyncio.CancelledError``
        # and ``KeyboardInterrupt`` — both ``BaseException`` subclasses in
        # 3.11 — propagate untouched. Everything else (calfkit
        # ``DeserializationError``, pydantic ``ValidationError``, broker
        # ``ConnectionError``, FastStream errors, etc.) is an infra failure
        # that must funnel through ``_raise_infra`` so operators get the
        # caller/target/correlation_id at the call site rather than at
        # calfkit's runtime handler (which has no domain context). Keeps the
        # documented contract — infra failures raise ``RuntimeError`` — uniform.
        _raise_infra(
            f"execute_node failed against {target_topic!r}: {e}",
            caller=caller_agent_id,
            target=target_agent_id,
            correlation_id=correlation_id,
            cause=e,
        )
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


def _lookup(
    phonebook: list[PhonebookEntry], agent_id: str
) -> PhonebookEntry | None:
    """Find an entry by id. Returns ``None`` if not present."""
    return next((e for e in phonebook if e.agent_id == agent_id), None)


def _raise_infra(
    message: str,
    *,
    correlation_id: str,
    caller: str | None = None,
    target: str | None = None,
    cause: Exception | None = None,
) -> None:
    """Log infra context at ERROR and raise ``RuntimeError``.

    Every infrastructure-bug signal in ``private_chat`` funnels through
    here so operators get the caller/target/correlation_id context at
    the call site (the natural place to grep) rather than at calfkit's
    runtime handler (which has no domain context). ``RuntimeError`` is
    the documented infra-bug exception type — callers upstream rely on
    this to distinguish infra bugs from LLM-recoverable errors (which
    are returned as ``"error: ..."`` strings instead of raised).

    Always raises; the ``None`` return annotation is a lie pytest-style
    to keep callers from needing dead-code returns after the call. The
    NoReturn annotation would be more honest but adds an import for
    little benefit at this scale.
    """
    logger.error(
        "private_chat infra error: %s caller=%s target=%s correlation_id=%s",
        message,
        caller,
        target,
        correlation_id,
        exc_info=cause is not None,
    )
    full_message = f"private_chat {message}"
    if cause is not None:
        raise RuntimeError(full_message) from cause
    raise RuntimeError(full_message)


async def _post_projection(
    persona: Persona,
    channel_id: int,
    content: str,
    *,
    caller: str,
    target: str,
    correlation_id: str | None,
) -> None:
    """Post a projection message with bounded retry; final-failure handling
    depends on whether this is the request or response side.

    Projections are an audit trail. The Kafka exchange is the system of
    record, so a transient Discord failure must never abort the *request*
    side of an A2A turn — losing that projection at worst leaves a one-
    sided audit entry, while raising would block the calfkit RPC entirely.
    The *response* side is different: if the response projection fails the
    calling LLM would see a reply that was never projected to humans, so
    that case raises through :func:`_raise_infra` to enforce the
    "no reply without audit" contract.

    Discriminator: ``correlation_id is None`` → request side (best-effort,
    log+accept on final failure). ``correlation_id is not None`` →
    response side (raise on final failure). The callsite already passes
    ``None`` before the RPC and the calfkit-assigned id after, so no
    extra parameter is needed.

    Exception scope: only :class:`discord.HTTPException` (transient HTTP /
    rate-limit / 5xx) is retried; :class:`discord.NotFound` and
    :class:`discord.Forbidden` propagate immediately because they signal
    a permanent operator-actionable condition (channel deleted, bot lost
    Manage Webhooks) where another attempt is pointless. ``RuntimeError``
    (sender not started) and ``TypeError`` (channel id not a text channel)
    also escape — those are misconfiguration, not transient down. Mirrors
    the explicit-NotFound/Forbidden pattern in
    :mod:`calfkit_organization.bridge.outbox`.

    ``caller``/``target``/``correlation_id`` are logged on failure so an
    operator finding an audit gap on a particular pair channel can
    correlate it back to the specific A2A turn.
    """
    assert _persona_sender is not None  # guarded by the caller
    # Empty content is legal (some agents may legitimately reply ""), but
    # Discord rejects it. Substitute a visible placeholder so the audit log
    # makes sense rather than silently dropping the projection entry. Log
    # at INFO when this happens so operators can spot agents producing
    # empty responses — could indicate an upstream bug or a confused LLM.
    if not content:
        logger.info(
            "private_chat substituting empty-content placeholder persona=%s caller=%s target=%s correlation_id=%s",
            persona.name,
            caller,
            target,
            correlation_id,
        )
        payload = "(empty response)"
    else:
        payload = content
    last_exc: discord.HTTPException | None = None
    for attempt in range(1, _MAX_PROJECTION_ATTEMPTS + 1):
        try:
            await _persona_sender.send(persona, channel_id=channel_id, content=payload)
            return
        except (discord.NotFound, discord.Forbidden):
            # Permanent: channel gone or bot lost Manage Webhooks. Retrying
            # changes nothing; let the caller (and the operator's logs) see
            # the original exception type so the diagnosis is direct.
            raise
        except discord.HTTPException as e:
            last_exc = e
            if attempt < _MAX_PROJECTION_ATTEMPTS:
                logger.warning(
                    "projection attempt=%d failed persona=%s channel=%s caller=%s target=%s correlation_id=%s; retrying in %.1fs",
                    attempt,
                    persona.name,
                    channel_id,
                    caller,
                    target,
                    correlation_id,
                    _PROJECTION_RETRY_DELAY_SECONDS,
                    exc_info=True,
                )
                await asyncio.sleep(_PROJECTION_RETRY_DELAY_SECONDS)
                continue
            # Final attempt failed: branch on side.
            if correlation_id is None:
                # Request side: accept the audit gap so the calfkit RPC
                # still happens. README documents this as best-effort.
                # ERROR (not WARNING) — permanent audit data loss, not a
                # transient blip; alerting hooks keyed off ERROR fire.
                logger.error(
                    "projection failed persona=%s channel=%s caller=%s target=%s correlation_id=%s; accepting audit gap",
                    persona.name,
                    channel_id,
                    caller,
                    target,
                    correlation_id,
                    exc_info=True,
                )
                return
            # Response side: raise so the calling LLM never sees a reply
            # that wasn't projected to humans.
            _raise_infra(
                f"a2a audit projection failed after {_MAX_PROJECTION_ATTEMPTS} attempts persona={persona.name!r} channel={channel_id}",
                caller=caller,
                target=target,
                correlation_id=correlation_id,
                cause=last_exc,
            )


# Calfkit's ``@agent_tool`` decorator wraps the bare async function in a
# ``ToolNodeDef`` whose subscribe/publish topics derive from the function
# name (``tool.private_chat.input`` / ``tool.private_chat.output``).
# Applied as a regular call rather than the ``@agent_tool`` decorator
# form so the bare function above stays directly importable (and unit-
# testable) under its real name.
private_chat_tool: ToolNodeDef = agent_tool(private_chat)
