"""Opt-in Kafka topic-provisioning policy and blind-spot helpers for the runners.

calfkit 0.5.x can create the topics a Worker's nodes reference (opt-in via
``ProvisioningConfig`` on ``Client.connect``), letting calfcord run on brokers
that do not auto-create topics — notably Tansu. Its provisioner walks each
node's ``subscribe_topics``/``publish_topic`` only, which leaves gaps that
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
* The client's reply topic is a framework inbox calfkit subscribes at ``connect``
  but only provisions lazily on first invoke — never before a direct
  ``broker.start()``. :func:`provision_and_start_broker` provisions it (plus the
  extras and, optionally, the worker's node topics) before starting the broker,
  so all four direct-start runners share one definition of that contract.

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
from typing import TYPE_CHECKING

from calfkit import ProvisioningConfig
from calfkit.provisioning import TopicProvisioner

from calfcord.control_plane.topics import (
    AGENT_STATE_TOPIC,
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)
from calfcord.topics import AMBIENT_REPLY_DISCARD_TOPIC

if TYPE_CHECKING:
    from calfkit.client import Client
    from calfkit.worker import Worker

PROVISIONING = ProvisioningConfig(enabled=True, num_partitions=1, replication_factor=1)
"""Shared opt-in provisioning policy passed to every calfcord ``Client.connect``.

``enabled`` so calfkit creates referenced topics on a broker without
auto-creation; ``num_partitions=1`` because ``agent.steps`` ordering REQUIRES a
single partition (see :data:`calfcord.topics.AGENT_STEPS_TOPIC`) and nothing
local benefits from more; ``replication_factor=1`` is the single-broker
local/dev default (NOT durable — raise it for a real multi-broker cluster)."""

# The bridge<->agent control-plane pair both sides touch: the bridge subscribes
# agent.state and publishes bridge.discovery at boot; agents subscribe
# bridge.discovery and publish agent.state at boot. Shared so the two infra sets
# that include it cannot drift apart.
_SHARED_CONTROL_PLANE_TOPICS = (AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC)


def bridge_infra_topics() -> list[str]:
    """Non-node topics the bridge must ensure exist before ``broker.start()``.

    ``agent.state`` is consumed by a raw broker subscriber (the state consumer,
    not a Worker node). ``bridge.discovery`` is *published* at boot (the
    discovery ping) possibly before any agent is up, so the bridge cannot rely
    on an agent having created it.
    """
    return list(_SHARED_CONTROL_PLANE_TOPICS)


def agent_infra_topics(agent_ids: Iterable[str]) -> list[str]:
    """Non-node topics the agents runner must ensure exist before ``broker.start()``.

    The shared control-plane pair (``bridge.discovery`` subscribed by the raw
    control sink, ``agent.state`` published at boot before the bridge may be up)
    plus one ``agent.{id}.control.in`` per hosted agent (each a raw control-sink
    subscriber).
    """
    return [*_SHARED_CONTROL_PLANE_TOPICS, *(control_topic_for(agent_id) for agent_id in agent_ids)]


def router_infra_topics() -> list[str]:
    """Non-node topics the router must ensure exist.

    The ambient discard topic is the router's terminal-callback target for
    ambient invocations and has no subscriber, so ``topics_for_nodes()`` omits
    it; on a no-auto-create broker the router's reply publish would otherwise
    fail.
    """
    return [AMBIENT_REPLY_DISCARD_TOPIC]


async def provision_extra_topics(client: Client, topics: Iterable[str]) -> None:
    """Create ``topics`` that calfkit's node-walking provisioner cannot discover.

    A no-op when ``topics`` is empty (no admin client is constructed). Otherwise
    de-duplicates (first-seen order) and creates them with :data:`PROVISIONING`
    via calfkit's :class:`~calfkit.provisioning.TopicProvisioner`, reusing the
    connected ``client``'s bootstrap URL(s) and security kwargs so creation hits
    the same broker with the same credentials as the Worker's own provisioning
    pass (idempotent; already-existing topics are reported, not recreated). These
    are plain data topics, so ``framework_topics`` is empty.
    """
    topic_list = list(dict.fromkeys(topics))
    if not topic_list:
        return
    provisioner = TopicProvisioner.from_connection(
        server_urls=client.server_urls,
        config=PROVISIONING,
        security_kwargs=client.security_kwargs,
    )
    await provisioner.provision(topic_list, framework_topics=set())


async def provision_and_start_broker(
    client: Client,
    *,
    extra_topics: Iterable[str] = (),
    worker: Worker | None = None,
) -> None:
    """Provision everything ``client``'s subscribers consume, then start its broker.

    The router/tools/mcp/agents runners bring their reply dispatcher up with a
    DIRECT ``broker.start()``; on a broker that does not auto-create topics that
    start blocks forever unless every subscribed topic already exists. Two
    categories are invisible to calfkit's node-walking provisioner and are
    created here first, BEFORE ``broker.start()``:

    * **the client's reply topic** — calfkit registers a reply-dispatcher
      subscriber on ``client.reply_topic`` at ``connect`` but only provisions it
      lazily on the first invoke/emit, which a direct ``broker.start()`` never
      triggers (these processes may never invoke at all). This works around an
      upstream gap: calf-ai/calfkit-sdk#180 — if calfkit provisions the reply
      topic before a direct start, this step (and the helper) can be dropped; and
    * **calfcord's blind-spot topics** (``extra_topics``) — raw control-plane
      subscribers and no-subscriber callback targets (see the module docstring).

    When ``worker`` is given (the hand-rolled path that bypasses ``Worker.run()``
    and so never fires calfkit's ``_on_startup`` provisioning), the worker's own
    node topics are provisioned too. A pure no-op on an auto-creating broker.

    The bridge does NOT use this helper: its reply topic ``discord.outbox`` is
    also its outbox node's inbox (so ``worker.provision_topics()`` already covers
    it), and it interleaves state-consumer registration before its own start.
    """
    if worker is not None:
        await worker.provision_topics()
    await provision_extra_topics(client, [client.reply_topic, *extra_topics])
    if not client.broker.running:
        await client.broker.start()
