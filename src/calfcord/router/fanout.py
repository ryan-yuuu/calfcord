"""Fan-out consumer: turn a :class:`RoutingDecision` into one synthesized wire.

Subscribed to ``routing.decisions`` (the router agent's
``publish_topic``). On every router reply whose state carries a final
output, this consumer:

1. Reads the :class:`RoutingDecision` from ``result.output``.
2. Recovers the original :class:`WireMessage`, the publisher's phonebook
   snapshot, and the channel history from ``result.deps`` — the same
   ``deps`` dict the bridge ingress passed to ``invoke_node``, carried
   forward through the router run to the consumer (calfkit's ``ConsumerContext``
   exposes inbound producer deps on ``ConsumerContext.deps``).
3. For the chosen ``agent_id`` (after defensive self-filter and
   phonebook validation), synthesizes a fresh wire with ``kind="slash"``
   and ``slash_target=<agent_id>`` plus a fresh ``event_id``, and
   publishes it to ``bridge.synthesized.in`` with the synthesized wire
   (and the forwarded history) on ``deps`` for the bridge's
   synthesized-in consumer to read back from ``result.deps``.

The single-id model is enforced at the :class:`RoutingDecision` schema
level: ``agent_id`` is a single ``str | None``, so the consumer cannot
fan out to multiple targets even by accident. The ``None`` case is
defense-in-depth: a misbehaving LLM that emits a tool call without an
``agent_id`` falls through to the no-op path here rather than
triggering pydantic-ai structured-output validation retries.

The fresh ``event_id`` is load-bearing: the bridge's ingress writes the
wire into the :class:`PendingWires` map keyed on ``event_id``, and the
outbox consumer reads back by ``correlation_id`` (which equals the
wire's ``event_id``). Reusing the original ambient's event_id would
collide on the map and the assistant's reply would be misattributed.

Built as a closure that captures ``client`` and ``router_agent_id`` —
same shape as :func:`build_outbox_consumer` in
:mod:`calfcord.bridge.outbox`. Use the ``@consumer``
decorator (calfkit's preferred sugar) rather than constructing
:class:`ConsumerNode` directly.
"""

from __future__ import annotations

import logging

import uuid_utils
from calfkit import ConsumerNode
from calfkit.client import Client
from calfkit.models import ConsumerContext, SessionRunContext
from calfkit.nodes.consumer import consumer
from pydantic import ValidationError

from calfcord.agents.phonebook import phonebook_from_deps
from calfcord.agents.routing import RoutingDecision
from calfcord.ambient_routing import raise_routing_contract_error
from calfcord.bridge.wire import WireMessage
from calfcord.topics import SYNTHESIZED_INGRESS_TOPIC

logger = logging.getLogger(__name__)


DEFAULT_FANOUT_NODE_ID = "router-fanout"


def build_fanout_consumer(
    client: Client,
    router_agent_id: str,
    *,
    subscribe_topic: str = "routing.decisions",
    node_id: str = DEFAULT_FANOUT_NODE_ID,
) -> ConsumerNode[RoutingDecision]:
    """Construct the router's fan-out consumer node.

    Args:
        client: Connected calfkit :class:`Client`. The closure uses it
            to publish the synthesized wire via
            :meth:`Client.invoke_node`.
        router_agent_id: The router's own ``agent_id`` (typically
            :data:`ROUTER_AGENT_ID`). The consumer defensively filters
            this id out — the LLM should never pick the router itself,
            but a misbehaving model output should not be allowed to
            publish a synthesized wire targeting the router (which
            would loop forever because the router's ingress topic is
            ambient, not channel-scoped).
        subscribe_topic: Override for tests. Production uses the
            router's ``publish_topic`` (``"routing.decisions"``).
        node_id: Stable consumer-group identifier. Two ``calfkit-router``
            processes would load-balance the fan-out across the topic's
            partitions — not recommended (you'd get half the decisions
            per process) but not catastrophic.

    Returns:
        A :class:`ConsumerNode` ready to register on the
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
        agent_output_type=RoutingDecision,
        node_id=node_id,
        gates=[_final_output_parts_gate],
    )
    async def _fan_out(result: ConsumerContext[RoutingDecision]) -> None:
        decision = result.output
        if decision is None:
            # Gate should prevent this: a ``final_output_parts``
            # envelope with no parsed output means calfkit's
            # deserializer dropped the value (upstream validation
            # failure / framework bug). Infrastructure contract
            # violation — raise so operators see it at ERROR.
            raise_routing_contract_error(
                correlation_id=result.correlation_id,
                site="fanout",
                reason="received envelope with no parsed RoutingDecision",
            )

        deps = result.deps
        try:
            wire = WireMessage.model_validate(deps.get("discord"))
        except ValidationError as exc:
            # The bridge ingress is contractually required to pack the
            # original wire under ``deps["discord"]`` (see
            # ``_publish_ambient``). A missing or malformed value here —
            # ``model_validate`` raises on both — is an infra bug, not an
            # LLM-recoverable input problem.
            raise_routing_contract_error(
                correlation_id=result.correlation_id,
                site="fanout",
                reason=(
                    f"invalid/missing deps['discord'] "
                    f"(agent_id={decision.agent_id}): {exc}"
                ),
                cause=exc,
            )

        logger.info(
            "fan-out received decision correlation_id=%s channel=%s agent_id=%s reasoning=%s",
            result.correlation_id,
            wire.channel_id,
            decision.agent_id,
            decision.reasoning,
        )

        if decision.agent_id is None:
            # Defense-in-depth: the prompt mandates picking exactly
            # one agent, so ``None`` is a prompt-disobedience signal —
            # the LLM emitted a tool call without populating
            # ``agent_id``. WARN so operators grep'ing for "why didn't
            # anyone respond to this user?" can find it; INFO would
            # bury the line in routine traffic. The ambient message
            # goes unanswered as a result.
            logger.warning(
                "fan-out skipping decision with no agent_id "
                "(LLM disobeyed always-pick-one prompt) "
                "event_id=%s channel=%s author=%s correlation_id=%s "
                "reasoning=%r",
                wire.event_id,
                wire.channel_id,
                wire.author.display_name,
                result.correlation_id,
                decision.reasoning,
            )
            return

        # Fail-closed on missing phonebook. Production producers
        # ALWAYS pack the phonebook under ``deps["phonebook"]`` on the
        # ambient publish so the fan-out can validate the chosen
        # ``agent_id`` against the publisher's registry snapshot. A
        # missing key here means an infra bug — not a backward-compat
        # case — and silently skipping validation would let LLM
        # hallucinations through. Raising surfaces the misconfiguration;
        # the Kafka envelope is already ACKed (``AckPolicy.ACK_FIRST``)
        # so this produces an ERROR log, not a redelivery loop.
        phonebook_raw = deps.get("phonebook")
        if phonebook_raw is None:
            raise_routing_contract_error(
                correlation_id=result.correlation_id,
                site="fanout",
                reason=(
                    f"missing deps['phonebook'] on ambient envelope "
                    f"(agent_id={decision.agent_id})"
                ),
            )
        try:
            phonebook = phonebook_from_deps(phonebook_raw)
        except (ValueError, ValidationError) as exc:
            raise_routing_contract_error(
                correlation_id=result.correlation_id,
                site="fanout",
                reason=(
                    f"malformed deps['phonebook'] "
                    f"(agent_id={decision.agent_id}): {exc}"
                ),
                cause=exc,
            )

        # Build the known-agent set from the publisher's phonebook
        # snapshot. The fan-out lives in a separate process from the
        # bridge and has no registry access of its own; the bridge
        # ships its :class:`PhonebookEntry` projection on every ambient
        # publish via ``deps["phonebook"]``.
        known_agent_ids: set[str] = {e.agent_id for e in phonebook}

        if decision.agent_id == router_agent_id:
            # Defensive self-filter: the LLM should never pick the
            # router itself (the roster builder excludes it), but a
            # misbehaving model could. A synthesized wire targeting
            # the router would loop forever (router's ingress topic
            # accepts every envelope it sees). WARN with full context
            # so the operator can distinguish "LLM hallucinated the
            # router's id" (model error) from "registry exposed it
            # to the roster" (wiring bug); the reasoning field
            # usually indicates which.
            logger.warning(
                "fan-out skipping router's own id (LLM picked itself) "
                "event_id=%s channel=%s author=%s correlation_id=%s "
                "reasoning=%r",
                wire.event_id,
                wire.channel_id,
                wire.author.display_name,
                result.correlation_id,
                decision.reasoning,
            )
            return

        if decision.agent_id not in known_agent_ids:
            # Unknown agent_id from the LLM (either hallucinated or
            # registry drift since the publisher built the
            # phonebook). Publishing a synthesized wire targeted at a
            # non-existent agent would orphan in
            # ``PendingWires`` until LRU eviction, with no operator
            # signal and no user-visible reply. ERROR log with full
            # context (channel, author, correlation_id, and the
            # agent_ids the LLM had to choose from) so the operator
            # can quickly identify the affected conversation and
            # decide whether the registry needs an update.
            logger.error(
                "fan-out skipping unknown agent_id=%r event_id=%s "
                "channel=%s author=%s correlation_id=%s "
                "known_agents=%s — agent is not in the publisher's "
                "phonebook (LLM hallucination or registry drift)",
                decision.agent_id,
                wire.event_id,
                wire.channel_id,
                wire.author.display_name,
                result.correlation_id,
                sorted(known_agent_ids),
            )
            return

        synthesized = wire.model_copy(
            update={
                "event_id": uuid_utils.uuid7().hex,
                "kind": "slash",
                "slash_target": decision.agent_id,
            }
        )
        # The synthesized publish deliberately omits ``phonebook``: the
        # bridge's slash branch rebuilds deps from its registry on each
        # re-entry, so shipping the projection through this hop would be
        # redundant.
        #
        # The history records ARE forwarded: the bridge's ambient publish
        # path fetches channel history once at publish time (see
        # ``BridgeIngress._fetch_ambient_history``) and packs it into
        # ``deps["history"]``. We forward that same opaque JSON list
        # unchanged (no re-validation — the synthesized-in consumer
        # validates on read), so an N-way fan-out still costs a single
        # Discord fetch. Without forwarding, the synthesized-in consumer
        # would receive no history, pass ``prefetched_history=()`` to
        # ``BridgeIngress.handle``, and the assistant would run with no
        # history.
        synth_deps = {
            "discord": synthesized.model_dump(mode="json"),
            "history": deps.get("history", []),
        }

        # ``handle`` lives outside the try so the finally clause
        # below can cancel its future even if a later step (e.g.
        # the success-log) raises. The fire-and-forget cancel is
        # load-bearing: the dispatcher's pending future would
        # otherwise leak; cancelling triggers the
        # ``add_done_callback`` that pops the registry entry.
        # Same idiom as :meth:`BridgeIngress.handle`.
        handle = None
        try:
            handle = await client.invoke_node(
                user_prompt="",
                topic=SYNTHESIZED_INGRESS_TOPIC,
                deps=synth_deps,
                correlation_id=synthesized.event_id,
            )
            logger.info(
                "fan-out published agent=%s event_id=%s channel=%s",
                decision.agent_id,
                synthesized.event_id,
                synthesized.channel_id,
            )
        except Exception:
            # A publish failure (broker hiccup, serialization error,
            # connection drop) silently drops the user's ambient
            # message unless we log + re-raise here. The envelope
            # was already ACKed (``AckPolicy.ACK_FIRST``) so the
            # consumer harness will not redeliver; we just need an
            # operator-greppable ERROR with channel/author/event_id
            # so a future "scribe never replied to my message"
            # report can be correlated to a specific failure mode.
            # Re-raising preserves the harness's own
            # uncaught-exception path on top of our rich log.
            logger.error(
                "fan-out publish failed agent=%s event_id=%s "
                "channel=%s author=%s correlation_id=%s",
                decision.agent_id,
                synthesized.event_id,
                synthesized.channel_id,
                wire.author.display_name,
                result.correlation_id,
                exc_info=True,
            )
            raise
        finally:
            if handle is not None:
                handle._future.cancel()

    return _fan_out
