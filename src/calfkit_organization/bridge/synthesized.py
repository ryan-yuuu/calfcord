"""Synthesized-wire consumer — feed router fan-outs back through ingress.

The router's fan-out @consumer
(:mod:`calfkit_organization.router.fanout`) publishes one synthesized
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
:mod:`calfkit_organization.bridge.outbox`. No NodeDef subclass; the
wire rides on ``state.metadata`` (the
:mod:`calfkit_organization._compat.invoke` channel) so a stock
``@consumer`` reads it without needing access to ``deps``.

``output_type`` is left auto-detect (``_UNSET``) because we don't use
``result.output`` — only ``result.state.metadata``. Documented in the
function body so a future reader doesn't add a type and break the
auto-detect contract.
"""

from __future__ import annotations

import logging

from calfkit import ConsumerNodeDef, NodeResult
from calfkit.nodes.consumer import consumer

from calfkit_organization._compat.invoke import (
    MetadataEnvelope,
    raise_envelope_error,
)
from calfkit_organization.bridge.ingress import BridgeIngress
from calfkit_organization.topics import SYNTHESIZED_INGRESS_TOPIC

logger = logging.getLogger(__name__)


DEFAULT_SYNTHESIZED_NODE_ID = "bridge-synthesized-in"


def build_synthesized_consumer(
    ingress: BridgeIngress,
    *,
    subscribe_topic: str = SYNTHESIZED_INGRESS_TOPIC,
    node_id: str = DEFAULT_SYNTHESIZED_NODE_ID,
) -> ConsumerNodeDef:
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
        A :class:`ConsumerNodeDef` ready to register on the bridge's
        :class:`Worker` alongside the outbox consumer. The consumer
        does NOT install the ``final_output_parts`` gate: a fan-out
        publish's envelope carries the wire in ``state.metadata`` but
        does not have ``final_output_parts`` set (it's the initial
        publish, not a ``ReturnCall``), so gating on final-output
        would reject every envelope.
    """

    @consumer(
        subscribe_topics=subscribe_topic,
        node_id=node_id,
    )
    async def _ingest_synthesized(result: NodeResult) -> None:
        try:
            envelope = MetadataEnvelope.extract(result.state.metadata)
        except ValueError as exc:
            # The router fan-out always packs a well-formed envelope
            # (see ``router/fanout.py``). Missing or malformed here —
            # including a wire that fails :class:`WireMessage`
            # validation, since the envelope's ``wire`` field is now
            # typed — means the fan-out contract is broken.
            raise_envelope_error(
                correlation_id=result.correlation_id,
                site="synthesized-in",
                reason=(
                    f"failed to extract MetadataEnvelope from "
                    f"state.metadata: {exc}"
                ),
                cause=exc,
            )

        wire = envelope.wire

        logger.info(
            "synthesized-in arrival event_id=%s channel=%s slash_target=%s",
            wire.event_id,
            wire.channel_id,
            wire.slash_target,
        )

        try:
            await ingress.handle(wire)
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
