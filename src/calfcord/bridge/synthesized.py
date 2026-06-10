"""Synthesized-wire consumer — feed router fan-outs back through ingress.

The router's fan-out @consumer
(:mod:`calfcord.router.fanout`) publishes one synthesized
``kind="slash"`` :class:`WireMessage` per chosen agent to
``bridge.synthesized.in``. This consumer picks each one off the topic
and feeds it to :meth:`BridgeIngress.handle` — the same handler real
Discord events go through, so the assistant chain that follows looks
identical to a directly-invoked slash.

This means :class:`BridgeIngress` remains the single source of truth
for two project-wide invariants:

1. The wire→channel-topic publish (each agent's channel subscriber
   sees exactly the same envelope shape regardless of whether the
   wire originated from Discord directly or from a router fan-out).
2. :class:`PendingWires` population. The outbox consumer reads back
   by ``correlation_id`` (which equals the synthesized wire's
   ``event_id``); the synthesized-in path going through
   :meth:`BridgeIngress.handle` writes the wire into the map exactly
   as a real Discord slash would.

Built as a closure capturing the ingress instance — same structural
shape as :func:`build_outbox_consumer` in
:mod:`calfcord.bridge.outbox`. No NodeDef subclass; the fan-out puts
the synthesized wire and forwarded history on ``deps``, and this
consumer reads them back from ``result.deps`` (calfkit ≥ 0.4.0 exposes
inbound producer deps on ``ConsumerContext.deps`` — the same dict a tool
reads as ``ctx.deps["key"]``).

``output_type`` is left auto-detect (``_UNSET``) because we don't use
``result.output`` — only ``result.deps``. Do not pass ``output_type=``
to the ``@consumer`` decorator below: adding a type would force calfkit
to deserialize the (uninteresting) ``output`` slot, breaking the
auto-detect contract this consumer relies on.
"""

from __future__ import annotations

import logging

from calfkit import ConsumerNode
from calfkit.models import ConsumerContext
from calfkit.nodes.consumer import consumer
from pydantic import ValidationError

from calfcord.ambient_routing import raise_routing_contract_error
from calfcord.bridge.history import history_from_deps
from calfcord.bridge.ingress import BridgeIngress
from calfcord.bridge.wire import WireMessage
from calfcord.topics import SYNTHESIZED_INGRESS_TOPIC

logger = logging.getLogger(__name__)


DEFAULT_SYNTHESIZED_NODE_ID = "bridge-synthesized-in"


def build_synthesized_consumer(
    ingress: BridgeIngress,
    *,
    subscribe_topic: str = SYNTHESIZED_INGRESS_TOPIC,
    node_id: str = DEFAULT_SYNTHESIZED_NODE_ID,
) -> ConsumerNode:
    """Construct the bridge's synthesized-wire consumer.

    Args:
        ingress: The bridge's :class:`BridgeIngress` instance. The
            consumer's closure captures it and calls
            :meth:`BridgeIngress.handle` for every synthesized wire.
        subscribe_topic: Override for tests. Production uses
            :data:`SYNTHESIZED_INGRESS_TOPIC`.
        node_id: Stable consumer-group identifier. Two bridge processes
            running in parallel would load-balance the synthesized
            traffic across this topic's partitions — operators should
            avoid that topology, but it's not catastrophic.

    Returns:
        A :class:`ConsumerNode` ready to register on the bridge's
        :class:`Worker` alongside the outbox consumer. The consumer
        does NOT install the ``final_output_parts`` gate: a fan-out
        publish's envelope carries the wire on ``deps`` but
        does not have ``final_output_parts`` set (it's the initial
        publish, not a ``ReturnCall``), so gating on final-output
        would reject every envelope.
    """

    @consumer(
        subscribe_topics=subscribe_topic,
        node_id=node_id,
    )
    async def _ingest_synthesized(result: ConsumerContext) -> None:
        deps = result.deps
        try:
            wire = WireMessage.model_validate(deps.get("discord"))
        except ValidationError as exc:
            # The router fan-out always packs the synthesized wire under
            # ``deps["discord"]`` (see ``router/fanout.py``). Missing or
            # malformed here — ``model_validate`` raises on both — means
            # the fan-out contract is broken.
            raise_routing_contract_error(
                correlation_id=result.correlation_id,
                site="synthesized-in",
                reason=f"invalid/missing deps['discord']: {exc}",
                cause=exc,
            )

        # The fan-out forwards channel history as an opaque JSON list
        # under ``deps["history"]``; validate it back into typed records
        # for ``BridgeIngress.handle`` via the same ``*_from_deps`` parser
        # the phonebook uses. An absent key (rolling-deploy edge case) is
        # fine — ``history_from_deps([])`` returns an empty tuple ("no
        # history").
        try:
            history = history_from_deps(deps.get("history", []))
        except (ValueError, ValidationError) as exc:
            raise_routing_contract_error(
                correlation_id=result.correlation_id,
                site="synthesized-in",
                reason=f"malformed deps['history']: {exc}",
                cause=exc,
            )

        logger.info(
            "synthesized-in arrival event_id=%s channel=%s slash_target=%s history_records=%d",
            wire.event_id,
            wire.channel_id,
            wire.slash_target,
            len(history),
        )

        # Forward the pre-fetched history through to ingress. The ambient
        # publish path (``BridgeIngress._publish_ambient``) fetches once
        # for the entire fan-out and packs the records into
        # ``deps["history"]``. Re-fetching here per synthesized target
        # would defeat the single-fetch design and cost N extra Discord
        # REST calls for an N-way fan-out. An empty history tuple
        # (rolling-deploy edge case) is fine — ``handle``'s slash branch
        # treats it as "no history" without falling back to a fresh
        # fetch.
        try:
            await ingress.handle(wire, prefetched_history=history)
        except Exception as exc:
            # A single ingress.handle failure must not poison-pill the
            # partition — log + swallow. ERROR level with full
            # context (channel + author) so a transient broker
            # failure during the synthesized publish leaves an
            # operator-actionable trace; without channel_id, the log
            # cannot be correlated back to a user-visible "no reply"
            # complaint.
            logger.error(
                "synthesized-in ingress.handle failed event_id=%s "
                "slash_target=%s channel=%s author=%s exc_class=%s",
                wire.event_id,
                wire.slash_target,
                wire.channel_id,
                wire.author.display_name,
                exc.__class__.__name__,
                exc_info=True,
            )

    return _ingest_synthesized
