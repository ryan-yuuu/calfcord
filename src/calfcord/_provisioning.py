"""Opt-in Kafka topic-provisioning policy and blind-spot helpers for the runners.

calfkit 0.5.x can create the topics a Worker's nodes reference (opt-in via
``ProvisioningConfig`` on ``Client.connect``), letting calfcord run on brokers
that do not auto-create topics — notably Tansu. Its provisioner walks each
node's ``subscribe_topics``/``publish_topic`` only, which leaves two gaps that
calfcord fills explicitly:

* The bridge and agents runners deliberately hand-roll ``register_handlers()`` +
  ``broker.start()`` instead of ``Worker.run()`` (see
  ``docs/design/calfkit-worker-lifecycle-gaps.md``), so calfkit's startup hook
  never fires — they must call ``worker.provision_topics()`` themselves.
* Some cross-process topics are raw FastStream broker subscribers, boot-time
  publish targets, or no-subscriber callback topics rather than node
  subscribe/publish topics, so ``topics_for_nodes()`` cannot see them.
  :func:`provision_extra_topics` creates those; the per-runner sets below name
  exactly which.

Why these particular extras (and not the rest of the cross-process contracts in
``calfcord.topics`` / ``calfcord.control_plane.topics``): every other shared
topic is also a node ``subscribe_topics``/``publish_topic`` on the process that
needs it created early, so the Worker already provisions it. Only the raw
control-plane subscribers, the boot-time publishes (which cannot wait for the
peer process to create the topic), and the no-subscriber discard topic fall
through — those are listed here.
"""

from __future__ import annotations

from collections.abc import Iterable

from calfkit import ProvisioningConfig
from calfkit.provisioning import TopicProvisioner

from calfcord.control_plane.topics import (
    AGENT_STATE_TOPIC,
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)
from calfcord.topics import AMBIENT_REPLY_DISCARD_TOPIC

PROVISIONING = ProvisioningConfig(enabled=True, num_partitions=1, replication_factor=1)
"""Shared opt-in provisioning policy passed to every calfcord ``Client.connect``.

``enabled`` so calfkit creates referenced topics on a broker without
auto-creation; ``num_partitions=1`` because ``agent.steps`` ordering REQUIRES a
single partition (see :data:`calfcord.topics.AGENT_STEPS_TOPIC`) and nothing
local benefits from more; ``replication_factor=1`` is the single-broker
local/dev default (NOT durable — raise it for a real multi-broker cluster)."""


def bridge_infra_topics() -> list[str]:
    """Non-node topics the bridge must ensure exist before ``broker.start()``.

    ``agent.state`` is consumed by a raw broker subscriber (the state consumer,
    not a Worker node). ``bridge.discovery`` is *published* at boot (the
    discovery ping) possibly before any agent is up, so the bridge cannot rely
    on an agent having created it.
    """
    return [AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC]


def agent_infra_topics(agent_ids: Iterable[str]) -> list[str]:
    """Non-node topics the agents runner must ensure exist before ``broker.start()``.

    ``bridge.discovery`` and each ``agent.{id}.control.in`` are consumed by raw
    control-sink subscribers (not Worker nodes); ``agent.state`` is *published*
    at boot (the startup announcement) possibly before the bridge is up. One
    control topic is added per hosted agent.
    """
    topics = [AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC]
    topics.extend(control_topic_for(agent_id) for agent_id in agent_ids)
    return topics


def router_infra_topics() -> list[str]:
    """Non-node topics the router must ensure exist.

    The ambient discard topic is the router's terminal-callback target for
    ambient invocations and has no subscriber, so ``topics_for_nodes()`` omits
    it; on a no-auto-create broker the router's reply publish would otherwise
    fail.
    """
    return [AMBIENT_REPLY_DISCARD_TOPIC]


async def provision_extra_topics(server_urls: str, topics: Iterable[str]) -> None:
    """Create ``topics`` that calfkit's node-walking provisioner cannot discover.

    A no-op when ``topics`` is empty (no admin client is constructed). Otherwise
    de-duplicates (first-seen order) and creates them with :data:`PROVISIONING`
    via calfkit's :class:`~calfkit.provisioning.TopicProvisioner`, which is
    idempotent (already-existing topics are reported, not recreated). These are
    plain data topics (no framework return inboxes), so ``framework_topics`` is
    empty.
    """
    topic_list = list(dict.fromkeys(topics))
    if not topic_list:
        return
    provisioner = TopicProvisioner.from_connection(server_urls=server_urls, config=PROVISIONING)
    await provisioner.provision(topic_list, framework_topics=set())
