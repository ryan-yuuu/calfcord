"""Opt-in Kafka topic-provisioning policy and blind-spot helpers for the runners.

calfkit 0.5.x can create the topics a Worker's nodes reference (opt-in via
``ProvisioningConfig`` on ``Client.connect``), letting calfcord run on brokers
that do not auto-create topics — notably Tansu. Its provisioner walks each
node's ``subscribe_topics``/``publish_topic`` only, which leaves gaps that
calfcord fills explicitly:

* Some cross-process topics are raw FastStream broker subscribers, boot-time
  publish targets, or no-subscriber callback topics rather than node
  subscribe/publish topics, so ``topics_for_nodes()`` cannot see them.
  :func:`provision_extra_topics` creates those; the per-runner sets below name
  exactly which.
* The client's reply topic is a framework inbox calfkit subscribes at ``connect``
  but only provisions lazily on first invoke — never before the direct
  ``broker.start()`` the worker performs inside ``Worker.run()``/``start()``. On
  a no-auto-create broker that start hangs forever unless the topic exists first
  (calf-ai/calfkit-sdk#180, still open in 0.5.4). :func:`provision_infra` creates
  it (plus the extras) BEFORE the runner hands the lifecycle to the worker, so
  every runner shares one definition of that contract.

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
    report = await provisioner.provision(topic_list, framework_topics=set())
    # calfkit's provisioner does NOT raise when the broker AUTHORIZES the connection
    # but denies CREATE (ACL code 29): it records the topic in ``report.unauthorized``
    # and logs a single low-visibility warning. Swallowing that here is the worst
    # failure mode for the managed lifecycle — the runner would proceed to
    # ``worker.run()``'s direct ``broker.start()``, which then HANGS FOREVER waiting
    # on the un-created reply topic (the calfkit#180 hang). Raise loudly instead
    # (calfcord's infra-failure-raises rule): an operator must pre-create these
    # out-of-band rather than watch a process "start" but never serve.
    if report.unauthorized:
        raise RuntimeError(
            f"topic provisioning unauthorized for {sorted(report.unauthorized)} on "
            f"broker {client.server_urls}: the broker denies CREATE (ACLs), so these "
            f"must be pre-created out-of-band. A managed worker.start() would otherwise "
            f"hang on the missing topic — see calfkit#180."
        )


async def provision_infra(client: Client, *, extra_topics: Iterable[str] = ()) -> None:
    """Create the topics calfkit's node-walking provisioner can't discover, before broker start.

    The provision-only seam for the 0.5.4 managed-worker lifecycle: under
    ``Worker.run()`` / ``Worker.start()`` the worker owns ``broker.start()`` and
    provisions its own node topics during startup, so this helper fills only the two
    blind spots calfkit cannot see:

    * **the client's reply topic** — calfkit registers a reply-dispatcher subscriber
      on ``client.reply_topic`` at ``connect`` but provisions it only lazily on the
      first invoke, never before the direct ``broker.start()`` the worker performs.
      On a no-auto-create broker that start hangs forever unless the topic exists
      first (TODO(calfkit#180): drop this line — and this whole reply-topic dance —
      once calf-ai/calfkit-sdk#180 lands and calfkit provisions the reply topic
      before a direct start); and
    * **calfcord's blind-spot topics** (``extra_topics``) — raw control-plane
      subscribers and no-subscriber callback targets (see the module docstring).

    Call once BEFORE ``worker.run()`` / ``worker.start()``. Idempotent, but not a
    no-op: because the reply topic is always in the list, it always performs one
    admin connect + ``create_topics`` round-trip — already-existing topics are
    reported as existing, not recreated, so it is safe to call on a broker that
    already has them (incl. an auto-creating one).
    """
    # TODO(calfkit#180): drop ``client.reply_topic`` from this list — and, when it
    # is the only entry, this whole call — once calf-ai/calfkit-sdk#180 lands and
    # calfkit provisions the reply topic before a direct broker.start(). The
    # strict-xfail canary in tests/integration/test_broker_startup_provisioning.py
    # flips to "unexpectedly passing" the moment that happens, forcing this edit.
    await provision_extra_topics(client, [client.reply_topic, *extra_topics])
