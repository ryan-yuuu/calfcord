"""Process-local LRU mapping ``correlation_id → PendingEntry``.

The bridge's ingress (:class:`BridgeIngress`) records inbound wires
before publishing to Kafka; the outbox consumer
(:func:`build_outbox_consumer`) reads them back when an agent reply
lands. The map exists because calfkit's :class:`NodeResult` does not
carry the inbound ``Envelope.context.deps`` — the consumer sees
``output``, ``state``, ``correlation_id``, and ``emitter_*`` but not
the original ``deps={"discord": wire}`` we sent on the way in. Rather
than subclass the consumer to peel the dep off the envelope, we keep
the wire locally in the bridge process since the consumer is colocated
with the ingress.

**What's in a `PendingEntry`** (beyond the bare wire):

* ``wire`` — the original :class:`WireMessage`.
* ``message_history`` — snapshot of what was passed to ``invoke_node``.
  Used by the outbox to reconstruct a faithful retry envelope when a
  Discord post fails with an agent-fixable error (length, etc.).
  Without this snapshot the retry would have to re-fetch + re-project
  channel history at retry time, which (a) costs an extra Discord REST
  call per retry, (b) may pick up new messages that arrived in the
  interim and so represent a different conversation than the original
  invocation responded to.
* ``temp_instructions`` — snapshot of the peer roster (for A2A-enabled
  agents). Forwarded verbatim on retries.
* ``model_settings`` — snapshot of per-call provider model settings
  (e.g. ``thinking_effort``). Forwarded verbatim on retries so a
  user-configured high-effort run doesn't silently degrade to the
  model client's default tier when the agent retries.

``PendingEntry`` is **frozen** — its fields are an immutable snapshot
of the original invocation context. The mutable retry counter is held
in a side-table inside :class:`PendingWires` (``_retry_counts``) so
the entry's type-level immutability is real rather than discipline-
documented. The two structures share an eviction lifecycle: when an
entry leaves ``_entries`` (via :meth:`pop` or LRU eviction), the
corresponding ``_retry_counts`` slot is also removed.

Multiple agents may emit replies for the same correlation_id (the
intent of the consumer-node migration), so :meth:`get` is non-popping
— each agent's reply hits the same entry. Entries leave only by LRU
eviction or by explicit :meth:`pop` from
:class:`BridgeIngress` when its own publish fails.

Capacity defaults to :data:`DEFAULT_CAPACITY`. At typical bridge
traffic this is far above the natural correlation window (a slow
LLM reply usually arrives within seconds). An eviction would only
strand an entry whose agent reply has not yet posted; a WARNING line
at eviction time surfaces it for an operator.

Thread safety: the bridge runs on a single asyncio event loop. All
methods are sync and effectively atomic. Do not share an instance
across event loops or threads without an external lock.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Final

from calfkit._vendor.pydantic_ai.messages import ModelMessage

from calfkit_organization.bridge.wire import WireMessage

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY: Final[int] = 1024


@dataclass(frozen=True)
class PendingEntry:
    """Immutable snapshot of one wire's bridge-local context.

    Stored in :class:`PendingWires` keyed on ``correlation_id`` (which
    equals ``wire.event_id`` for the bridge's slash-publish path).
    The extra fields beyond ``wire`` exist for the outbox's retry-on-
    Discord-error path: the outbox needs to rebuild an invocation
    envelope (history + temp_instructions + model_settings) to send
    the agent back to its inbox for a revised reply.

    Frozen so callsites cannot mutate any field. The mutable retry
    counter (incremented on each retry attempt) lives in a side-table
    on :class:`PendingWires` accessed via
    :meth:`PendingWires.get_retry_count` and
    :meth:`PendingWires.increment_retry`.
    """

    wire: WireMessage
    message_history: tuple[ModelMessage, ...] = field(default_factory=tuple)
    """Snapshot of ``message_history`` passed to the original ``invoke_node``.
    Tuple (not list) for shallow immutability; combined with the
    frozen dataclass, callers cannot accidentally mutate the snapshot."""
    temp_instructions: str | None = None
    """Snapshot of ``temp_instructions`` from the original invocation.
    Forwarded verbatim on retries so the agent's tool affordances
    (peer roster, etc.) stay consistent across attempts."""
    model_settings: dict[str, Any] | None = None
    """Snapshot of the per-call ``model_settings`` (provider-specific
    reasoning / thinking-effort knobs) from the original invocation.
    Forwarded verbatim on retries so a user-configured
    ``thinking_effort=high`` run doesn't silently degrade to the
    model client's baked-in default on the retry. Mutable ``dict``
    type matches calfkit's :meth:`Client.invoke_node` parameter
    signature; the snapshot discipline is enforced by the frozen
    enclosing dataclass — callers see a reference but cannot rebind
    the field."""


class PendingWires:
    """Bounded-LRU ``correlation_id → PendingEntry`` map.

    Maintains a parallel ``_retry_counts`` dict so the entry itself
    can stay frozen while the retry attempt counter mutates in place.
    The two structures share a lifecycle: every operation that
    inserts / pops / evicts an entry also touches ``_retry_counts``.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._capacity = capacity
        self._entries: OrderedDict[str, PendingEntry] = OrderedDict()
        self._retry_counts: dict[str, int] = {}

    def put(self, correlation_id: str, entry: PendingEntry) -> None:
        """Insert (or replace) the entry for ``correlation_id`` and mark it
        most-recently-used.

        Last-writer-wins on a duplicate id. A Discord redelivery (typically
        after a gateway reconnect) carries the same ``message_id`` but may
        carry edited content; the consumer reads the wire to build the
        inline-reply UI, so binding the reply to the stale wire would
        misrepresent what the agent was replying to. On a duplicate, the
        retry counter is **also reset to 0** — a redelivery is a fresh
        invocation from the user's perspective and should not inherit
        the prior wire's mid-retry state. The redelivery is logged at
        INFO so an operator can correlate against the
        ``DiscordIngressGateway`` seen-id LRU if needed.

        If at capacity, evicts the oldest entry and logs a WARNING — its
        agent reply, if any later arrives, will be quietly dropped by the
        consumer (no entry to look up).
        """
        if correlation_id in self._entries:
            logger.info(
                "pending_wires overwriting entry for correlation_id=%s "
                "(redelivery or duplicate event_id); resetting retry counter",
                correlation_id,
            )
            self._entries[correlation_id] = entry
            self._entries.move_to_end(correlation_id)
            self._retry_counts.pop(correlation_id, None)
            return
        self._entries[correlation_id] = entry
        if len(self._entries) > self._capacity:
            evicted_id, _ = self._entries.popitem(last=False)
            self._retry_counts.pop(evicted_id, None)
            logger.warning(
                "pending_wires evicted correlation_id=%s (cap=%d); "
                "any late agent reply for this event will be dropped",
                evicted_id,
                self._capacity,
            )

    def get(self, correlation_id: str) -> PendingEntry | None:
        """Return the entry for ``correlation_id`` or ``None``.

        Non-popping: multiple agents may reply for the same id, and the
        outbox may need to read the same entry across multiple retry
        attempts. ``move_to_end`` on hit so an active multi-agent
        conversation (or an in-progress retry sequence) stays warm
        against background eviction pressure.
        """
        entry = self._entries.get(correlation_id)
        if entry is not None:
            self._entries.move_to_end(correlation_id)
        return entry

    def pop(self, correlation_id: str) -> PendingEntry | None:
        """Remove and return the entry for ``correlation_id`` if present.

        Also removes any retry-counter state for the same key, so the
        two side structures stay synchronized.

        Used by :class:`BridgeIngress` to free a slot when the Kafka
        publish itself fails — the agent will never run, so no reply
        will ever arrive, so the entry would otherwise wait out its
        LRU lifetime for nothing.
        """
        self._retry_counts.pop(correlation_id, None)
        return self._entries.pop(correlation_id, None)

    def get_retry_count(self, correlation_id: str) -> int:
        """Return how many retry attempts have already been triggered.

        Returns ``0`` for entries that have never retried (the normal
        case) and ``0`` for unknown keys (so callers don't need to
        distinguish "no retries yet" from "evicted" for read-only
        observations like "is this a first attempt?"). Callers that
        need to distinguish "evicted" from "no retries yet" should
        check ``get(correlation_id) is None`` separately.
        """
        return self._retry_counts.get(correlation_id, 0)

    def increment_retry(self, correlation_id: str) -> int | None:
        """Atomically increment the retry counter and return the new value.

        Returns ``None`` if the entry has been evicted between the
        outbox's read and its retry decision (the LRU could in
        principle evict an entry under sustained load before its
        retry sequence completes; the outbox treats this as
        "evicted → fall back to chunk-split"). Touch promotes the
        entry's recency so it doesn't get evicted mid-retry-sequence.
        """
        if correlation_id not in self._entries:
            return None
        new_count = self._retry_counts.get(correlation_id, 0) + 1
        self._retry_counts[correlation_id] = new_count
        self._entries.move_to_end(correlation_id)
        return new_count

    def __len__(self) -> int:
        return len(self._entries)


def make_pending_entry(
    wire: WireMessage,
    *,
    message_history: tuple[ModelMessage, ...] = (),
    temp_instructions: str | None = None,
    model_settings: dict[str, Any] | None = None,
) -> PendingEntry:
    """Convenience constructor for tests and callers that don't need
    to spell every kwarg. Equivalent to ``PendingEntry(wire=...)`` but
    accepts only keyword arguments for the snapshot fields, which
    documents the intent at the callsite (the wire is positional;
    everything else is optional snapshot context).
    """
    return PendingEntry(
        wire=wire,
        message_history=message_history,
        temp_instructions=temp_instructions,
        model_settings=model_settings,
    )
