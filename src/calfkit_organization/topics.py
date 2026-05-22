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

Producer: :mod:`calfkit_organization.bridge.ingress` (ambient branch).
Consumer: the built-in router agent
(:mod:`calfkit_organization.router.runner`, via the ``subscribe_topics``
list built in :func:`calfkit_organization.agents.factory.AgentFactory._build_router_node`).

Hardcoded rather than env-driven because the project's topology contract
is fixed — operators changing this topic would also need to retune the
router's subscribe_topics, which is also constant."""

SYNTHESIZED_INGRESS_TOPIC = "bridge.synthesized.in"
"""Topic the bridge's synthesized-in consumer subscribes to. The router's
fan-out @consumer publishes one envelope per chosen agent here; the
bridge picks each synthesized wire off the topic, validates it, and
feeds it through :meth:`BridgeIngress.handle` so the assistant chain
looks identical to a real Discord slash.

Producer: :mod:`calfkit_organization.router.fanout`.
Consumer: :mod:`calfkit_organization.bridge.synthesized`."""

AMBIENT_REPLY_DISCARD_TOPIC = "_calf.ambient.callback-discard"
"""Throwaway reply topic for ambient invocations. The router's
:class:`ReturnCall` publishes the :class:`RoutingDecision` to BOTH its
``publish_topic`` (``routing.decisions``, where the fan-out consumer
subscribes) AND to the caller's ``reply_topic``. We don't want the
decision echoed to the bridge's outbox or anywhere visible, so we
direct it to this no-subscriber topic.

**Retention / privacy warning.** The router's ``ReturnCall``
envelope carries ``state.metadata`` containing the original Discord
wire (author info, message content) and phonebook. The same envelope
lands on BOTH ``routing.decisions`` AND this discard topic via
FastStream's publisher mirroring — we cannot strip the metadata
from the discard side without also losing it from the consumed side
(the fan-out needs it). Operators MUST configure short retention or
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
:func:`calfkit_organization.router.definition.build_router_definition`).
Consumer: :mod:`calfkit_organization.router.fanout`."""
