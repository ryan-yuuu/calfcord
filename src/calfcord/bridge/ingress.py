"""Discord → calfkit publish path: fire-and-forget agent invocation.

The bridge publishes the normalized :class:`WireMessage` to the right
ingress topic based on ``wire.kind`` and returns immediately. Replies
land on ``discord.outbox`` and are posted by the outbox consumer
(:mod:`calfcord.bridge.outbox`) — every reply, not just the
first to win the calfkit reply dispatcher's ``correlation_id`` race.

Kind-branching:

* ``kind="slash"`` (a Discord slash command, an @-mention parsed as a
  slash, OR a synthesized wire from the router's fan-out): published
  to ``discord.channel.{cid}.in`` where every assistant agent
  subscribed to the channel sees it. The agent's
  ``addressed_to_me_gate`` accepts iff ``slash_target`` matches its
  own ``agent_id``.
* ``kind="message"`` (ambient — non-slash, non-@-mention text from a
  human): published to the router's ambient ingress
  (``discord.ambient.in``) with the original wire, the phonebook, and
  the channel history on ``deps``. The router's fan-out consumer reads
  them back from ``result.deps`` (calfkit ≥ 0.4.0), decides which
  assistants should respond, and republishes synthesized ``kind="slash"``
  wires through ``bridge.synthesized.in``, which loops back to this
  class's :meth:`handle` via the bridge's synthesized-in consumer.

Two pieces of state cross the ingress→egress boundary:

* ``deps={"discord": wire}`` rides on the envelope so the agent's gates
  (:mod:`calfcord.agents.gates`) can read it. The dep
  survives the agent's :class:`ReturnCall` republish into
  ``discord.outbox`` because :meth:`BaseNodeDef._publish_action` carries
  ``envelope.context.deps`` forward.
* The same wire is also written to a process-local :class:`PendingWires`
  map keyed on ``correlation_id``, together with the bridge-computed
  retry context (history snapshot + cursor, temp instructions, model
  settings) the outbox needs to rebuild a faithful retry envelope —
  context that does NOT ride on ``ConsumerContext.deps`` the way the
  wire itself does. See :mod:`pending_wires` for the rationale.

Per-call thinking-effort overrides: when ``wire.slash_target`` is set
we read the target agent's current ``thinking_effort`` from the
in-memory registry and attach a provider-specific ``model_settings``
dict to the invocation so the agent uses the configured effort on this
exact call. Ambient messages flow without an override (the agent falls
back to whatever was baked into its model client at boot).

Ambient-reply health signal (v1):
    We log INFO at every ambient publish and (in
    :mod:`bridge.synthesized`) INFO at every synthesized-in arrival.
    Operators correlating those two streams can detect a silent
    router. Per-reply WARN tracking (pairing each publish with its
    eventual user-visible reply) is deferred — keeps v1 simple. See
    ``docs/ambient-routing.md`` "Operating" section for the runbook.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from typing import Any, Final

from calfkit._vendor.pydantic_ai.messages import (
    BaseToolReturnPart,
    ModelMessage,
    ModelMessagesTypeAdapter,
)
from calfkit.client import Client

from calfcord.agents.definition import Provider
from calfcord.agents.factory import DEFAULT_PROVIDER, resolve_provider
from calfcord.agents.memory import memory_prompt_deps_for_registry
from calfcord.agents.peer_roster import build_temp_instructions
from calfcord.agents.phonebook import (
    PhonebookEntry,
    phonebook_from_registry,
    phonebook_to_deps,
)
from calfcord.agents.thinking import build_model_settings
from calfcord.bridge.history import (
    ChannelHistoryFetcher,
    HistoryRecord,
    project_history,
)
from calfcord.bridge.pending_wires import PendingEntry, PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.transcripts import TranscriptStoreLike
from calfcord.bridge.wire import WireMessage
from calfcord.topics import DISCORD_OUTBOX_TOPIC

logger = logging.getLogger(__name__)

_DEFAULT_INGRESS_TOPIC_TEMPLATE = "discord.channel.{cid}.in"

REPLAY_TOOL_RETURN_MAX_CHARS: Final[int] = 6000
"""Per-tool-return character cap applied to replayed (hydrated) tool
returns before they re-enter an agent's ``message_history``.

Deliberately distinct from — and much larger than — the 2000-char Discord
display budget the steps renderer targets: the Discord cap is a *display*
budget, while this one bounds the *LLM context* a replayed turn re-injects.
Reusing a display-sized render cap would lobotomize the tool context the
model gets to reason over on the next turn (the whole point of replay).
Only oversized individual ``str``
tool returns are trimmed; ``history_turns`` already bounds how many turns
replay can touch (plan §4: "``history_turns`` bounds replay; no backstop"
and §11 Q-5: this is the lever that keeps the hydrated envelope under the
broker's max message size)."""

_REPLAY_TRUNCATION_MARKER: Final[str] = "\n…(truncated)"
"""Visible marker appended to a tool return truncated to
:data:`REPLAY_TOOL_RETURN_MAX_CHARS` so the model can tell the content was
cut rather than genuinely short."""


def _truncate_replay_tool_returns(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Cap oversized tool-return payloads in a replayed delta.

    Walks ``messages`` (the deserialized structured slice of a prior turn)
    and, for every :class:`BaseToolReturnPart` (so both plain
    :class:`ToolReturnPart` *and* :class:`BuiltinToolReturnPart`) whose
    model-facing string — ``part.model_response_str()`` — exceeds
    :data:`REPLAY_TOOL_RETURN_MAX_CHARS`, replaces the part with an
    immutable copy whose ``content`` is the truncated string (plus
    :data:`_REPLAY_TRUNCATION_MARKER`). This catches the cases the old
    ``str``-only check missed: a non-``str`` ``content`` (a structured
    payload) that serializes large, and ``BuiltinToolReturnPart``. Swapping
    a structured ``content`` for a truncated ``str`` is acceptable because
    the model only ever sees ``model_response_str()`` regardless — and a
    ``str`` round-trips through that method unchanged.

    Already-small returns are left untouched; messages with no oversized
    tool returns are returned unchanged (the same object), and a message
    that does need trimming is rebuilt with :func:`dataclasses.replace` on
    the affected parts so the deserialized objects are never mutated in
    place.

    Returns a NEW list — callers can splice it into the hydration map
    without worrying about aliasing the input. The non-mutating contract
    matters because the deserialized parts may be shared once the
    type-adapter caches anything, and because :func:`project_history`
    treats its hydration deltas as read-only.
    """
    out: list[ModelMessage] = []
    for msg in messages:
        new_parts: list[Any] | None = None
        parts = msg.parts
        for idx, part in enumerate(parts):
            if isinstance(part, BaseToolReturnPart):
                rendered = part.model_response_str()
                if len(rendered) > REPLAY_TOOL_RETURN_MAX_CHARS:
                    if new_parts is None:
                        new_parts = list(parts)
                    head = REPLAY_TOOL_RETURN_MAX_CHARS - len(_REPLAY_TRUNCATION_MARKER)
                    truncated = rendered[: max(head, 0)] + _REPLAY_TRUNCATION_MARKER
                    new_parts[idx] = dataclasses.replace(part, content=truncated)
        if new_parts is None:
            out.append(msg)
        else:
            out.append(dataclasses.replace(msg, parts=new_parts))
    return out


class BridgeIngress:
    """Publish inbound Discord events as agent invocations.

    Fire-and-forget. The eventual reply (or replies — multi-agent flows
    are the point of the migration) is posted by the outbox consumer,
    not by this class. The slash command path and the ambient-message
    path share this single handler.
    """

    def __init__(
        self,
        calfkit_client: Client,
        registry: AgentRegistry,
        pending_wires: PendingWires,
        *,
        default_provider: Provider = DEFAULT_PROVIDER,
        ingress_topic_template: str = _DEFAULT_INGRESS_TOPIC_TEMPLATE,
    ) -> None:
        self._client = calfkit_client
        self._registry = registry
        self._pending_wires = pending_wires
        self._default_provider = default_provider
        self._ingress_topic_template = ingress_topic_template
        # The history fetcher is injected via :meth:`set_fetcher` after
        # the gateway client connects (its WebSocket-paired
        # :class:`discord.Client` isn't usable until ``_on_ready`` fires).
        # ``None`` is the documented pre-ready state — :meth:`handle`
        # degrades gracefully to empty history when the fetcher isn't
        # set yet so a Discord event arriving in the brief window
        # before ``_on_ready`` doesn't crash the invocation path.
        self._fetcher: ChannelHistoryFetcher | None = None
        # The transcript store is injected via :meth:`set_transcript_store`
        # once the bridge's SQLite connection is open (gateway ``main()``,
        # right after the ``async with TranscriptStore(...)`` enters).
        # ``None`` is the documented pre-ready state — the slash history
        # builder degrades to today's no-replay projection when the store
        # isn't set yet, so a Discord event arriving before the store opens
        # never crashes the invocation path. Mirrors :attr:`_fetcher`.
        self._transcript_store: TranscriptStoreLike | None = None
        # Whether the memory-prompt load has already failed once this process —
        # gates the one-shot error log in :meth:`_memory_prompt_deps` (re-armed
        # on a later successful load). Same late-bound-state idiom as the two
        # fields above.
        self._memory_prompt_load_failed = False
        # Validate every agent's provider at boot so a typo'd
        # CALFKIT_AGENT_DEFAULT_PROVIDER surfaces here (fail-fast) rather
        # than as an uncaught ValueError inside every targeted invocation.
        for spec in registry.all():
            resolve_provider(spec, default_provider=default_provider)
        # Symmetrically validate that every `tools:` reference in every
        # .md resolves against TOOL_REGISTRY. The agent runner runs the same
        # check at its own boot, but the bridge usually starts first in dev —
        # surfacing typos here gives operators a single actionable error before
        # any agent process boots. (``mcp/...`` selectors are rejected far
        # earlier, at parse time in ``AgentDefinition._validate_tools``, so a
        # stray one would already be an unknown-tool reference here.)
        # Lazy import: `calfcord.tools` transitively imports bridge code, so a
        # top-level import would cycle at boot.
        from calfcord.tools import TOOL_REGISTRY

        unknown: list[tuple[str, str]] = []
        for spec in registry.all():
            # ``spec.tools is None`` means "all registered tools" — the
            # loader's default-resolution sentinel for assistants whose
            # frontmatter omits the ``tools:`` line. Nothing to validate
            # (every name comes from TOOL_REGISTRY by construction).
            if spec.tools is None:
                continue
            for tool_name in spec.tools:
                if tool_name not in TOOL_REGISTRY:
                    unknown.append((spec.agent_id, tool_name))
        if unknown:
            known = sorted(TOOL_REGISTRY)
            entries = ", ".join(f"{aid!r} declares {tname!r}" for aid, tname in unknown)
            raise ValueError(
                f"bridge boot found unknown tool references: {entries}; known tools: {known or '<none registered>'}"
            )

    def set_fetcher(self, fetcher: ChannelHistoryFetcher) -> None:
        """Inject the channel-history fetcher.

        Called by the gateway from ``_on_ready`` once the underlying
        :class:`discord.Client` has authenticated. Until this is called,
        :meth:`handle` runs without history (returns empty
        ``message_history``); after, history is fetched per invocation.

        Idempotent — calling again replaces the fetcher. Useful for
        tests that swap in a fake.
        """
        self._fetcher = fetcher

    def set_transcript_store(self, store: TranscriptStoreLike) -> None:
        """Inject the bridge-local transcript store for tool-call replay.

        Called by the gateway's ``main()`` once the store's SQLite
        connection is open. Until then the slash-history builder
        (:meth:`_build_slash_message_history`) runs without hydration —
        producing the same projection as before the replay feature —
        rather than crashing on a missing store. After injection, each
        slash invocation joins this turn's surviving self-reply records
        against the store and splices their persisted tool calls/returns
        into the projected ``message_history``.

        Idempotent — calling again replaces the store. Mirrors
        :meth:`set_fetcher`; useful for tests that swap in a fake store.
        """
        self._transcript_store = store

    async def handle(
        self,
        wire: WireMessage,
        *,
        prefetched_history: Sequence[HistoryRecord] | None = None,
    ) -> None:
        """Publish ``wire`` to the right ingress topic for its kind.

        Slash wires (``kind="slash"``) — including real Discord
        slashes, parsed @-mentions, and the router's synthesized
        fan-out wires — go to the per-channel ingress topic. Ambient
        wires (``kind="message"``) go to the router's ambient ingress
        with the wire packed onto ``deps`` so the router's
        fan-out consumer can recover it from ``result.deps``.

        ``prefetched_history`` is populated only by the synthesized-in
        consumer (:mod:`calfcord.bridge.synthesized`) which
        reads the history off ``deps["history"]`` and forwards it
        here. This avoids a redundant per-target Discord fetch when one
        ambient publish fans out to multiple agents — the bridge made
        a single fetch at ambient publish time, packed it onto
        ``deps``, and the synthesized-in consumer hands the same raw
        records to each target's invocation here. The per-agent POV
        projection happens locally in this method.

        Only the slash branch writes to :class:`PendingWires` (before
        publishing, so a fast agent reply can never race the
        outbox's lookup). The ambient branch deliberately does NOT
        populate ``PendingWires``: the router's terminal reply is
        suppressed (ambient sends use ``reply_to=None``) and the
        original ``event_id`` is never looked up; the synthesized wires
        the fan-out generates each carry their own fresh ``event_id``,
        and those are what the outbox correlates on. See the inline
        comment at the ambient branch for the LRU-eviction failure mode
        that skipping prevents.

        Per-call ``model_settings`` resolution: when
        ``wire.slash_target`` is set, we look up the target agent's
        current ``thinking_effort`` in the registry and build a
        provider-specific override. Ambient messages send no override
        (the router's effort comes from its own definition).

        Both branches publish with :meth:`Client.send`, which registers
        **no** reply future — there is nothing to cancel and nothing to
        leak. The slash branch names ``discord.outbox`` as the
        ``reply_to`` return address so the agent's terminal reply lands
        there; the ambient branch uses ``reply_to=None`` (the router's
        decision flows forward on its ``publish_topic``, not back to us).
        Either way the bridge's egress is the outbox consumer in a
        different consumer group, so the actual reply is still observed.
        """
        # Build the phonebook fresh on every invocation so any future
        # hot-add on the registry takes effect immediately. The same
        # phonebook is used twice: locally to compute temp_instructions,
        # and serialized into deps so decoupled deployments (e.g. the
        # tools runner) can do persona lookups and peer-roster building
        # without needing local file access to agents/*.md.
        phonebook = phonebook_from_registry(self._registry)

        if wire.kind == "message":
            # Ambient (non-@mention) messages go unanswered: the ambient
            # router that used to choose an addressee has been removed (C2).
            # Accept the wire and drop it — there is no agent to route it to.
            logger.debug(
                "dropping ambient message event_id=%s channel=%s (ambient routing removed)",
                wire.event_id,
                wire.channel_id,
            )
        else:  # wire.kind == "slash"
            model_settings = self._resolve_model_settings(wire)
            temp_instructions = self._resolve_temp_instructions(wire, phonebook)
            message_history = await self._build_slash_message_history(wire, prefetched_history)
            # Load-bearing ordering: ``put`` MUST precede the
            # ``send`` publish. The outbox consumer reads
            # ``PendingWires`` keyed on ``correlation_id`` (= wire
            # ``event_id``) to recover the original channel/message/
            # author context for its Discord post. A fast assistant
            # reply could race the consumer's lookup if we published
            # first and then ``put``. This invariant also matters
            # for synthesized wires arriving via
            # :mod:`bridge.synthesized`: the synthesized-in consumer
            # calls ``ingress.handle(wire)`` here, and the same
            # ``put``-before-publish order is what keeps the
            # synthesized wire findable by the outbox the moment the
            # synthesized assistant replies.
            # Snapshot the per-invocation context into the
            # PendingEntry so the outbox can rebuild a faithful retry
            # envelope on Discord-post failure. All snapshot fields
            # mirror what we're about to pass to ``send``:
            # the projected ``message_history`` (frozen as a tuple),
            # the peer-roster ``temp_instructions``, and the per-call
            # ``model_settings`` (provider-specific thinking-effort).
            # Without snapshotting ``model_settings``, a retry would
            # silently drop the operator-configured effort tier and
            # run at the model client's bake-in default.
            self._pending_wires.put(
                wire.event_id,
                PendingEntry(
                    wire=wire,
                    message_history=tuple(message_history),
                    initial_message_history_length=len(message_history),
                    temp_instructions=temp_instructions,
                    model_settings=model_settings,
                ),
            )
            try:
                await self._client.send(
                    user_prompt=wire.content,
                    topic=self._ingress_topic_template.format(cid=wire.channel_id),
                    reply_to=DISCORD_OUTBOX_TOPIC,
                    correlation_id=wire.event_id,
                    deps={
                        "discord": wire.model_dump(mode="json"),
                        "phonebook": phonebook_to_deps(phonebook),
                        **self._memory_prompt_deps(),
                    },
                    model_settings=model_settings,
                    temp_instructions=temp_instructions,
                    message_history=message_history,
                )
            except Exception:
                # Publish failed; the agent will not run, so no reply
                # will ever look up this wire. Free the slot.
                self._pending_wires.pop(wire.event_id)
                logger.exception(
                    "ingress slash publish failed event_id=%s channel=%s slash_target=%s",
                    wire.event_id,
                    wire.channel_id,
                    wire.slash_target,
                )
                raise

    async def _build_slash_message_history(
        self,
        wire: WireMessage,
        prefetched_history: Sequence[HistoryRecord] | None,
    ) -> list[ModelMessage]:
        """Build the ``message_history`` list for a slash invocation.

        Two record-source paths:

        * ``prefetched_history is not None`` — synthesized-in path. The
          ambient → router → fan-out chain has already fetched the
          channel history (eagerly, at ambient publish time) and packed
          it onto ``deps["history"]``. The synthesized-in
          consumer hands those records here so we don't refetch once
          per fan-out target. An empty tuple is a legitimate "no
          records" value — *not* a signal to refetch.
        * ``prefetched_history is None`` — direct slash / @-mention.
          Fetch now via :class:`ChannelHistoryFetcher`. If the fetcher
          hasn't been injected yet (pre-ready), fall back to empty.

        The records are then trimmed to the target agent's
        ``history_turns`` and projected from its POV. Returns a list
        (possibly empty) of :class:`ModelMessage`.
        """
        target = wire.slash_target
        if target is None:
            # Defensive: the slash branch is only entered when
            # wire.kind == "slash", and the wire schema enforces a
            # non-None slash_target in that case. Belt-and-suspenders
            # against a future code path that forgets the invariant.
            return []
        spec = self._registry.by_id(target)
        if spec is None:
            # The unknown-target case is already ERROR-logged by
            # :meth:`_resolve_model_settings`; don't double-log.
            return []
        if spec.history_turns <= 0:
            logger.debug(
                "slash history skipped event_id=%s agent=%s: history_turns=0",
                wire.event_id,
                target,
            )
            return []

        if prefetched_history is not None:
            records: Sequence[HistoryRecord] = prefetched_history
        elif self._fetcher is None:
            # Pre-ready window. The gateway hasn't injected the fetcher
            # yet; an event arrived before ``_on_ready`` fired.
            # Degrade gracefully — empty history is better than a
            # broken invocation.
            logger.debug(
                "slash history skipped event_id=%s agent=%s: fetcher not yet injected (pre-_on_ready window)",
                wire.event_id,
                target,
            )
            return []
        else:
            records = await self._fetcher.fetch(
                source_channel_id=wire.source_channel_id or wire.channel_id,
                before_message_id=wire.message_id,
                limit=spec.history_turns,
            )

        if spec.history_turns < len(records):
            records = records[-spec.history_turns :]
        hydration = await self._build_replay_hydration(records, target)
        return project_history(records, self_agent_id=target, hydration=hydration)

    async def _build_replay_hydration(
        self,
        records: Sequence[HistoryRecord],
        target: str,
    ) -> dict[int, list[ModelMessage]] | None:
        """Build the tool-call replay map for ``target``'s slash invocation.

        Joins — never DB-scans — the already-fetched (and ``/clear``-
        truncated) ``records`` against the transcript store: only records
        that are THIS agent's own replies (``author_agent_id == target``)
        are candidates, so a ``/clear`` that truncated a reply out of
        ``records`` simply means it is never looked up (plan §4: "replay
        is a join against fetcher output, never a DB scan"; the read scope
        is the surviving record set, which keeps ``/clear`` correct for
        free). Each found row's ``delta_json`` is deserialized and its
        oversized tool returns trimmed to
        :data:`REPLAY_TOOL_RETURN_MAX_CHARS` before it enters the map.

        Returns ``None`` (⇒ :func:`project_history` runs its pre-replay
        behavior) when the store has not been injected yet (pre-ready
        window) or when none of this turn's own-reply records have a
        persisted transcript row. Otherwise returns
        ``{int(final_message_id) -> trimmed delta messages}`` keyed to
        match :attr:`HistoryRecord.message_id`. Best-effort: any failure
        deserializing or trimming a single row is logged and that row is
        skipped, so a corrupt blob degrades to "no replay for that reply"
        rather than breaking the whole invocation.
        """
        store = self._transcript_store
        if store is None:
            return None
        self_reply_ids = [r.message_id for r in records if r.author_agent_id == target]
        if not self_reply_ids:
            return None
        # The store keys ``final_message_id`` as TEXT (snowflake
        # precision); join on the str form and map back to int to match
        # ``HistoryRecord.message_id`` in :func:`project_history`.
        rows = await store.get_by_final_message_ids([str(mid) for mid in self_reply_ids])
        if not rows:
            return None
        hydration: dict[int, list[ModelMessage]] = {}
        for final_message_id_str, row in rows.items():
            try:
                msgs = list(ModelMessagesTypeAdapter.validate_json(row.delta_json))
                msgs = _truncate_replay_tool_returns(msgs)
            except Exception:
                # A single unparseable / malformed blob must not sink the
                # whole invocation: skip just this reply's replay. The
                # row was written by the outbox from a live turn, so this
                # is unexpected — log loudly for investigation.
                logger.exception(
                    "replay hydration skipped reply_id=%s agent=%s: failed to "
                    "deserialize/trim stored delta; this turn keeps only its "
                    "final text in replay",
                    final_message_id_str,
                    target,
                )
                continue
            hydration[int(final_message_id_str)] = msgs
        return hydration or None

    def _memory_prompt_deps(self) -> dict[str, str]:
        """Return the memory-prompt ``deps`` entry, or ``{}`` when not applicable.

        The bridge is the single reader of the memory-prompt template. It ships
        the raw (un-localized) template under
        :data:`~calfcord.agents.memory.MEMORY_PROMPT_DEPS_KEY`
        whenever the registry holds at least one memory-enabled agent — so it
        reaches every agent and propagates through A2A (``private_chat``
        forwards ``deps``), and each memory agent's instructions hook localizes
        it. Returns ``{}`` when:

        * no agent opts into memory (existing deployments stay byte-identical,
          no template read, no wire cost); or
        * the template can't be loaded (a bad ``CALFCORD_MEMORY_PROMPT_PATH``);
          the error is logged once and memory agents degrade to no memory block
          rather than the bridge failing every invocation. The loader re-reads
          on failure, so a fixed path self-heals on the next invocation (no
          restart needed); the recovery is logged once and the one-shot error
          log re-arms.
        """
        try:
            deps = memory_prompt_deps_for_registry(self._registry.all())
        except ValueError as exc:
            if not self._memory_prompt_load_failed:
                self._memory_prompt_load_failed = True
                logger.error(
                    "failed to load the memory prompt (%s); memory-enabled agents "
                    "will run without their memory instructions until it loads "
                    "successfully",
                    exc,
                    exc_info=True,
                )
            return {}
        # ``deps`` is empty when no agent opted into memory: nothing was loaded, so
        # leave the one-shot error state untouched (there is no recovery to report).
        if deps and self._memory_prompt_load_failed:
            self._memory_prompt_load_failed = False
            logger.info("memory prompt loaded successfully; memory instructions restored")
        return deps

    def _resolve_temp_instructions(
        self,
        wire: WireMessage,
        phonebook: list[PhonebookEntry],
    ) -> str | None:
        """Compute the per-call ``temp_instructions`` for ``wire``.

        Slash-only: the ambient branch builds router-specific
        ``temp_instructions`` via
        :func:`~calfcord.router.roster.build_router_temp_instructions`
        in :meth:`_publish_ambient` and never reaches this method. The
        ``target is None`` guard remains as belt-and-suspenders so any
        future caller that bypasses the kind-branch fails closed (no
        roster) instead of crashing.
        """
        target = wire.slash_target
        if target is None:
            return None
        if not any(e.agent_id == target for e in phonebook):
            # Mirrors the symmetric log in :meth:`_resolve_model_settings`
            # below — both ``build_temp_instructions`` and the model-settings
            # resolver silently degrade when the target isn't in the
            # phonebook/registry, so the operator-actionable signal has to
            # live at the call site. The two most plausible causes are a
            # registry hot-mutation between normalize and publish, or a
            # future regression where a router-role agent slips past the
            # phonebook filter and becomes a ``slash_target``.
            logger.error(
                "slash_target=%r missing from phonebook event_id=%s; "
                "agent will run without peer roster or @-mention rules",
                target,
                wire.event_id,
            )
        return build_temp_instructions(phonebook, target, channel=True)

    def _resolve_model_settings(self, wire: WireMessage) -> dict[str, Any] | None:
        """Compute per-call ``model_settings`` for ``wire``, or ``None``.

        Reads the target agent's current ``thinking_effort`` from the
        in-memory registry (kept fresh by
        :meth:`AgentRegistry.set_thinking_effort`). Returns ``None`` for
        ambient messages and for any error — the agent then falls back
        to whatever was baked into its model client at boot.
        """
        target = wire.slash_target
        if target is None:
            return None

        spec = self._registry.by_id(target)
        if spec is None:
            logger.error(
                "slash_target=%r missing from registry event_id=%s; operator effort tier will not apply",
                target,
                wire.event_id,
            )
            return None

        try:
            provider = resolve_provider(spec, default_provider=self._default_provider)
            return build_model_settings(provider, spec.thinking_effort)
        except ValueError as e:
            # Boot validates the steady-state provider for every agent
            # in :meth:`BridgeIngress.__init__` — a runtime
            # ``ValueError`` here therefore means env/registry drift
            # after boot, which is operator-actionable. ERROR (not
            # WARN) so it alerts; falling back silently to
            # model-client defaults would degrade answer quality for
            # a user who asked for ``thinking_effort=high``.
            logger.error(
                "model_settings resolution failed for agent=%s event_id=%s "
                "cause=%s; falling back to model client defaults — the "
                "agent will run at its baked-in thinking effort, NOT the "
                "operator-configured tier",
                target,
                wire.event_id,
                type(e).__name__,
                exc_info=True,
            )
            return None
