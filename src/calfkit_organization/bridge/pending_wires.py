"""Process-local LRU mapping ``correlation_id → WireMessage``.

The bridge's ingress (:class:`BridgeIngress`) records the inbound wire
before publishing to Kafka; the outbox consumer
(:func:`build_outbox_consumer`) reads it back when an agent reply lands.
The map exists because calfkit's :class:`NodeResult` does not carry the
inbound ``Envelope.context.deps`` — the consumer sees ``output``,
``state``, ``correlation_id``, and ``emitter_*`` but not the original
``deps={"discord": wire}`` we sent on the way in. Rather than subclass
the consumer to peel the dep off the envelope, we keep the wire locally
in the bridge process since the consumer is colocated with the ingress.

Multiple agents may emit replies for the same correlation_id (the
intent of the consumer-node migration), so :meth:`get` is non-popping —
each agent's reply hits the same wire. Entries leave only by LRU
eviction.

Capacity defaults to 1024 — the same bound as
``DiscordIngressGateway._SEEN_MESSAGE_IDS_CAPACITY``. At typical
bridge traffic this is far above the natural correlation window (a
slow LLM reply usually arrives within seconds). An eviction would only
strand a wire whose agent reply has not yet posted; an INFO line at
eviction time surfaces it for an operator.

Thread safety: the bridge runs on a single asyncio event loop. All
methods are sync and effectively atomic. Do not share an instance
across event loops or threads without an external lock.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from calfkit_organization.bridge.wire import WireMessage

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY = 1024


class PendingWires:
    """Bounded-LRU ``correlation_id → WireMessage`` map."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._capacity = capacity
        self._wires: OrderedDict[str, WireMessage] = OrderedDict()

    def put(self, correlation_id: str, wire: WireMessage) -> None:
        """Insert (or replace) the wire for ``correlation_id`` and mark it
        most-recently-used.

        Last-writer-wins on a duplicate id. A Discord redelivery (typically
        after a gateway reconnect) carries the same ``message_id`` but may
        carry edited content; the consumer reads the wire to build the
        inline-reply UI, so binding the reply to the stale wire would
        misrepresent what the agent was replying to. The redelivery is
        logged at INFO so an operator can correlate against the
        ``DiscordIngressGateway`` seen-id LRU if needed.

        If at capacity, evicts the oldest entry and logs a WARNING — its
        agent reply, if any later arrives, will be quietly dropped by the
        consumer (no wire to look up).
        """
        if correlation_id in self._wires:
            logger.info(
                "pending_wires overwriting wire for correlation_id=%s "
                "(redelivery or duplicate event_id)",
                correlation_id,
            )
            self._wires[correlation_id] = wire
            self._wires.move_to_end(correlation_id)
            return
        self._wires[correlation_id] = wire
        if len(self._wires) > self._capacity:
            evicted_id, _ = self._wires.popitem(last=False)
            logger.warning(
                "pending_wires evicted correlation_id=%s (cap=%d); "
                "any late agent reply for this event will be dropped",
                evicted_id,
                self._capacity,
            )

    def get(self, correlation_id: str) -> WireMessage | None:
        """Return the wire for ``correlation_id`` or ``None``.

        Non-popping: multiple agents may reply for the same id. ``move_to_end``
        on hit so an active multi-agent conversation stays warm against
        background eviction pressure.
        """
        wire = self._wires.get(correlation_id)
        if wire is not None:
            self._wires.move_to_end(correlation_id)
        return wire

    def pop(self, correlation_id: str) -> WireMessage | None:
        """Remove and return the wire for ``correlation_id`` if present.

        Used by :class:`BridgeIngress` to free a slot when the Kafka
        publish itself fails — the agent will never run, so no reply
        will ever arrive, so the entry would otherwise wait out its
        LRU lifetime for nothing.
        """
        return self._wires.pop(correlation_id, None)

    def __len__(self) -> int:
        return len(self._wires)
