"""``private_chat`` — A2A tool for agent-to-agent communication over calfkit.

Routes a request from one agent to another via calfkit RPC and projects
the exchange to a unified ``private-a2a-chats`` Discord channel for human
audit (channel name configurable via :envvar:`CALFKIT_A2A_CHANNEL_NAME`).
Each A2A conversation gets its own Discord thread anchored on the
caller's first request message. Kafka is the system of record; Discord
is the projection.

Flow per invocation:
    1. Validate caller and target identities against the phonebook
       supplied by the bridge in ``deps``.
    2. Build a *forwarded* :class:`WireMessage` — a shallow clone of the
       caller's originating wire with ``slash_target=target_agent_id``,
       ``kind="slash"``, and the new ``content``. This makes the target's
       existing ``addressed_to_me`` gate accept the call without any gate
       changes.
    3. Resolve the unified audit channel via :class:`A2AChannelResolver`.
    4. Branch on ``thread_id``:
       * ``None`` (new thread): post the caller's request projection to
         the unified channel, then anchor a new thread on that message;
         ``message_history`` is empty.
       * ``int`` (continue thread): fetch the prior thread history
         (cache-bypassed), project it from the target's POV, then post
         the caller's request projection into the existing thread.
    5. ``Client.execute_node`` against ``agent.{target}.in`` with deps
       ``{"discord": forwarded_wire, "caller_agent_id": <caller>, "phonebook": ...}``
       and the computed ``message_history``. Default 60s timeout —
       fail-fast on no consumer or timeout.
    6. Post the response projection as the target's persona into the
       thread. Unlike the request projection this is **not** best-effort:
       persistent failure raises ``RuntimeError`` so the calling LLM
       never sees a reply that wasn't projected to humans.
    7. Return the target's text output prefixed with
       ``<thread_id>{id}</thread_id>\\n`` so the caller's LLM can opt
       into continuing the same thread on a follow-up call.

The module exposes both the bare async function ``private_chat`` (so
tests can call it directly without going through calfkit's dispatch) and
``private_chat_tool``, the :class:`ToolNodeDef` produced by ``agent_tool``
that the registry and ``calfkit-tools`` runner wire up.

Runtime dependencies reach the tool body via ``ctx.resources`` (calfkit
0.6.0 node-scoped lifecycle resources), not module globals. The Discord
connection (persona sender, channel resolver, REST client) is built by the
node-scoped :func:`_a2a_resource` ``@resource`` bracket — opened at worker
startup only when this node is hosted, closed at drain — and the
process-wide calfkit ``Client`` is exposed by the tools runner as a
worker-scoped resource. :func:`_resources_from_ctx` assembles both into the
per-call :class:`_A2A` working set. Agent processes import this module solely
for the ``ToolNodeDef`` schema and never call the function body, and the
resource bracket only runs on a worker that hosts the node, so importing the
module constructs nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import discord
from calfkit._vendor.pydantic_ai.messages import ModelMessage
from calfkit.client import Client
from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool
from calfkit.worker.lifecycle import ResourceSetupContext
from pydantic import ValidationError

from calfcord.agents.peer_roster import build_temp_instructions
from calfcord.agents.phonebook import PhonebookEntry, phonebook_from_deps, phonebook_to_deps
from calfcord.bridge.egress import A2AChannelResolver
from calfcord.bridge.history import HistoryRecord, project_history
from calfcord.bridge.wire import WireMessage
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import DiscordPersonaSender, Persona
from calfcord.discord.retry_feedback import (
    MAX_REPLY_RETRY_ATTEMPTS,
    build_retry_history,
    build_retry_reminder,
    chunk_split,
    classify_error,
)
from calfcord.discord.sender import DiscordSender
from calfcord.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
"""Default per-call timeout for the target's response. Overridable via
:envvar:`CALFKIT_TOOLS_TIMEOUT_SECONDS` (see :func:`_resolve_timeout`).
Fail-fast on timeout per A2A design — the caller's LLM can adapt or re-issue."""

_AGENT_INBOX_TOPIC_TEMPLATE = "agent.{agent_id}.in"
"""Must match the template the agent factory subscribes on
(``calfcord.agents.factory._AGENT_INBOX_TOPIC_TEMPLATE``).
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
:data:`calfcord.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS`
— same shape of failure (discord.py's internal budget exhausted), same
constraint (a single-worker consumer can't afford a long sleep)."""

_THREAD_NAME_MAX_TOTAL = 100
"""Discord's hard cap on thread names. Exceeding it 400s the thread
creation. The helper :func:`_build_thread_name` enforces it."""

_THREAD_NAME_CONTENT_MAX = 40
"""Soft cap on the topic-tail portion of the thread name (the part after
``caller→target: ``). Tunable; balances "scannable in Discord's
thread list" against "carries enough of the topic to disambiguate"."""

_THREAD_NAME_EMPTY_PLACEHOLDER = "<empty>"
"""Substituted into the thread name when the caller's ``content`` is
empty or whitespace-only after normalization. Prevents the degenerate
``"alice→bob: "`` trailing-whitespace name."""

# --- Deployment configuration (read at resource-bracket startup) -----------
# Env names + resolvers for the A2A audit channel and the per-call response
# timeout. They live here (the consumer) rather than in the tools runner so the
# runner stays free of private_chat specifics — a worker hosting only fs/shell
# tools never reads them.
_TIMEOUT_ENV = "CALFKIT_TOOLS_TIMEOUT_SECONDS"
_CATEGORY_ENV = "CALFKIT_A2A_CHANNEL_CATEGORY"
_CHANNEL_NAME_ENV = "CALFKIT_A2A_CHANNEL_NAME"
_DEFAULT_CHANNEL_NAME = "private-a2a-chats"
"""The single unified A2A audit channel. Every A2A conversation lives inside a
thread under this channel; operator setup collapses to one channel + one
permission overwrite. Overridable via :data:`_CHANNEL_NAME_ENV`."""


def _resolve_timeout() -> float:
    """Read ``CALFKIT_TOOLS_TIMEOUT_SECONDS`` or fall back to the default.

    Raises ``RuntimeError`` on a malformed value: this runs inside the
    :func:`_a2a_resource` bracket at worker startup, so a misconfiguration is an
    infra bug (matching the bracket's ``DISCORD_GUILD_ID`` check) — not the
    process-boot ``SystemExit`` it was when the tools runner called it directly.
    A setup error here propagates out of the resource bracket and fails boot.
    """
    raw = os.getenv(_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as e:
        raise RuntimeError(f"{_TIMEOUT_ENV} must be a number, got {raw!r}") from e
    if value <= 0:
        raise RuntimeError(f"{_TIMEOUT_ENV} must be positive, got {value}")
    return value


def _resolve_category_name() -> str | None:
    """Read ``CALFKIT_A2A_CHANNEL_CATEGORY`` or return ``None``.

    Empty / whitespace-only values are treated as unset so a stray blank line in
    ``.env`` yields the default uncategorized behavior rather than a category
    literally named "" or " ".
    """
    raw = os.getenv(_CATEGORY_ENV)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _resolve_channel_name() -> str:
    """Read ``CALFKIT_A2A_CHANNEL_NAME`` or fall back to the default.

    Mirrors :func:`_resolve_category_name`'s empty-as-unset normalization so a
    blank line in ``.env`` falls back to :data:`_DEFAULT_CHANNEL_NAME` rather
    than creating a literally-named channel.
    """
    raw = os.getenv(_CHANNEL_NAME_ENV)
    if raw is None:
        return _DEFAULT_CHANNEL_NAME
    stripped = raw.strip()
    return stripped or _DEFAULT_CHANNEL_NAME


@dataclass(frozen=True)
class _A2ADiscord:
    """The node-scoped Discord resource yielded by the ``@resource`` bracket.

    Built once at worker startup (only when ``private_chat`` is hosted) and torn
    down at drain. Distinct from the process-wide calfkit ``Client``, which the
    Worker owns regardless and is merged in under :data:`_A2A`.
    """

    persona_sender: DiscordPersonaSender
    resolver: A2AChannelResolver
    discord_client: discord.Client
    timeout_seconds: float


@dataclass(frozen=True)
class _A2A:
    """The per-invocation working set threaded through the tool body + helpers.

    Assembled from ``ctx.resources`` at the top of :func:`private_chat`: the
    worker-scoped ``Client`` plus the node-scoped :class:`_A2ADiscord` fields.
    Replaces the former module-level singletons.
    """

    client: Client
    persona_sender: DiscordPersonaSender
    resolver: A2AChannelResolver
    discord_client: discord.Client
    timeout_seconds: float

    @classmethod
    def from_parts(cls, *, client: Client, discord: _A2ADiscord) -> _A2A:
        """Merge the worker-scoped ``Client`` with the node-scoped Discord bundle.

        The single canonical assembly point so production (:func:`_resources_from_ctx`)
        and the tests can't drift when an ``_A2ADiscord`` field is added. Spreads
        the fields explicitly (not ``dataclasses.asdict``, which would deep-copy
        the live ``Client``/``discord.Client`` instead of sharing them).
        """
        return cls(
            client=client,
            persona_sender=discord.persona_sender,
            resolver=discord.resolver,
            discord_client=discord.discord_client,
            timeout_seconds=discord.timeout_seconds,
        )


_DISCORD_HISTORY_MAX_LIMIT = 100
"""Discord's per-call REST cap for ``channel.history(limit=...)``. Matches
:data:`bridge.history._DISCORD_HISTORY_MAX_LIMIT` (duplicated rather than
re-imported because the bridge symbol is module-private)."""

# A2A resource keys read from ``ctx.resources`` (see :func:`_resources_from_ctx`).
# ``_RES_CLIENT`` is worker-scoped (the process-wide calfkit ``Client`` the tools
# runner exposes); ``_RES_DISCORD`` is node-scoped (built by :func:`_a2a_resource`,
# only when private_chat is hosted).
_RES_CLIENT = "a2a_client"
_RES_DISCORD = "a2a"


def _resources_from_ctx(ctx: ToolContext, correlation_id: str) -> _A2A:
    """Assemble the per-call :class:`_A2A` working set from ``ctx.resources``.

    calfkit merges the worker-scoped resources (the ``Client``) under the
    node-scoped ones (the Discord bundle) into ``ctx.resources`` on every
    invocation. A missing key means the worker hosting this node never built
    the resources — a deployment bug, not an LLM-recoverable error, so raise
    with correlation context per the error-handling convention.

    Note the absence of any AgentRegistry: the tool's deployment is decoupled
    from the bridge and cannot read agents/*.md. The bridge passes the canonical
    roster snapshot in ``deps["phonebook"]`` on every invocation; the tool reads
    it per-call from ``ctx.deps``.
    """
    client = ctx.resources.get(_RES_CLIENT)
    discord_res = ctx.resources.get(_RES_DISCORD)
    # Type-check BOTH halves, not just presence: a wrong-typed value (e.g. the
    # runner keyed the wrong object under a2a_client) must fail here with the
    # real cause, not 200 lines later as an opaque AttributeError. Name which
    # half is missing so the operator fixes the right deployment side.
    if not isinstance(client, Client) or not isinstance(discord_res, _A2ADiscord):
        missing: list[str] = []
        if not isinstance(client, Client):
            missing.append(f"{_RES_CLIENT!r} (worker-scoped Client; tools runner must expose it)")
        if not isinstance(discord_res, _A2ADiscord):
            missing.append(f"{_RES_DISCORD!r} (node-scoped Discord bundle; private_chat must be hosted)")
        _raise_infra(
            "a2a resources unavailable; the worker did not supply "
            f"{' and '.join(missing)} (have keys: {sorted(ctx.resources)})",
            correlation_id=correlation_id,
        )
    return _A2A.from_parts(client=client, discord=discord_res)


def _build_thread_name(caller: str, target: str, content: str) -> str:
    """Produce a thread name like ``'conan→scribe: please summarize the doc'``.

    Newlines (``\\n``, ``\\r``) and other ASCII control characters in
    ``content`` are normalized to single spaces, then collapsed so runs
    of whitespace become one space. The cleaned tail is truncated to
    :data:`_THREAD_NAME_CONTENT_MAX` characters before assembly. The
    final string is hard-capped at :data:`_THREAD_NAME_MAX_TOTAL`
    characters (Discord's limit).

    Uses the unicode arrow ``→`` (U+2192) as the caller→target
    separator. Discord's thread-name limit is char-based (not byte-
    based), so the multi-byte arrow does not consume extra budget at
    the boundary.

    An empty or whitespace-only ``content`` becomes
    :data:`_THREAD_NAME_EMPTY_PLACEHOLDER` so the resulting name never
    ends with bare trailing whitespace.
    """
    # Replace control chars (including \n, \r, \t) with a space, then
    # collapse runs of whitespace. ``str.split()`` with no args splits on
    # any run of whitespace and drops empty segments — cheaper than a
    # regex for this hot path.
    cleaned_chars = [c if c.isprintable() else " " for c in content]
    cleaned = " ".join("".join(cleaned_chars).split())
    if not cleaned:
        cleaned = _THREAD_NAME_EMPTY_PLACEHOLDER
    truncated = cleaned[:_THREAD_NAME_CONTENT_MAX]
    name = f"{caller}→{target}: {truncated}"
    return name[:_THREAD_NAME_MAX_TOTAL]


async def private_chat(
    ctx: ToolContext,
    target_agent_id: str,
    content: str,
    thread_id: int | None = None,
) -> str:
    """Send a private message to another agent and get their reply.

    Use this to collaborate one-on-one with a peer — delegate a
    sub-task, ask a focused question, or get input only they can
    provide. The exchange is posted to a shared audit channel (inside
    a per-conversation thread) so users observing the organization can
    see agents collaborate.

    ## When not to use

    If you can answer the user yourself, just answer — don't loop
    through a peer for things you already know. Reserve ``private_chat``
    for genuine delegation: the peer has expertise, access, or persona
    you don't, OR you specifically want their take on something.

    Also: ``private_chat`` is for *peer-to-peer* work, not for talking
    to the human in the room. If the user asked a question, reply
    directly — don't ping a peer just to relay their answer.

    ## Writing ``content``

    Brief the peer like a smart colleague who just walked into the
    room — they haven't seen the conversation you're in, don't know
    what the user asked, don't know what you've already tried. **Make
    ``content`` self-contained**: include any background, constraints,
    examples, or references the peer needs to answer well.

    **Never delegate understanding.** Don't forward the user's question
    verbatim and hope the peer figures it out. Distill what *you* want
    from the peer, in your own words, with the context they need.

    Good vs. bad:

    * BAD: ``content="summarize that"`` — the peer has no "that" to
      resolve.
    * BAD: ``content="<user's literal question>"`` — the peer doesn't
      know who the user is, what conversation this is, or what you've
      already established.
    * GOOD: ``content="The user is choosing between Postgres and SQLite
      for a 50GB analytics workload with daily batch reads. They've
      ruled out cloud-managed options for cost. Give me a one-paragraph
      recommendation focused on the long-term operational burden."``

    The rule of thumb: a peer with zero context should be able to
    answer your ``content`` without asking a clarifying question.

    ## Continuation

    By default the peer sees only ``content`` — no prior turns. To give
    them multi-turn context, pass the ``thread_id`` returned from an
    earlier ``private_chat`` call **with this same target**. The peer
    will then see the thread's prior turns as ``message_history`` and
    you can write follow-ups that reference earlier exchanges
    (``"refine that into bullet points"``, ``"what about the third
    option?"``).

    Use continuation when your message references something the peer
    already said, or when you want a back-and-forth on the same topic.
    Use a fresh thread (omit ``thread_id``) when starting an unrelated
    request.

    Args:
        target_agent_id: The peer agent's id (e.g. ``"scribe"``). Must
            be a registered agent and must not be your own id.
        content: The message text to send to the peer. See the
            "Writing ``content``" guidance above.
        thread_id: When ``None`` (default), start a fresh conversation
            in a new thread — the peer sees only ``content``. When set
            to an integer thread id (extracted from the ``<thread_id>``
            tag of a prior reply from this same target), continue that
            thread.

    Returns:
        On success, a string of the form::

            <thread_id>1234567890123456</thread_id>
            sure, here's the summary you asked for: ...

        The first line is a tag carrying the thread id; the peer's
        actual response begins on line 2. Remember the integer if you
        might want to continue this conversation later. The response
        may be empty if the peer produced no output.

        **The ``<thread_id>`` tag is internal — do NOT show it to the
        user or include it in your own reply.** It's there so YOU can
        opt into continuation on a later turn; it has no meaning to the
        human reading your output.

        On operational issues the tool returns an error message
        starting with ``error:`` (with NO ``<thread_id>`` tag) so you
        can adapt (retry, pick a different agent, or fall back).
        Possible errors:

        * ``error: unknown agent '<name>'; known agents: ...`` —
          ``target_agent_id`` is not registered. Fix the id and retry.
        * ``error: agent '<self>' cannot privately chat with itself`` —
          pick a different agent.
        * ``error: target '<name>' did not reply within Ns`` — the
          peer did not respond in time. Try again or pick another
          agent.
        * ``error: thread <id> not accessible; start a new conversation
          by omitting thread_id`` — the supplied ``thread_id`` was
          deleted, points at a non-thread channel, or you lack access.
          Drop the id and call again.
    """
    correlation_id = ctx.correlation_id
    res = _resources_from_ctx(ctx, correlation_id)

    caller_agent_id = ctx.agent_name
    if caller_agent_id is None:
        # ctx.agent_name is set from the inbound x-calf-emitter header.
        # If it's missing, calfkit's dispatch was bypassed somehow.
        _raise_infra(
            "invoked without emitter_node_id; cannot identify caller",
            correlation_id=correlation_id,
        )

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
    phonebook_raw = ctx.deps.get("phonebook")
    if phonebook_raw is None:
        _raise_infra(
            "invoked without deps['phonebook']; the bridge ingress is expected to populate this key on every publish",
            correlation_id=correlation_id,
            caller=caller_agent_id,
            target=target_agent_id,
        )
    try:
        phonebook = phonebook_from_deps(phonebook_raw)
    except (ValueError, ValidationError) as e:
        _raise_infra(
            f"received malformed deps['phonebook']: {e}",
            correlation_id=correlation_id,
            caller=caller_agent_id,
            target=target_agent_id,
            cause=e,
        )

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
        _raise_infra(
            f"caller {caller_agent_id!r} is not in the phonebook; cannot resolve persona",
            correlation_id=correlation_id,
            caller=caller_agent_id,
            target=target_agent_id,
        )

    incoming_wire_dict = ctx.deps.get("discord")
    if not isinstance(incoming_wire_dict, dict):
        _raise_infra(
            "invoked without deps['discord']; the bridge ingress is expected to "
            "populate this key before any agent runs",
            correlation_id=correlation_id,
            caller=caller_agent_id,
            target=target_agent_id,
        )
    try:
        incoming_wire = WireMessage.model_validate(incoming_wire_dict)
    except ValidationError as e:
        _raise_infra(
            f"received malformed deps['discord']: {e}",
            correlation_id=correlation_id,
            caller=caller_agent_id,
            target=target_agent_id,
            cause=e,
        )

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
        unified_channel_id = await res.resolver.resolve_unified_channel()
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

    if thread_id is None:
        message_history, conversation_thread_id = await _start_new_thread(
            res=res,
            caller_persona=caller_persona,
            unified_channel_id=unified_channel_id,
            content=content,
            caller_agent_id=caller_agent_id,
            target_agent_id=target_agent_id,
            correlation_id=correlation_id,
        )
    else:
        continue_result = await _continue_existing_thread(
            res=res,
            caller_persona=caller_persona,
            unified_channel_id=unified_channel_id,
            content=content,
            thread_id=thread_id,
            target_entry=target_entry,
            target_agent_id=target_agent_id,
            caller_agent_id=caller_agent_id,
            correlation_id=correlation_id,
            phonebook=phonebook,
        )
        if isinstance(continue_result, str):
            # Recoverable error path — bare error string (no <thread_id>
            # tag) so the LLM does not try to continue a dead thread.
            return continue_result
        message_history, conversation_thread_id = continue_result

    target_topic = _AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id=target_agent_id)
    logger.info(
        "private_chat invoking caller=%s target=%s topic=%s thread_id=%s history_len=%d timeout=%.1fs",
        caller_agent_id,
        target_agent_id,
        target_topic,
        conversation_thread_id,
        len(message_history),
        res.timeout_seconds,
    )
    try:
        result = await res.client.execute_node(
            user_prompt=content,
            topic=target_topic,
            deps={
                # Project the caller's deps forward so ambient context the
                # bridge seeds at the root (e.g. the memory-prompt template)
                # survives the A2A hop. The explicit keys below override the
                # forwarded values — the target must see the A2A-forwarded
                # wire and this hop's caller_agent_id, not the caller's.
                # ``ctx.deps`` is always a dict (calfkit constructs it as
                # ``deps or {}``), matching the unguarded ``.get`` reads above.
                **ctx.deps,
                "discord": forwarded_wire.model_dump(mode="json"),
                "caller_agent_id": caller_agent_id,
                # Propagate the phonebook so the target (if it chains into
                # another private_chat) sees the same roster we did.
                "phonebook": phonebook_to_deps(phonebook),
            },
            output_type=str,
            timeout=res.timeout_seconds,
            temp_instructions=build_temp_instructions(phonebook, target_agent_id, channel=False),
            message_history=message_history,
        )
    except TimeoutError:
        # Returning as a string (rather than raising) is deliberate: if we
        # raise, the tool's ReturnCall never fires and the calling agent's
        # execute_node also times out — two timeouts for one logical
        # failure. An error string lets the LLM see the failure and adapt.
        logger.warning(
            "private_chat timeout caller=%s target=%s topic=%s timeout=%.1fs",
            caller_agent_id,
            target_agent_id,
            target_topic,
            res.timeout_seconds,
        )
        return f"error: target {target_agent_id!r} did not reply within {res.timeout_seconds:.0f}s"
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

    projected_text = await _post_response_with_feedback_retries(
        res=res,
        target_agent_id=target_agent_id,
        target_persona=target_persona,
        original_user_prompt=content,
        original_history=message_history,
        forwarded_wire_dict=forwarded_wire.model_dump(mode="json"),
        phonebook=phonebook,
        unified_channel_id=unified_channel_id,
        conversation_thread_id=conversation_thread_id,
        initial_response_text=response_text,
        caller_agent_id=caller_agent_id,
        caller_deps=ctx.deps,
        correlation_id=result.correlation_id,
    )

    logger.info(
        "private_chat completed caller=%s target=%s thread_id=%s correlation_id=%s response_len=%d",
        caller_agent_id,
        target_agent_id,
        conversation_thread_id,
        result.correlation_id,
        len(projected_text),
    )
    return f"<thread_id>{conversation_thread_id}</thread_id>\n{projected_text}"


async def _post_response_with_feedback_retries(
    *,
    res: _A2A,
    target_agent_id: str,
    target_persona: Persona,
    original_user_prompt: str,
    original_history: Sequence[ModelMessage],
    forwarded_wire_dict: dict,
    phonebook: Sequence[PhonebookEntry],
    unified_channel_id: int,
    conversation_thread_id: int,
    initial_response_text: str,
    caller_agent_id: str,
    correlation_id: str,
    caller_deps: dict | None = None,
) -> str:
    """Project the target's reply to the audit thread, retrying the
    target with system-reminder feedback when Discord rejects content
    the LLM can plausibly fix (400 family). Falls back to chunk-
    splitting the latest reply into the audit thread when the retry
    budget is exhausted.

    Returns the LAST reply text — the value ``private_chat`` returns
    to the caller. On a successful single attempt, that's the original
    response. After one or more retries, that's the most recent
    (typically shorter) version the target produced. On budget
    exhaustion, the chunk-split fallback projects the latest text
    across N audit-thread posts and the caller still receives the
    full untruncated content.

    Mirrors the bridge outbox's retry-with-feedback (see
    :func:`calfcord.bridge.outbox._handle_post_failure`),
    sharing policy via
    :mod:`calfcord.discord.retry_feedback`. The orchestration
    differs because A2A is synchronous inside the caller's
    ``execute_node`` RPC, whereas the bridge republishes
    fire-and-forget across the Kafka consumer boundary.

    On ``"drop"`` or ``"transient"`` errors (per
    :func:`classify_error`) raises through :func:`_raise_infra` — these
    aren't LLM-fixable and the caller's existing infra-bug contract
    applies.
    """
    current_text = initial_response_text
    attempts_used = 0
    while True:
        # Direct ``persona_sender.send`` (not ``_post_projection``) so a
        # ``discord.HTTPException`` propagates to ``classify_error``
        # below instead of getting swallowed by ``_post_projection``'s
        # internal retry-and-raise. ``_post_projection`` remains the
        # right helper for the request-side audit-gap path used by
        # ``_start_new_thread`` / ``_continue_existing_thread``;
        # response-side retry-with-feedback orchestration lives here.
        payload = current_text if current_text else "(empty response)"
        if not current_text:
            logger.info(
                "a2a substituting empty-content placeholder for response "
                "persona=%s caller=%s target=%s correlation_id=%s",
                target_persona.name, caller_agent_id, target_agent_id,
                correlation_id,
            )
        try:
            await res.persona_sender.send(
                target_persona,
                channel_id=unified_channel_id,
                content=payload,
                thread_id=conversation_thread_id,
            )
            return current_text
        except discord.DiscordException as error:
            # Catch ``DiscordException`` (broader than ``HTTPException``)
            # so :class:`discord.RateLimited` — a sibling, NOT a subclass
            # — also routes through ``classify_error`` instead of
            # escaping uncaught to the caller's LLM. Mirrors the bridge
            # outbox's ``_handle_post_failure`` catch surface.
            decision = classify_error(error)
            # ``status_str`` is for the operator-facing log message
            # only. ``RateLimited`` has no ``.status``; non-HTTPException
            # ``DiscordException`` subclasses similarly. ``getattr`` keeps
            # the log message construction total against either case.
            status_str = getattr(error, "status", type(error).__name__)
            if decision == "drop":
                _raise_infra(
                    f"a2a audit projection failed (non-agent-fixable "
                    f"status={status_str}) persona={target_persona.name!r} "
                    f"channel={unified_channel_id} thread_id={conversation_thread_id}",
                    caller=caller_agent_id,
                    target=target_agent_id,
                    correlation_id=correlation_id,
                    cause=error,
                )
            if decision == "transient":
                # 5xx — Discord-side outage. A content-retry can't fix
                # it; discord.py's own internal backoff has already
                # tried 5+ times before raising. Surface to the caller's
                # infra-bug contract.
                _raise_infra(
                    f"a2a audit projection failed (transient status={status_str}, "
                    f"discord backoff exhausted) persona={target_persona.name!r} "
                    f"channel={unified_channel_id} thread_id={conversation_thread_id}",
                    caller=caller_agent_id,
                    target=target_agent_id,
                    correlation_id=correlation_id,
                    cause=error,
                )
            # ``classify_error`` only returns ``"agent_fixable"`` for
            # ``HTTPException`` (RateLimited and other DiscordException
            # subclasses short-circuit to ``"drop"`` above), so
            # ``error.status`` below is safe.
            assert isinstance(error, discord.HTTPException)
            # decision == "agent_fixable"
            if attempts_used >= MAX_REPLY_RETRY_ATTEMPTS:
                logger.warning(
                    "a2a retry budget exhausted attempt=%d max=%d caller=%s "
                    "target=%s thread_id=%s status=%s; chunk-splitting "
                    "projection and returning full latest reply to caller",
                    attempts_used, MAX_REPLY_RETRY_ATTEMPTS,
                    caller_agent_id, target_agent_id, conversation_thread_id,
                    error.status,
                )
                await _post_chunked_projection(
                    res, target_persona, unified_channel_id, conversation_thread_id,
                    current_text, caller_agent_id, target_agent_id,
                )
                return current_text
            attempts_used += 1
            logger.info(
                "a2a triggering agent retry-with-feedback attempt=%d caller=%s "
                "target=%s status=%s: %s",
                attempts_used, caller_agent_id, target_agent_id,
                error.status, error,
            )
            try:
                current_text = await _execute_retry_with_feedback(
                    res=res,
                    target_agent_id=target_agent_id,
                    error=error,
                    failed_text=current_text,
                    original_user_prompt=original_user_prompt,
                    original_history=original_history,
                    forwarded_wire_dict=forwarded_wire_dict,
                    phonebook=phonebook,
                    caller_agent_id=caller_agent_id,
                    caller_deps=caller_deps,
                )
            except Exception:
                # Catches ``TimeoutError`` and everything else the RPC
                # can produce (calfkit ``DeserializationError``, broker
                # ``ConnectionError``, etc.). ``asyncio.CancelledError``
                # is a ``BaseException`` subclass and propagates by
                # design — a shutdown mid-retry must not be swallowed
                # into the chunk-split fallback. ERROR-level (via
                # :meth:`logger.exception`) so the alerting hooks fire;
                # parity with the bridge outbox's retry-publish-failure
                # handler at :func:`bridge.outbox._handle_post_failure`.
                logger.exception(
                    "a2a retry execute_node failed caller=%s target=%s "
                    "attempt=%d; chunk-splitting latest reply and "
                    "returning to caller",
                    caller_agent_id, target_agent_id, attempts_used,
                )
                await _post_chunked_projection(
                    res, target_persona, unified_channel_id, conversation_thread_id,
                    current_text, caller_agent_id, target_agent_id,
                )
                return current_text


async def _execute_retry_with_feedback(
    *,
    res: _A2A,
    target_agent_id: str,
    error: discord.HTTPException,
    failed_text: str,
    original_user_prompt: str,
    original_history: Sequence[ModelMessage],
    forwarded_wire_dict: dict,
    phonebook: Sequence[PhonebookEntry],
    caller_agent_id: str,
    caller_deps: dict | None = None,
) -> str:
    """Re-invoke the target with a ``<system-reminder>``-tagged prompt
    plus the failed reply appended to history.

    Mirrors :func:`calfcord.bridge.outbox._publish_retry`
    but uses :meth:`Client.execute_node` (sync await) instead of
    :meth:`Client.invoke_node` (fire-and-forget), because A2A is
    inside the caller's RPC.
    """
    reminder = build_retry_reminder(error, failed_text)
    retry_history = build_retry_history(
        original_history=original_history,
        original_user_prompt=original_user_prompt,
        failed_text=failed_text,
    )
    target_topic = _AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id=target_agent_id)
    result = await res.client.execute_node(
        user_prompt=reminder,
        topic=target_topic,
        deps={
            # Same forward-then-override as the primary A2A invocation, so the
            # retry carries the bridge-seeded ambient context (memory prompt).
            **(caller_deps or {}),
            "discord": forwarded_wire_dict,
            "caller_agent_id": caller_agent_id,
            "phonebook": phonebook_to_deps(phonebook),
        },
        output_type=str,
        timeout=res.timeout_seconds,
        temp_instructions=build_temp_instructions(
            phonebook, target_agent_id, channel=False,
        ),
        message_history=retry_history,
    )
    return result.output if result.output is not None else ""


async def _post_chunked_projection(
    res: _A2A,
    persona: Persona,
    channel_id: int,
    thread_id: int,
    text: str,
    caller: str,
    target: str,
) -> None:
    """Final fallback: split ``text`` into ≤2000-char chunks and post
    each into the A2A audit thread under ``persona``.

    Mirrors :func:`calfcord.bridge.outbox._post_chunked_fallback`
    but posts into a thread rather than as an anchored channel reply.
    Per-chunk failures are logged independently so partial delivery
    is preserved. Catches :class:`discord.DiscordException` (broader
    than :class:`HTTPException`) — :class:`RateLimited` at the chunk
    layer is the last resort and nothing useful can route around it.
    """
    chunks = chunk_split(text)
    if not chunks:
        logger.warning(
            "a2a chunk-split received empty text caller=%s target=%s thread_id=%s",
            caller, target, thread_id,
        )
        return
    total = len(chunks)
    failure_statuses: list[int | None] = []
    for i, chunk in enumerate(chunks):
        try:
            await res.persona_sender.send(
                persona=persona,
                channel_id=channel_id,
                content=chunk,
                thread_id=thread_id,
            )
        except discord.DiscordException as e:
            status = getattr(e, "status", None)
            failure_statuses.append(status)
            logger.error(
                "a2a chunk-split failed chunk %d/%d caller=%s target=%s "
                "thread_id=%s status=%s: %s",
                i + 1, total, caller, target, thread_id, status, e,
            )
    if failure_statuses and len(failure_statuses) == total:
        dominant_status = max(set(failure_statuses), key=failure_statuses.count)
        logger.warning(
            "a2a chunk-split delivered 0/%d chunks caller=%s target=%s "
            "thread_id=%s dominant_status=%s; audit lost for this turn",
            total, caller, target, thread_id, dominant_status,
        )


async def _start_new_thread(
    *,
    res: _A2A,
    caller_persona: Persona,
    unified_channel_id: int,
    content: str,
    caller_agent_id: str,
    target_agent_id: str,
    correlation_id: str,
) -> tuple[list[ModelMessage], int]:
    """Post the request projection to the unified channel and anchor a new thread on it.

    Returns ``(empty_message_history, new_thread_id)``. Raises
    ``RuntimeError`` (via :func:`_raise_infra`) if anchoring fails or
    if the request projection's audit-gap fallback returns ``None`` —
    we cannot anchor a thread on a phantom message, so a missing
    anchor breaks the continuation contract.
    """
    sent = await _post_projection(
        res,
        caller_persona,
        unified_channel_id,
        content,
        caller=caller_agent_id,
        target=target_agent_id,
        correlation_id=None,
    )
    if sent is None:
        # Request-side audit-gap was accepted (persistent transient
        # failure swallowed by ``_post_projection``). Without an anchor
        # we cannot create a thread, and without a thread the caller
        # has no continuation handle for this conversation — escalate
        # to infra error rather than silently degrade to "no thread".
        _raise_infra(
            "request projection accepted an audit gap; no anchor message available to create a thread",
            caller=caller_agent_id,
            target=target_agent_id,
            correlation_id=correlation_id,
        )
    try:
        new_thread_id = await res.resolver.create_anchored_thread(
            unified_channel_id,
            sent.id,
            name=_build_thread_name(caller_agent_id, target_agent_id, content),
        )
    except discord.DiscordException as e:
        # No thread = no future continuation, which is the audit-and-
        # continuation invariant. The already-posted request projection
        # is acceptable orphan data; the operator-facing log captures
        # the failure.
        _raise_infra(
            f"create_anchored_thread failed channel={unified_channel_id} anchor={sent.id}: {e}",
            caller=caller_agent_id,
            target=target_agent_id,
            correlation_id=correlation_id,
            cause=e,
        )
    return [], new_thread_id


async def _continue_existing_thread(
    *,
    res: _A2A,
    caller_persona: Persona,
    unified_channel_id: int,
    content: str,
    thread_id: int,
    target_entry: PhonebookEntry,
    target_agent_id: str,
    caller_agent_id: str,
    correlation_id: str,
    phonebook: Sequence[PhonebookEntry],
) -> tuple[list[ModelMessage], int] | str:
    """Fetch prior thread history, project it, then post the request projection into the thread.

    Returns either:
    * ``(message_history, thread_id)`` on success, or
    * a recoverable error string (no ``<thread_id>`` tag) when the
      supplied ``thread_id`` is inaccessible — the caller surfaces
      this directly to the LLM.

    Fetch FIRST, then post: posting first then fetching would put the
    just-posted request in both ``message_history`` AND ``user_prompt``,
    which the callee would see as a duplicate.
    """
    try:
        records = await _fetch_thread_history(
            res.discord_client,
            thread_id,
            limit=target_entry.history_turns,
            phonebook=phonebook,
        )
    except (discord.Forbidden, discord.NotFound, TypeError):
        # Permanent + LLM-recoverable: the caller passed an id we cannot
        # read (deleted thread, bot lacks Read Message History, wrong id,
        # or an id that resolves to a non-messageable channel — e.g. a
        # category id the LLM hallucinated). Return a documented error
        # string so the LLM can drop the id and restart the conversation.
        logger.warning(
            "private_chat thread fetch failed caller=%s target=%s thread_id=%s "
            "correlation_id=%s; returning recoverable error",
            caller_agent_id,
            target_agent_id,
            thread_id,
            correlation_id,
            exc_info=True,
        )
        return (
            f"error: thread {thread_id} not accessible; "
            "start a new conversation by omitting thread_id"
        )
    except discord.HTTPException as e:
        # Transient 5xx flavor: not the LLM's bug. Funnel through infra.
        # Inline ``status``/``text`` for operator triage — the generic
        # ``_raise_infra`` prefix would otherwise hide them in ``exc_info``.
        _raise_infra(
            f"thread history fetch failed thread_id={thread_id} "
            f"status={e.status} text={e.text!r}: {e}",
            caller=caller_agent_id,
            target=target_agent_id,
            correlation_id=correlation_id,
            cause=e,
        )
    except discord.DiscordException as e:
        # Catchall for the rest of the ``DiscordException`` family:
        # :class:`discord.RateLimited` (NOT an :class:`HTTPException`
        # subclass) plus the :class:`discord.ClientException` branches
        # (``ConnectionClosed``, ``InvalidData``) we'd otherwise let
        # propagate raw out of ``private_chat``. Funnel through infra so
        # the calling LLM sees a ``RuntimeError`` with caller/target
        # context (the documented infra-bug contract).
        _raise_infra(
            f"thread history fetch failed thread_id={thread_id} "
            f"exc_class={type(e).__name__}: {e}",
            caller=caller_agent_id,
            target=target_agent_id,
            correlation_id=correlation_id,
            cause=e,
        )
    message_history = project_history(records, self_agent_id=target_agent_id)
    await _post_projection(
        res,
        caller_persona,
        unified_channel_id,
        content,
        thread_id=thread_id,
        caller=caller_agent_id,
        target=target_agent_id,
        correlation_id=None,
    )
    return message_history, thread_id


async def _fetch_thread_history(
    discord_client: discord.Client,
    thread_id: int,
    *,
    limit: int,
    phonebook: Sequence[PhonebookEntry],
) -> list[HistoryRecord]:
    """Fetch recent messages from a Discord thread as :class:`HistoryRecord` list.

    Lighter than :class:`bridge.history.ChannelHistoryFetcher`: A2A has no
    fan-out (so no single-flight coalescing) and no freshness budget (the
    caller's request was just posted, so any LRU window would serve stale
    records — we always go to Discord). Identity resolution uses the
    per-call ``phonebook`` rather than an :class:`AgentRegistry` because
    the tools process is registry-free by design (see module docstring).

    Unlike the bridge fetcher's fail-safe contract, this helper RAISES on
    Discord errors. The caller maps :class:`discord.Forbidden` and
    :class:`discord.NotFound` to the recoverable error string surfaced to
    the LLM (so it can drop a dead ``thread_id`` and start over), and
    funnels :class:`discord.HTTPException` (5xx) through ``_raise_infra``.

    Args:
        discord_client: The bot's REST client (reused from the persona
            sender; no second gateway connection).
        thread_id: Discord thread id to read.
        limit: Maximum records to return. Clamped to Discord's per-call
            cap (:data:`_DISCORD_HISTORY_MAX_LIMIT`); ``0`` short-circuits.
        phonebook: Per-invocation roster used to map a webhook author's
            ``display_name`` back to its ``agent_id``. Non-webhook authors
            (humans, third-party bots) always get ``author_agent_id=None``.

    Returns:
        Oldest-first list of :class:`HistoryRecord`. Records mirror the
        bridge fetcher's shape so :func:`project_history` consumes them
        identically.

    Raises:
        discord.Forbidden: Bot lacks Read Message History on the thread.
        discord.NotFound: Thread does not exist or bot lacks View Channel.
        discord.HTTPException: Other Discord-side failure (transient 5xx).
    """
    if limit <= 0:
        return []
    capped = min(limit, _DISCORD_HISTORY_MAX_LIMIT)

    channel = discord_client.get_channel(thread_id)
    if channel is None:
        channel = await discord_client.fetch_channel(thread_id)

    # ``thread_id`` is LLM-supplied; a hallucinated/spoofed id pointing at
    # a ``CategoryChannel`` / ``ForumChannel`` / voice channel would
    # ``AttributeError`` (or worse, silently return zero messages) inside
    # the history loop. ``discord.abc.Messageable`` is the protocol
    # ``Thread`` / ``TextChannel`` / ``DMChannel`` satisfy; non-messageable
    # types raise the same recoverable error the caller maps to a
    # documented LLM-facing string. ``TypeError`` (rather than ``NotFound``)
    # because the channel exists — it's just not usable for history reads.
    if not isinstance(channel, discord.abc.Messageable):
        raise TypeError(
            f"channel id={thread_id} is a {type(channel).__name__}, "
            "not a messageable channel/thread"
        )

    display_to_agent: dict[str, str] = {
        e.display_name: e.agent_id for e in phonebook
    }

    # ``channel.history`` returns newest-first; we reverse for chronological.
    messages = [m async for m in channel.history(limit=capped)]
    records: list[HistoryRecord] = []
    for msg in reversed(messages):
        author_display_name = (
            getattr(msg.author, "display_name", None) or msg.author.name
        )
        # Non-webhook messages (humans, third-party bots) always map to
        # author_agent_id=None — project_history then treats them as
        # peer messages with a `<display_name>` prefix.
        author_agent_id: str | None = None
        if msg.webhook_id is not None:
            author_agent_id = display_to_agent.get(author_display_name)
        records.append(
            HistoryRecord(
                message_id=msg.id,
                created_at=msg.created_at,
                content=msg.content,
                author_display_name=author_display_name,
                author_agent_id=author_agent_id,
            )
        )
    return records


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
    res: _A2A,
    persona: Persona,
    channel_id: int,
    content: str,
    *,
    thread_id: int | None = None,
    caller: str,
    target: str,
    correlation_id: str | None,
) -> SentMessage | None:
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
    :mod:`calfcord.bridge.outbox`.

    ``caller``/``target``/``correlation_id`` are logged on failure so an
    operator finding an audit gap in the unified A2A channel can
    correlate it back to the specific A2A turn.

    Returns:
        :class:`SentMessage` on successful first-or-retried send. The
        new-thread branch of :func:`private_chat` uses ``.id`` as the
        anchor for the freshly-created thread. ``None`` only when the
        request-side branch accepts an audit gap on persistent
        failure — callers that need an anchor must guard on ``None``
        and escalate via :func:`_raise_infra`.
    """
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
            return await res.persona_sender.send(
                persona,
                channel_id=channel_id,
                content=payload,
                thread_id=thread_id,
            )
        except (discord.NotFound, discord.Forbidden):
            # Permanent: channel gone or bot lost Manage Webhooks. Retrying
            # changes nothing; let the caller (and the operator's logs) see
            # the original exception type so the diagnosis is direct.
            raise
        except discord.HTTPException as e:
            last_exc = e
            if attempt < _MAX_PROJECTION_ATTEMPTS:
                logger.warning(
                    "projection attempt=%d failed persona=%s channel=%s thread_id=%s "
                    "caller=%s target=%s correlation_id=%s; retrying in %.1fs",
                    attempt,
                    persona.name,
                    channel_id,
                    thread_id,
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
                    "projection failed persona=%s channel=%s thread_id=%s "
                    "caller=%s target=%s correlation_id=%s; accepting audit gap",
                    persona.name,
                    channel_id,
                    thread_id,
                    caller,
                    target,
                    correlation_id,
                    exc_info=True,
                )
                return None
            # Response side: raise so the calling LLM never sees a reply
            # that wasn't projected to humans.
            _raise_infra(
                f"a2a audit projection failed after {_MAX_PROJECTION_ATTEMPTS} attempts "
                f"persona={persona.name!r} channel={channel_id} thread_id={thread_id}",
                caller=caller,
                target=target,
                correlation_id=correlation_id,
                cause=last_exc,
            )
    # Unreachable: every loop iteration either returns or raises. The
    # explicit ``return None`` keeps mypy from inferring a missing branch
    # and keeps callers' ``SentMessage | None`` type discipline honest.
    return None


# Calfkit's ``@agent_tool`` decorator wraps the bare async function in a
# ``ToolNodeDef`` whose subscribe/publish topics derive from the function
# name (``tool.private_chat.input`` / ``tool.private_chat.output``).
# Applied as a regular call rather than the ``@agent_tool`` decorator
# form so the bare function above stays directly importable (and unit-
# testable) under its real name.
private_chat_tool: ToolNodeDef = agent_tool(private_chat)


@private_chat_tool.resource(_RES_DISCORD)
async def _a2a_resource(ctx: ResourceSetupContext[ToolNodeDef]) -> AsyncIterator[_A2ADiscord]:
    """Open the Discord connection ``private_chat`` needs, scoped to this node.

    calfkit builds a node's resources iff the node is registered on the worker,
    so this runs at startup only when ``private_chat`` is actually hosted and
    tears the connection down at drain. That is why a worker hosting only
    fs/shell tools never constructs Discord and never needs ``DISCORD_GUILD_ID``
    — the requirement is enforced here, not unconditionally in the tools runner.

    The thread-history fetch needs a live ``discord.Client``; the persona sender
    already authenticates one on startup (REST-only, no gateway), so we reuse
    ``persona_sender.client`` rather than opening a second connection.
    """
    settings = DiscordSettings()  # type: ignore[call-arg]
    if settings.guild_id is None:
        raise RuntimeError(
            "DISCORD_GUILD_ID is required to host private_chat "
            "(the A2A channel resolver anchors the audit channel to a guild)"
        )
    async with (
        DiscordSender(settings) as sender,
        DiscordPersonaSender(settings) as persona_sender,
    ):
        resolver = A2AChannelResolver(
            sender,
            settings.guild_id,
            channel_name=_resolve_channel_name(),
            category_name=_resolve_category_name(),
        )
        yield _A2ADiscord(
            persona_sender=persona_sender,
            resolver=resolver,
            # ``persona_sender.client`` raises if start() hasn't been awaited;
            # the ``async with`` above already awaited it, so a future lazy-init
            # refactor fails fast here at boot rather than at first invocation.
            discord_client=persona_sender.client,
            timeout_seconds=_resolve_timeout(),
        )
