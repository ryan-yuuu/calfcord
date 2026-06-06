"""First-reply detection over the Discord outbox (host-agnostic, §4.6 / §12.2).

The init wizard's live finish needs to confirm — end to end, over the real
broker — that the agent it just started actually answers. After the user types
``@<agent> hello`` in Discord, that agent's ``ReturnCall`` lands on
``discord.outbox`` (the same topic the bridge posts replies from). This module
watches that topic for the FIRST reply from the *target* agent and returns
``True``, or returns ``False`` on a clean timeout.

Why a calfkit consumer, not a raw subscriber
--------------------------------------------
The replying-agent identity is NOT in the envelope JSON body. ``emitter_node_id``
/ ``emitter_node_kind`` are ``PrivateAttr`` on :class:`SessionRunContext`,
stamped from the Kafka headers ``x-calf-emitter`` / ``x-calf-emitter-kind`` by
the consumer handler's ``_stamp_transport`` and explicitly excluded from
serialization. A raw ``broker.subscriber`` that only decodes the body therefore
*cannot* recover which agent replied. So — exactly like the bridge's own outbox
consumer (:func:`calfcord.bridge.outbox.build_outbox_consumer`) — we register a
:class:`~calfkit.ConsumerNodeDef`, whose handler reads the headers and projects a
:class:`~calfkit.NodeResult` with ``emitter_node_id`` / ``emitter_node_kind``
populated.

The match field (mirrors the bridge's gate set): ``emitter_node_kind == "agent"``
AND ``emitter_node_id == agent_id`` (an agent's ``node_id`` == its ``agent_id``
== its ``.md`` ``name``), behind the same ``final_output_parts`` non-empty gate
the outbox uses to skip intermediate hops (tool completions, mid-loop
transitions).

Isolation from the bridge
--------------------------
The watcher runs in its OWN Kafka consumer group (a per-call uuid node_id) at
``auto_offset_reset="latest"`` (the Worker default), so it never disturbs the
bridge's outbox group nor replays backlog — it sees only replies that arrive
after it joins, which is precisely the reply to the user's ``@<agent> hello``.

Separation of concerns
----------------------
:func:`make_first_reply_node` is the pure, stateless matcher (a
``ConsumerNodeDef`` factory) — unit-testable by driving its handler directly,
no broker. :func:`wait_for_first_reply` is the thin orchestrator that wires the
node onto a transient managed Worker (whose start ensurer provisions the node's
``discord.outbox`` subscribe topic for it) and bounds the wait. One-shot
semantics (stop on first match) live in the orchestrator, keeping the node a
reusable matcher.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from calfkit import ConsumerNodeDef, NodeResult
from calfkit.client import Client
from calfkit.models import SessionRunContext
from calfkit.worker import Worker

from calfcord._provisioning import PROVISIONING
from calfcord.topics import DISCORD_OUTBOX_TOPIC

_DEFAULT_REPLY_TIMEOUT_S = 60.0

_AGENT_EMITTER_KIND = "agent"
"""``NodeResult.emitter_node_kind`` value for an agent reply (== ``NodeKind``
``"agent"``); the bridge's outbox consumer gates on the same literal."""


def make_first_reply_node(
    agent_id: str,
    *,
    on_match: Callable[[], None],
    on_match_correlation: Callable[[str], None] | None = None,
    node_id: str | None = None,
) -> ConsumerNodeDef[str]:
    """Build the consumer node that fires ``on_match`` on the target agent's reply.

    Pure and stateless: it carries no "have I matched yet" flag — every envelope
    that satisfies the gate + emitter checks invokes ``on_match``. The caller
    (the orchestrator) owns first-only semantics by, e.g., setting an
    :class:`asyncio.Event` in ``on_match`` and tearing the Worker down once it
    fires.

    Args:
        agent_id: The agent whose reply we are watching for. An agent's
            ``node_id`` equals its ``agent_id`` equals its ``.md`` ``name``, so
            this is matched directly against ``NodeResult.emitter_node_id``.
        on_match: Invoked (no args) when a reply from ``agent_id`` lands. Kept
            argument-free so the common case (set an Event) is trivial.
        on_match_correlation: Optional secondary callback receiving the matching
            envelope's ``correlation_id`` — lets a caller tie the live reply
            back to the inbound wire's ``event_id`` (the bridge's idiom). Called
            after ``on_match``.
        node_id: Optional consumer node id override (the Kafka ``group_id``).
            Defaults to a per-instance uuid so the watcher always reads in its
            own group at ``latest`` and never disturbs the bridge's outbox group.

    Returns:
        A :class:`ConsumerNodeDef` ready to register on a :class:`Worker`.
    """

    def _final_output_parts_gate(ctx: SessionRunContext) -> bool:
        # Same gate the bridge outbox uses: skip intermediate hops (tool
        # completions, mid-loop transitions) that carry no final output.
        return bool(ctx.state.final_output_parts)

    def _consume(result: NodeResult[str]) -> None:
        # Identity lives in the Kafka headers, recovered by the consumer handler
        # into these fields — a non-agent emitter (tool/client) or a different
        # agent (multi-agent org) is not the reply we're confirming.
        if result.emitter_node_kind != _AGENT_EMITTER_KIND:
            return
        if result.emitter_node_id != agent_id:
            return
        on_match()
        if on_match_correlation is not None:
            on_match_correlation(result.correlation_id)

    return ConsumerNodeDef[str](
        node_id=node_id or f"calfcord-firstreply-{uuid.uuid4().hex}",
        subscribe_topics=DISCORD_OUTBOX_TOPIC,
        consume_fn=_consume,
        output_type=str,
        gates=[_final_output_parts_gate],
    )


async def wait_for_first_reply(
    server_urls: str,
    *,
    agent_id: str,
    timeout_s: float = _DEFAULT_REPLY_TIMEOUT_S,
    client: Client | None = None,
    ready: asyncio.Event | None = None,
) -> bool:
    """Watch ``discord.outbox`` for the first reply from ``agent_id``.

    Host-agnostic and bridge-independent: connects a transient client (its own
    consumer group at ``latest``), registers a :func:`make_first_reply_node`
    consumer on a Worker, and awaits an :class:`asyncio.Event` set by the node's
    ``on_match``, bounded by ``timeout_s``.

    Returns:
        ``True`` if a reply from ``agent_id`` arrived within ``timeout_s``;
        ``False`` on a clean timeout (the wizard then downgrades to a
        try-it-yourself hint rather than promising more than it detected).

    Args:
        server_urls: Kafka bootstrap URL(s) — the same value passed to
            ``Client.connect`` elsewhere (calfkit 0.6.0 removed
            ``Client.server_urls``, so the URL is threaded through explicitly for
            provisioning).
        agent_id: The agent whose reply confirms the org is live.
        timeout_s: Bounded wait before giving up.
        client: Injected transient client for tests (so a unit/integration test
            can supply a fake or a pre-wired connection). Defaults to a fresh
            :meth:`Client.connect` with the shared opt-in provisioning policy.
        ready: Optional caller-owned event set the instant the consumer group has
            JOINED (right after ``worker.start()`` returns), BEFORE the match
            wait. The init wizard awaits it so it only prompts the human to send
            ``@<agent> hello`` once this ``latest``-offset watcher is listening —
            closing the race where a fast human posts before the group joins and
            the reply lands before the watcher is subscribed. Set even when the
            wait later times out: readiness is the join, not the reply.
    """
    matched = asyncio.Event()
    node = make_first_reply_node(agent_id, on_match=matched.set)

    owns_client = client is None
    transient = client if client is not None else Client.connect(server_urls, provisioning=PROVISIONING)
    try:
        worker = Worker(transient, [node])
        # ``discord.outbox`` is this node's SUBSCRIBE topic, and Worker is a
        # MANAGED surface: its ``start()`` ensurer auto-provisions the node's
        # subscribe topics (via ``topics_for_nodes``) in calfkit's single
        # pre-start pass — so no separate ``provision_extra_topics`` is needed
        # here. This matches the tools/router/agents managed runners, which all
        # trust the managed start to create their node topics (see
        # ``calfcord._provisioning``). Embedded managed lifecycle (signals OFF),
        # like the bridge: start() joins the consumer group, then we block on the
        # match Event with a hard timeout. ``stop()`` in the finally always
        # drains the broker — a no-op if start() never ran.
        await worker.start()
        # Signal readiness ONLY after the group has joined: the caller gates its
        # "now send @agent hello" prompt on this, so any reply the human triggers
        # afterward lands while we are already subscribed at ``latest``.
        if ready is not None:
            ready.set()
        try:
            await asyncio.wait_for(matched.wait(), timeout=timeout_s)
            return True
        except TimeoutError:
            return False
        finally:
            await worker.stop()
    finally:
        if owns_client:
            # Graceful client shutdown: cancels the (unused) reply dispatcher's
            # pending futures and stops the broker. Idempotent with the
            # ``worker.stop()`` above (both stop the same broker connection).
            await transient.close()
