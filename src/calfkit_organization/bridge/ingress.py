"""Discord → calfkit publish path: fire-and-forget agent invocation.

The bridge publishes the normalized :class:`WireMessage` to the right
ingress topic based on ``wire.kind`` and returns immediately. Replies
land on ``discord.outbox`` and are posted by the outbox consumer
(:mod:`calfkit_organization.bridge.outbox`) — every reply, not just the
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
  (``discord.ambient.in``) with the original wire packed into
  ``state.metadata`` via
  :func:`calfkit_organization._compat.invoke.invoke_node_with_metadata`.
  The router decides which assistants should respond and its fan-out
  consumer republishes synthesized ``kind="slash"`` wires through
  ``bridge.synthesized.in``, which loops back to this class's
  :meth:`handle` via the bridge's synthesized-in consumer.

Two pieces of state cross the ingress→egress boundary:

* ``deps={"discord": wire}`` rides on the envelope so the agent's gates
  (:mod:`calfkit_organization.agents.gates`) can read it. The dep
  survives the agent's :class:`ReturnCall` republish into
  ``discord.outbox`` because :meth:`BaseNodeDef._publish_action` carries
  ``envelope.context.deps`` forward.
* The same wire is also written to a process-local :class:`PendingWires`
  map keyed on ``correlation_id``. The outbox consumer reads this map
  to recover the channel id / message id / author info it needs for
  the Discord post — :class:`~calfkit.NodeResult` doesn't expose
  ``Envelope.context.deps``. See :mod:`pending_wires` for the rationale.

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

import logging
from typing import Any

import uuid_utils
from calfkit.client import Client, InvocationHandle

from calfkit_organization._compat.invoke import (
    MetadataEnvelope,
    invoke_node_with_metadata,
)
from calfkit_organization.agents.definition import Provider
from calfkit_organization.agents.factory import DEFAULT_PROVIDER, resolve_provider
from calfkit_organization.agents.peer_roster import build_temp_instructions
from calfkit_organization.agents.phonebook import (
    PhonebookEntry,
    phonebook_from_registry,
    phonebook_to_deps,
)
from calfkit_organization.agents.thinking import build_model_settings
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireMessage
from calfkit_organization.router.roster import build_router_temp_instructions
from calfkit_organization.topics import (
    AMBIENT_INGRESS_TOPIC as _AMBIENT_INGRESS_TOPIC,
)
from calfkit_organization.topics import (
    AMBIENT_REPLY_DISCARD_TOPIC as _AMBIENT_REPLY_DISCARD_TOPIC,
)

logger = logging.getLogger(__name__)

_DEFAULT_INGRESS_TOPIC_TEMPLATE = "discord.channel.{cid}.in"


class AmbientRosterEmptyError(ValueError):
    """Raised when an ambient publish is aborted because the registry
    has no eligible assistant agents.

    The router LLM run would be useless (it would receive no roster,
    likely hallucinate, and the fan-out's phonebook validation would
    reject every chosen id anyway), and the user would see no reply.
    The gateway catches this specific exception and sends an
    operator-actionable inline reply to the Discord message — making
    the misconfiguration visible to the user instead of silently
    dropping the message.

    Carries the original Discord ``event_id`` and ``channel_id`` for
    the gateway's reply text and any structured-logging downstream.
    """

    def __init__(self, *, event_id: str, channel_id: int) -> None:
        self.event_id = event_id
        self.channel_id = channel_id
        super().__init__(
            f"ambient publish aborted: registry has no eligible "
            f"assistant agents (event_id={event_id!r}, "
            f"channel_id={channel_id})"
        )


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
        # Validate every agent's provider at boot so a typo'd
        # CALFKIT_AGENT_DEFAULT_PROVIDER surfaces here (fail-fast) rather
        # than as an uncaught ValueError inside every targeted invocation.
        for spec in registry.all():
            resolve_provider(spec, default_provider=default_provider)
        # Symmetrically validate that every `tools:` reference in every
        # .md resolves against TOOL_REGISTRY. The agent runner runs the
        # same check at its own boot, but the bridge usually starts first
        # in dev — surfacing typos here gives operators a single
        # actionable error before any agent process boots.
        # Lazy import: `calfkit_organization.tools` transitively imports
        # bridge code, so a top-level import would cycle at boot.
        from calfkit_organization.tools import TOOL_REGISTRY  # noqa: PLC0415

        unknown: list[tuple[str, str]] = []
        for spec in registry.all():
            for tool_name in spec.tools:
                if tool_name not in TOOL_REGISTRY:
                    unknown.append((spec.agent_id, tool_name))
        if unknown:
            known = sorted(TOOL_REGISTRY)
            entries = ", ".join(f"{aid!r} declares {tname!r}" for aid, tname in unknown)
            raise ValueError(
                f"bridge boot found unknown tool references: {entries}; "
                f"known tools: {known or '<none registered>'}"
            )

    async def handle(self, wire: WireMessage) -> None:
        """Publish ``wire`` to the right ingress topic for its kind.

        Slash wires (``kind="slash"``) — including real Discord
        slashes, parsed @-mentions, and the router's synthesized
        fan-out wires — go to the per-channel ingress topic. Ambient
        wires (``kind="message"``) go to the router's ambient ingress
        with the wire packed into ``state.metadata`` so the router's
        fan-out consumer can recover it.

        Only the slash branch writes to :class:`PendingWires` (before
        publishing, so a fast agent reply can never race the
        outbox's lookup). The ambient branch deliberately does NOT
        populate ``PendingWires``: the router's reply lands on the
        discard topic and is never looked up by the original
        ``event_id``; the synthesized wires the fan-out generates
        each carry their own fresh ``event_id``, and those are what
        the outbox correlates on. See the inline comment at the
        ambient branch for the LRU-eviction failure mode that
        skipping prevents.

        Per-call ``model_settings`` resolution: when
        ``wire.slash_target`` is set, we look up the target agent's
        current ``thinking_effort`` in the registry and build a
        provider-specific override. Ambient messages send no override
        (the router's effort comes from its own definition).

        Cancelling the invocation handle's future immediately after
        publish is load-bearing on both branches: :meth:`Client.invoke_node`
        unconditionally registers a pending future with the reply
        dispatcher (the dispatcher will otherwise resolve+pop it on
        the first reply, but a no-reply event would leak ``_pending``
        forever, and a redelivered ``correlation_id`` would raise
        inside ``_ReplyDispatcher.expect``). Cancelling the future
        triggers its ``add_done_callback`` to pop the registry entry
        synchronously. The bridge's egress is the outbox consumer in
        a different consumer group, so the actual reply is still
        observed.
        """
        # Build the phonebook fresh on every invocation so any future
        # hot-add on the registry takes effect immediately. The same
        # phonebook is used twice: locally to compute temp_instructions,
        # and serialized into deps so decoupled deployments (e.g. the
        # tools runner) can do persona lookups and peer-roster building
        # without needing local file access to agents/*.md.
        phonebook = phonebook_from_registry(self._registry)

        if wire.kind == "message":
            # Ambient path. Filter non-human authors before publish:
            # peer-agent webhook chatter without an @-mention must not
            # trigger the router (would risk agent-on-agent reply
            # storms). Agent @-mentions still route normally because
            # the normalizer classifies on content, producing
            # kind="slash" and entering the branch below.
            if wire.author.is_bot or wire.author.is_webhook:
                # Webhook traffic is the documented motivation
                # (recognized agent personas chatting without an
                # @-mention); keep that path at DEBUG since it can
                # be high-volume. An unexpected bot author — flagged
                # as is_bot but NOT a recognized agent webhook —
                # might be a third-party Discord bot or a regression
                # mis-classifying a real human; INFO so it shows up
                # at production baseline log levels.
                is_unrecognized_bot = (
                    wire.author.is_bot
                    and not wire.author.is_webhook
                    and wire.author.agent_id is None
                )
                level = logging.INFO if is_unrecognized_bot else logging.DEBUG
                logger.log(
                    level,
                    "skipping ambient publish for non-human author "
                    "event_id=%s author=%s is_bot=%s is_webhook=%s "
                    "(un-addressed agent chatter does not route)",
                    wire.event_id,
                    wire.author.display_name,
                    wire.author.is_bot,
                    wire.author.is_webhook,
                )
                return
            # NOTE: we do NOT populate pending_wires on the ambient
            # branch. The router's reply lands on the discard topic
            # and is never looked up by event_id; the synthesized
            # wires fan-out generates each get their own fresh
            # event_id (and that's what the outbox correlates on).
            # Inserting here would waste an LRU slot per ambient
            # message and could evict legitimate slash entries under
            # load.
            try:
                handle = await self._publish_ambient(wire, phonebook)
            except AmbientRosterEmptyError:
                # ``_publish_ambient`` already logged the
                # operator-actionable ERROR identifying the empty
                # roster as the cause. Re-raise without the
                # ``logger.exception`` stack trace — the empty-roster
                # case is a deployment-config rejection, not a
                # broker/runtime failure, so the trace would only
                # add noise. The gateway catches this specific type
                # and surfaces the misconfiguration to the user.
                raise
            except Exception:
                logger.exception(
                    "ingress ambient publish failed event_id=%s channel=%s",
                    wire.event_id,
                    wire.channel_id,
                )
                raise
        else:  # wire.kind == "slash"
            model_settings = self._resolve_model_settings(wire)
            temp_instructions = self._resolve_temp_instructions(wire, phonebook)
            # Load-bearing ordering: ``put`` MUST precede the
            # ``invoke_node`` publish. The outbox consumer reads
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
            self._pending_wires.put(wire.event_id, wire)
            try:
                handle = await self._client.invoke_node(
                    user_prompt=wire.content,
                    topic=self._ingress_topic_template.format(cid=wire.channel_id),
                    correlation_id=wire.event_id,
                    deps={
                        "discord": wire.model_dump(mode="json"),
                        "phonebook": phonebook_to_deps(phonebook),
                    },
                    output_type=str,
                    model_settings=model_settings,
                    temp_instructions=temp_instructions,
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

        handle._future.cancel()

    async def _publish_ambient(
        self,
        wire: WireMessage,
        phonebook: list[PhonebookEntry],
    ) -> InvocationHandle[Any]:
        """Publish an ambient wire to the router's ingress topic.

        Uses :func:`invoke_node_with_metadata` to pack the original
        wire into ``state.metadata`` — calfkit's stock
        :meth:`Client.invoke_node` doesn't expose a ``metadata``
        parameter, and the router's fan-out consumer (a stock
        ``@consumer``) needs to recover the wire from ``NodeResult``
        which doesn't expose ``deps``. The helper is documented in
        :mod:`calfkit_organization._compat.invoke` as a temporary
        workaround until upstream calfkit exposes ``deps`` on
        ``NodeResult`` (or accepts ``metadata=`` on ``invoke_node``).

        The wire is ALSO included in ``deps={"discord": ...}`` so the
        downstream synthesized-assistant chain (which goes through
        :meth:`handle` again with ``kind="slash"``) sees it in the
        usual place. This mirrors how slashes carry the wire in deps;
        the synthesized-in consumer reuses :meth:`handle`'s slash
        branch unchanged, and that branch reads from deps.

        Operator-side health signal: we log INFO at every ambient
        publish. The synthesized-in consumer (in
        :mod:`bridge.synthesized`) logs INFO on every arrival, so
        correlating those streams reveals a silent router. Per-reply
        WARN tracking is deferred for v1 (see module docstring).
        """
        # ``wire_dict`` / ``phonebook_dict`` are kept as locals
        # because the deps channel below carries the JSON-shaped
        # projection (deps is a plain dict on the publish envelope,
        # not a typed model). The MetadataEnvelope itself is built
        # from the typed instances and pydantic handles the JSON dump
        # at ``envelope.model_dump(mode="json")`` time — no duplicate
        # serialization, but no conflation between the two channels
        # either.
        wire_dict = wire.model_dump(mode="json")
        phonebook_dict = phonebook_to_deps(phonebook)
        temp_instructions = build_router_temp_instructions(phonebook)
        if temp_instructions is None:
            # An empty roster is a deployment misconfiguration (no
            # assistants registered). Publishing anyway would burn
            # LLM tokens on a router run with no roster to draw from,
            # the LLM would either return an empty list (silent
            # drop, indistinguishable from a normal "ignore" decision)
            # or hallucinate ids that the fan-out then rejects via
            # phonebook validation — either way no user reply. ERROR
            # log here names the symptom (this specific ambient
            # message will go unanswered);
            # :func:`build_router_temp_instructions` already WARNs on
            # the registry-shape side. Raising
            # :class:`AmbientRosterEmptyError` lets the gateway
            # surface the misconfiguration to the user with an
            # inline reply rather than silently dropping the message.
            logger.error(
                "ambient publish aborted: empty router roster "
                "event_id=%s channel=%s — registry has no eligible "
                "respondents; check that at least one non-router agent "
                "is registered.",
                wire.event_id,
                wire.channel_id,
            )
            raise AmbientRosterEmptyError(
                event_id=wire.event_id, channel_id=wire.channel_id
            )
        logger.info(
            "ingress ambient publish event_id=%s channel=%s topic=%s",
            wire.event_id,
            wire.channel_id,
            _AMBIENT_INGRESS_TOPIC,
        )
        # MetadataEnvelope accepts typed instances directly — pydantic
        # validates on construction (no-op for already-validated
        # models) and serializes through to JSON via
        # ``envelope.model_dump(mode="json")`` below. The wire shape on
        # the Kafka envelope is identical to the pre-typed
        # implementation.
        envelope = MetadataEnvelope(wire=wire, phonebook=tuple(phonebook))
        # Use a FRESH correlation_id (not ``wire.event_id``) for the
        # ambient publish. The router's reply lands on the discard
        # topic and is never looked up by event_id, and the fan-out
        # mints its own fresh event_ids for each synthesized wire —
        # so nothing downstream needs to correlate to the original.
        # Using ``wire.event_id`` here was an avoidable collision
        # risk against the calfkit reply dispatcher's pending-future
        # map: a Discord redelivery (after a gateway reconnect)
        # could re-enter the dispatcher with the same id before our
        # ``cancel()`` had popped the prior entry, causing
        # ``_ReplyDispatcher.expect`` to raise. A fresh uuid7
        # decouples this path from the wire's identity entirely.
        return await invoke_node_with_metadata(
            self._client,
            user_prompt=wire.content,
            topic=_AMBIENT_INGRESS_TOPIC,
            reply_topic=_AMBIENT_REPLY_DISCARD_TOPIC,
            metadata=envelope.model_dump(mode="json"),
            deps={
                "discord": wire_dict,
                "phonebook": phonebook_dict,
            },
            temp_instructions=temp_instructions,
            correlation_id=uuid_utils.uuid7().hex,
        )

    def _resolve_temp_instructions(
        self,
        wire: WireMessage,
        phonebook: list[PhonebookEntry],
    ) -> str | None:
        """Compute the per-call ``temp_instructions`` for ``wire``.

        Slash-only: the ambient branch builds router-specific
        ``temp_instructions`` via
        :func:`~calfkit_organization.router.roster.build_router_temp_instructions`
        in :meth:`_publish_ambient` and never reaches this method. The
        ``target is None`` guard remains as belt-and-suspenders so any
        future caller that bypasses the kind-branch fails closed (no
        roster) instead of crashing.
        """
        target = wire.slash_target
        if target is None:
            return None
        return build_temp_instructions(phonebook, target)

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
                "slash_target=%r missing from registry event_id=%s; "
                "operator effort tier will not apply",
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
