"""Process-local LRU mapping ``correlation_id → StepsEntry``.

The bridge's steps consumer
(:func:`calfkit_organization.bridge.steps.build_steps_consumer`) needs
to remember, across the multiple hops of a single agent invocation, which
Discord parent message the steps belong to and how far through the
agent's running ``message_history`` we've already streamed. Holding that
state in a per-correlation entry lets the consumer treat each inbound
envelope as a stateless delta against the cursor.

**What's in a `StepsEntry`:**

* ``parent_channel_id`` / ``parent_message_id`` — the user's original
  Discord message. The thread is created off this message; intermediate
  step posts target ``parent_channel_id`` with ``thread_id``.
* ``persona`` — the agent's own persona (display name + avatar).
  Resolved once on the first hop from :class:`AgentRegistry` and cached
  so subsequent hops can post without a registry lookup.
* ``thread_id`` — populated lazily on the first hop that produces a
  rendered step. ``None`` until then so a pure-text agent reply (no
  intermediates) does not create an empty thread.
* ``history_cursor`` — ``len(state.message_history)`` already processed.
  The consumer advances this on each hop so the next delta is
  ``message_history[history_cursor:]``.

``StepsEntry`` is **mutable** (not frozen) because ``thread_id`` and
``history_cursor`` advance across hops. Entries are popped on the
terminal hop (the one carrying ``state.final_output_parts``), so a
clean run never leaves an entry behind. Bridge restarts strand entries
in-process; the next hop after restart finds nothing and skips with a
DEBUG log.

Thread safety: the bridge runs on a single asyncio event loop and the
steps consumer is single-worker by default
(:attr:`Worker._max_workers`), so all mutations are effectively
serialized. Do not share an instance across event loops or threads
without an external lock.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

from calfkit_organization.discord.persona import Persona

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY: Final[int] = 1024


@dataclass(slots=True)
class StepsEntry:
    """Per-correlation state for an in-flight agent invocation's step stream.

    See module docstring for field rationale. Mutable because
    ``thread_id`` and ``history_cursor`` advance across hops.

    ``slots=True`` catches accidental typo-creates-new-attribute bugs
    (``entry.histroy_cursor = 7`` raises instead of silently shadowing).

    Persona is captured at the first renderable hop and held for the
    correlation's lifetime so transcript identity is stable across hops;
    a registry rename during an in-flight invocation only takes effect on
    the next invocation.
    """

    parent_channel_id: int
    parent_message_id: int
    persona: Persona
    thread_id: int | None = None
    history_cursor: int = 0


class StepsState:
    """Bounded-LRU ``correlation_id → StepsEntry`` map plus a parallel
    bounded set of correlation ids whose terminal hop has already been
    processed.

    Sized at :data:`DEFAULT_CAPACITY` by default — well above the natural
    in-flight window of agent invocations on a single bridge process.
    Eviction would only strand an entry whose terminal hop has not yet
    arrived; a WARNING line at eviction time surfaces it.

    **Why the completed set:** the bridge's outbox path retries an agent
    invocation by re-publishing to ``agent.{aid}.in`` with the **same**
    ``correlation_id`` after a Discord-post failure (see
    :func:`~calfkit_organization.bridge.outbox._publish_retry`). Without
    a completion guard, the steps consumer would see the retry's first
    hop, find no active entry (the original terminal hop already popped
    it), seed a fresh one, and create a second transcript thread off the
    same parent message — the original is now locked and orphaned, the
    new one inherits an even-longer prefix of "already-rendered" history.
    Tracking completed correlation ids in a bounded LRU lets the consumer
    cheaply detect "this is a retry hop; skip the entire steps surface."
    The set is independent from the entries map and has its own capacity
    so completion records don't get pushed out by a burst of unrelated
    new invocations.
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        *,
        completed_capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        if completed_capacity <= 0:
            raise ValueError(
                f"completed_capacity must be positive, got {completed_capacity}"
            )
        self._capacity = capacity
        self._completed_capacity = completed_capacity
        self._entries: OrderedDict[str, StepsEntry] = OrderedDict()
        # ``OrderedDict[..., None]`` rather than ``set`` because we need
        # LRU eviction semantics (oldest completion records age out first)
        # and ``set`` does not preserve insertion order for popping.
        self._completed: OrderedDict[str, None] = OrderedDict()

    def put(self, correlation_id: str, entry: StepsEntry) -> None:
        """Insert (or replace) the entry for ``correlation_id`` and mark it
        most-recently-used.

        Last-writer-wins on a duplicate id. If at capacity, evicts the
        oldest entry and logs a WARNING — its subsequent hops, if any
        later arrive, will be quietly dropped by the consumer (no entry
        to look up).
        """
        if correlation_id in self._entries:
            self._entries[correlation_id] = entry
            self._entries.move_to_end(correlation_id)
            return
        self._entries[correlation_id] = entry
        if len(self._entries) > self._capacity:
            evicted_id, _ = self._entries.popitem(last=False)
            logger.warning(
                "steps_state evicted correlation_id=%s (cap=%d); "
                "any further hops for this invocation will be skipped",
                evicted_id,
                self._capacity,
            )

    def get(self, correlation_id: str) -> StepsEntry | None:
        """Return the entry for ``correlation_id`` or ``None``.

        Touches recency so an active invocation stays warm against
        background eviction pressure.
        """
        entry = self._entries.get(correlation_id)
        if entry is not None:
            self._entries.move_to_end(correlation_id)
        return entry

    def pop_and_mark_completed(self, correlation_id: str) -> StepsEntry | None:
        """Remove the entry for ``correlation_id`` and record completion.

        Called by the consumer on the terminal hop. Adding the id to
        ``_completed`` is what prevents a later outbox-retry hop from
        creating a second transcript thread for the same invocation.
        Bounded by ``completed_capacity`` so a burst of completed
        correlations cannot grow the set without limit; the oldest
        completion records age out first.
        """
        entry = self._entries.pop(correlation_id, None)
        # Always record completion, even when the entry was never created
        # (pure-text reply with no intermediate hops). Otherwise a retry
        # of such an invocation would create a thread the original run
        # never had.
        self._completed[correlation_id] = None
        self._completed.move_to_end(correlation_id)
        while len(self._completed) > self._completed_capacity:
            self._completed.popitem(last=False)
        return entry

    def is_completed(self, correlation_id: str) -> bool:
        """Return ``True`` if this correlation has already passed a terminal hop.

        Used by the consumer's first-hop entry-creation guard to skip
        outbox-retry hops without seeding a duplicate transcript thread.
        Touches recency so a retried correlation stays in the completed
        set long enough to absorb the retry's hops.
        """
        if correlation_id in self._completed:
            self._completed.move_to_end(correlation_id)
            return True
        return False

    def __len__(self) -> int:
        return len(self._entries)
