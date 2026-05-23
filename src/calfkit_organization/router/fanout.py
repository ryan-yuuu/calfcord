"""Fan-out consumer: turn a :class:`RoutingDecision` into N synthesized wires.

Subscribed to ``routing.decisions`` (the router agent's
``publish_topic``). On every router reply whose state carries a final
output, this consumer:

1. Reads the :class:`RoutingDecision` from ``result.output``.
2. Recovers the original :class:`WireMessage` from ``result.state.metadata``
   (the bridge ingress put it there via :func:`invoke_node_with_metadata`).
3. For each chosen ``agent_id`` (after filtering out the router's own id
   defensively), synthesizes a fresh wire with ``kind="slash"`` and
   ``slash_target=<agent_id>`` plus a fresh ``event_id``, and publishes
   it to ``bridge.synthesized.in`` via :func:`invoke_node_with_metadata`
   (so the wire rides on ``state.metadata`` for the bridge's
   synthesized-in consumer to pick up).

The fresh ``event_id`` per chosen agent is load-bearing: the bridge's
ingress writes each wire into the :class:`PendingWires` map keyed on
``event_id``, and the outbox consumer reads back by ``correlation_id``
(which equals the wire's ``event_id``). Two synthesized wires sharing
the same id would collide on the map and the second-arriving agent's
reply would be misattributed to the first's channel/message context.

Built as a closure that captures ``client`` and ``router_agent_id`` —
same shape as :func:`build_outbox_consumer` in
:mod:`calfkit_organization.bridge.outbox`. Use the ``@consumer``
decorator (calfkit's preferred sugar) rather than constructing
:class:`ConsumerNodeDef` directly.
"""

from __future__ import annotations

import logging

import uuid_utils
from calfkit import ConsumerNodeDef, NodeResult
from calfkit.client import Client
from calfkit.models import SessionRunContext
from calfkit.nodes.consumer import consumer

from calfkit_organization._compat.invoke import (
    MetadataEnvelope,
    invoke_node_with_metadata,
    raise_envelope_error,
)
from calfkit_organization.agents.routing import RoutingDecision
from calfkit_organization.topics import SYNTHESIZED_INGRESS_TOPIC

logger = logging.getLogger(__name__)


DEFAULT_FANOUT_NODE_ID = "router-fanout"


def build_fanout_consumer(
    client: Client,
    router_agent_id: str,
    *,
    subscribe_topic: str = "routing.decisions",
    node_id: str = DEFAULT_FANOUT_NODE_ID,
) -> ConsumerNodeDef[RoutingDecision]:
    """Construct the router's fan-out consumer node.

    Args:
        client: Connected calfkit :class:`Client`. The closure uses it
            to publish synthesized wires via
            :func:`invoke_node_with_metadata`.
        router_agent_id: The router's own ``agent_id`` (typically
            :data:`ROUTER_AGENT_ID`). The consumer defensively filters
            this id out of every decision's ``agents`` list — the LLM
            should never pick the router itself, but a misbehaving
            model output should not be allowed to publish a synthesized
            wire targeting the router (which would loop forever
            because the router's ingress topic is ambient, not
            channel-scoped).
        subscribe_topic: Override for tests. Production uses the
            router's ``publish_topic`` (``"routing.decisions"``).
        node_id: Stable consumer-group identifier. Two ``calfkit-router``
            processes would load-balance the fan-out across the topic's
            partitions — not recommended (you'd get half the decisions
            per process) but not catastrophic.

    Returns:
        A :class:`ConsumerNodeDef` ready to register on the
        ``calfkit-router`` :class:`Worker`. Gate filters out
        intermediate hops via the same ``final_output_parts``
        non-emptiness idiom that :mod:`bridge.outbox` uses.
    """

    def _final_output_parts_gate(ctx: SessionRunContext) -> bool:
        # Skip intermediate hops (mid-loop transitions). Same idiom
        # as ``bridge/outbox.py``'s ``_final_output_parts_gate``. The
        # router's ToolOutput path emits exactly one final-output
        # envelope per invocation, so this gate only fires on the
        # terminal decision.
        return bool(ctx.state.final_output_parts)

    @consumer(
        subscribe_topics=subscribe_topic,
        output_type=RoutingDecision,
        node_id=node_id,
        gates=[_final_output_parts_gate],
    )
    async def _fan_out(result: NodeResult[RoutingDecision]) -> None:
        decision = result.output
        if decision is None:
            # Gate should prevent this: a ``final_output_parts``
            # envelope with no parsed output means calfkit's
            # deserializer dropped the value (upstream validation
            # failure / framework bug). Infrastructure contract
            # violation — raise so operators see it at ERROR.
            raise_envelope_error(
                correlation_id=result.correlation_id,
                site="fanout",
                reason="received envelope with no parsed RoutingDecision",
            )

        try:
            envelope = MetadataEnvelope.extract(result.state.metadata)
        except ValueError as exc:
            # The bridge ingress is contractually required to pack a
            # well-formed envelope (see ``_publish_ambient``). A
            # missing/malformed envelope — including a wire that
            # fails :class:`WireMessage` validation, since the
            # envelope's ``wire`` field is now typed — is an infra bug.
            raise_envelope_error(
                correlation_id=result.correlation_id,
                site="fanout",
                reason=(
                    f"failed to extract MetadataEnvelope from "
                    f"state.metadata (agents={decision.agents}): {exc}"
                ),
                cause=exc,
            )

        wire = envelope.wire

        logger.info(
            "fan-out received decision correlation_id=%s channel=%s agents=%s reasoning=%s",
            result.correlation_id,
            wire.channel_id,
            decision.agents,
            decision.reasoning,
        )

        # Fail-closed on missing phonebook. Production producers
        # ALWAYS pack the phonebook on the ambient publish so the
        # fan-out can validate every chosen ``agent_id`` against the
        # publisher's registry snapshot. ``None`` here means an infra
        # bug — not a backward-compat case — and silently skipping
        # validation would let LLM hallucinations through. Raising
        # surfaces the misconfiguration; the Kafka envelope is
        # already ACKed (``AckPolicy.ACK_FIRST``) so this produces an
        # ERROR log, not a redelivery loop.
        if envelope.phonebook is None:
            raise_envelope_error(
                correlation_id=result.correlation_id,
                site="fanout",
                reason=(
                    f"missing phonebook on ambient envelope "
                    f"(agents={decision.agents})"
                ),
            )

        # Build the known-agent set from the publisher's phonebook
        # snapshot. The fan-out lives in a separate process from the
        # bridge and has no registry access of its own; the bridge
        # ships its typed :class:`PhonebookEntry` projection on every
        # ambient publish via ``envelope.phonebook``. Typed access
        # eliminates the legacy isinstance-on-dict-entry guard — every
        # entry is a validated model with a ``str`` ``agent_id``.
        known_agent_ids: set[str] = {e.agent_id for e in envelope.phonebook}

        # NB: ``decision.agents`` is already deduplicated by
        # :class:`RoutingDecision`'s field validator. Self-filter +
        # phonebook validation are the remaining consumer-side
        # filters — both depend on runtime context (``router_agent_id``
        # and the publisher's phonebook) which the schema cannot see.
        for agent_id in decision.agents:
            if agent_id == router_agent_id:
                # Defensive self-filter: the LLM should never pick the
                # router itself, but a misbehaving model could. A
                # synthesized wire targeting the router would loop
                # forever (router's ingress topic accepts every
                # envelope it sees).
                logger.info(
                    "fan-out skipping router's own id event_id=%s",
                    wire.event_id,
                )
                continue

            if agent_id not in known_agent_ids:
                # Unknown agent_id from the LLM (either hallucinated
                # or registry drift since the publisher built the
                # phonebook). Publishing a synthesized wire targeted
                # at a non-existent agent would orphan in
                # ``PendingWires`` until LRU eviction, with no
                # operator signal and no user-visible reply. ERROR
                # log with full context (channel, author,
                # correlation_id, and the agent_ids the LLM had to
                # choose from) so the operator can quickly identify
                # the affected conversation and decide whether the
                # registry needs an update.
                logger.error(
                    "fan-out skipping unknown agent_id=%r event_id=%s "
                    "channel=%s author=%s correlation_id=%s "
                    "known_agents=%s — agent is not in the publisher's "
                    "phonebook (LLM hallucination or registry drift)",
                    agent_id,
                    wire.event_id,
                    wire.channel_id,
                    wire.author.display_name,
                    result.correlation_id,
                    sorted(known_agent_ids),
                )
                continue

            synthesized = wire.model_copy(
                update={
                    "event_id": uuid_utils.uuid7().hex,
                    "kind": "slash",
                    "slash_target": agent_id,
                }
            )
            # Synthesized envelope deliberately omits ``phonebook``:
            # the bridge's slash branch rebuilds deps from its
            # registry on each re-entry, so shipping the projection
            # through this hop would be redundant. Passing the typed
            # ``WireMessage`` directly — pydantic dumps it as part of
            # ``envelope.model_dump(mode="json")`` at the call site.
            #
            # The history records ARE forwarded: the bridge's ambient
            # publish path fetches channel history once at publish
            # time (see ``BridgeIngress._fetch_ambient_history``) and
            # packs it into the parent envelope. Without forwarding
            # here, the synthesized-in consumer would receive an
            # empty ``MetadataEnvelope.history`` (the default), pass
            # ``prefetched_history=()`` to ``BridgeIngress.handle``,
            # and every fan-out assistant would run with no history
            # — defeating the single-fetch-per-fan-out design.
            synth_envelope = MetadataEnvelope(
                wire=synthesized,
                history=envelope.history,
            )

            # ``handle`` lives outside the try so the finally clause
            # below can cancel its future even if a later step (e.g.
            # the success-log) raises. The fire-and-forget cancel is
            # load-bearing: the dispatcher's pending future would
            # otherwise leak; cancelling triggers the
            # ``add_done_callback`` that pops the registry entry.
            # Same idiom as :meth:`BridgeIngress.handle`.
            handle = None
            try:
                handle = await invoke_node_with_metadata(
                    client,
                    user_prompt="",
                    topic=SYNTHESIZED_INGRESS_TOPIC,
                    metadata=synth_envelope.model_dump(mode="json"),
                    correlation_id=synthesized.event_id,
                )
                logger.info(
                    "fan-out published agent=%s event_id=%s channel=%s",
                    agent_id,
                    synthesized.event_id,
                    synthesized.channel_id,
                )
            except Exception as exc:
                # A publish failure for one fan-out target must not
                # block the other targets (a transient broker hiccup
                # would otherwise drop the whole multi-agent reply).
                # Log at ERROR with channel + author so a partial
                # fan-out degradation is correlatable to a user
                # complaint.
                logger.error(
                    "fan-out publish failed agent=%s event_id=%s "
                    "channel=%s author=%s exc_class=%s; continuing with "
                    "other targets",
                    agent_id,
                    synthesized.event_id,
                    synthesized.channel_id,
                    synthesized.author.display_name,
                    exc.__class__.__name__,
                    exc_info=True,
                )
            finally:
                if handle is not None:
                    handle._future.cancel()

    return _fan_out
