"""Project-wide Kafka topic constants for cross-process contracts.

Topics that are produced by one process and consumed by another live
here so producer and consumer always agree on the literal. Putting
these in a tiny zero-dependency module lets any package
(``bridge/``, ``router/``, ``agents/``) import them without risking
import cycles, and removes the need for "drift-guard" contract tests
that re-assert the same string in two places.

Add a topic here only when **multiple processes** subscribe or publish
to it. Per-agent topics (``agent.{id}.in``, channel topics) stay where
they're consumed — they're parameterized strings, not cross-process
contracts.
"""

from __future__ import annotations

AMBIENT_INGRESS_TOPIC = "discord.ambient.in"
"""Topic the router agent subscribes to. The bridge publishes ambient
(``kind="message"``) wires here for the router to classify.

Producer: :mod:`calfcord.bridge.ingress` (ambient branch).
Consumer: the built-in router agent
(:mod:`calfcord.router.runner`, via the ``subscribe_topics``
list built in :func:`calfcord.agents.factory.AgentFactory._build_router_node`).

Hardcoded rather than env-driven because the project's topology contract
is fixed — operators changing this topic would also need to retune the
router's subscribe_topics, which is also constant."""

SYNTHESIZED_INGRESS_TOPIC = "bridge.synthesized.in"
"""Topic the bridge's synthesized-in consumer subscribes to. The router's
fan-out @consumer publishes one envelope per chosen agent here; the
bridge picks each synthesized wire off the topic, validates it, and
feeds it through :meth:`BridgeIngress.handle` so the assistant chain
looks identical to a real Discord slash.

Producer: :mod:`calfcord.router.fanout`.
Consumer: :mod:`calfcord.bridge.synthesized`."""

AMBIENT_REPLY_DISCARD_TOPIC = "_calf.ambient.callback-discard"
"""Throwaway reply topic for ambient invocations. The router's
:class:`ReturnCall` publishes the :class:`RoutingDecision` to BOTH its
``publish_topic`` (``routing.decisions``, where the fan-out consumer
subscribes) AND to the caller's ``reply_topic``. We don't want the
decision echoed to the bridge's outbox or anywhere visible, so we
direct it to this no-subscriber topic.

**Retention / privacy warning.** The router's ``ReturnCall``
envelope carries ``deps`` containing the original Discord
wire (author info, message content), phonebook, and channel history.
The same envelope lands on BOTH ``routing.decisions`` AND this discard
topic via FastStream's publisher mirroring — we cannot strip the deps
from the discard side without also losing them from the consumed side
(the fan-out needs them). Operators MUST configure short retention or
``cleanup.policy=delete`` with a low ``retention.ms`` on this topic;
the cluster default (7 days) would persist a copy of every ambient
message in plaintext on a topic nobody reads. Recommended setting:

    kafka-configs.sh --alter --entity-type topics \\
        --entity-name _calf.ambient.callback-discard \\
        --add-config retention.ms=60000,cleanup.policy=delete

The leading underscore signals "internal infrastructure" to operators
reading Kafka topic lists. ``kafka-topics.sh --list`` shows the topic
by default; pass ``--exclude-internal`` only if you want it hidden
from routine listings. When checking retention or other config, use
the literal topic name (``--entity-name _calf.ambient.callback-discard``)."""

ROUTING_DECISIONS_TOPIC = "routing.decisions"
"""Topic the router agent's ``ReturnCall`` publishes to (via the agent's
``publish_topic``); the fan-out @consumer subscribes here.

Producer: built-in router agent (see
:func:`calfcord.router.definition.build_router_definition`).
Consumer: :mod:`calfcord.router.fanout`."""

AGENT_STEPS_TOPIC = "agent.steps"
"""Shared topic that every assistant agent mirrors its handler hops to.

FastStream's ``@publisher`` decorator (see
:meth:`calfkit.worker.Worker.register_handlers`) re-publishes every
``Response`` returned from a node's handler onto its ``publish_topic``.
Wiring this topic as the assistant agent's ``publish_topic`` makes
every intermediate ``Call`` / ``TailCall`` / ``ReturnCall`` envelope
visible on a single feed.

Producer: every assistant :class:`~calfkit.nodes.Agent` built by
:meth:`calfcord.agents.factory.AgentFactory.build_node`.
Consumer: the bridge's steps consumer
(:func:`calfcord.bridge.steps.build_steps_consumer`),
which projects the message-history delta on each hop into a Discord
transcript thread.

**Operator note — single partition required.** This topic MUST be
configured with a single partition (or all of one agent's hops must
hash to the same partition by some other means). FastStream's
``@publisher`` decorator wraps the calfkit handler's plain
``faststream.Response`` return without carrying a Kafka key, so on a
multi-partition topic the hops for one ``correlation_id`` can
round-robin partitions and arrive at the consumer out of order. The
consumer's history-cursor advance is monotonic, so an out-of-order
late hop would silently swallow its delta; an intermediate hop
arriving after a terminal hop would seed a second (unlocked)
transcript thread. The bridge's direct
:meth:`~calfkit.Client.publish` calls do stamp the correlation_id as
the partition key (see ``calfkit/nodes/base.py``); the gap is only
the publisher-decorator mirror path that ``publish_topic`` activates.

Recommended Kafka config:

    kafka-topics.sh --create --topic agent.steps --partitions 1 ..."""
