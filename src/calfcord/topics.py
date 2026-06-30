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

DISCORD_OUTBOX_TOPIC = "discord.outbox"
"""Topic every agent reply lands on; the bridge posts each one to Discord.

An agent reply is the agent node's ``ReturnCall`` envelope: assistant agents
have ``publish_topic=None`` and emit to the inbound frame's ``callback_topic``,
which the bridge sets to this topic. The replying agent's identity is NOT in the
JSON body — it rides the Kafka headers (``x-calf-emitter`` /
``x-calf-emitter-kind``) and is recoverable only through a calfkit consumer
handler's ``_stamp_transport``.

Producer: every assistant :class:`~calfkit.nodes.Agent` (via the bridge-set
``callback_topic``) — the bridge's ingress and retry sites publish with
``send(reply_to="discord.outbox")``, which sets each invocation's
``callback_topic`` to this topic. The bridge :class:`~calfkit.Client` does NOT
name it as its own ``reply_topic`` (it takes a private auto-generated inbox); the
topic is provisioned on broker start as the outbox consumer's
``subscribe_topics`` via the managed :class:`~calfkit.Worker` lifecycle (see
:mod:`calfcord.bridge.gateway`).

Consumers:
* the bridge's outbox consumer
  (:func:`calfcord.bridge.outbox.build_outbox_consumer`), which posts each reply
  to Discord under the agent's persona;
* the init wizard's first-reply detector
  (:func:`calfcord.control_plane.first_reply.wait_for_first_reply`), a transient
  CLI-side consumer in its own group that confirms the org is live."""

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
