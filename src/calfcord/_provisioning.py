"""Opt-in Kafka topic-provisioning policy and blind-spot helpers for the runners.

calfkit 0.6.0 auto-provisions topics on broker start (opt-in via
``ProvisioningConfig`` on ``Client.connect``), letting calfcord run on brokers
that do not auto-create topics — notably Tansu — but only for what its startup
ensurer can declare:

* The **client reply topic** is auto-provisioned on EVERY ``broker.start()``
  path (a connect-time pre-start hook; calf-ai/calfkit-sdk#180). calfcord no
  longer provisions it anywhere.
* A **Worker's node topics** are auto-provisioned only on the *managed* run
  surfaces (``Worker.run()`` / ``start()`` / ``async with``), whose
  ``_on_startup`` hook declares :func:`~calfkit.provisioning.topics_for_nodes`
  into the same ensurer. The tools/mcp/router/agents runners use ``Worker.run()``
  and the bridge uses the embedded ``Worker.start()`` — all managed surfaces — so
  every Worker-hosted process gets its node topics for free.

Two gaps remain that calfcord fills explicitly via :func:`provision_extra_topics`
(and, before a bare start, :func:`provision_and_start_broker`):

* **Hand-rolled node topics.** The control-plane probe deliberately decomposes
  ``Worker.run()`` — wiring a raw ``broker.subscriber(...)`` and then a bare
  ``broker.start()`` — because it owns a one-shot read (see
  ``docs/design/calfkit-worker-lifecycle-gaps.md``). The managed ``_on_startup``
  ensurer never fires for it, so it provisions its (blind-spot-only) topics
  itself before the bare start. (The bridge USED to be such a caller; it now
  folds onto the embedded ``Worker.start()`` managed surface, so calfkit
  auto-provisions its node topics and it only declares its blind spots — below.)
* **Blind-spot topics** that ``topics_for_nodes()`` cannot see at all: raw
  FastStream broker subscribers, boot-time publish targets, and no-subscriber
  callback topics. The per-runner ``*_infra_topics`` sets below name exactly
  which. On the managed runners these are declared into the client's startup
  ensurer from a worker ``on_startup`` hook so they ride calfkit's single
  pre-start provisioning pass: the agents runner declares
  :func:`agent_infra_topics` and the bridge declares :func:`bridge_infra_topics`
  that way, and the router's publish-only discard target is the one case
  provisioned via the standalone :func:`provision_extra_topics` instead.

Why these particular extras (and not the rest of the cross-process contracts in
``calfcord.topics`` / ``calfcord.control_plane.topics``): every other shared
topic is also a node ``subscribe_topics``/``publish_topic`` on the process that
needs it created early, so the ensurer already covers it. Only the raw
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

    Declared into the client's startup ensurer from the bridge's
    ``Worker.on_startup`` hook (pre-broker-start;
    :func:`calfcord.bridge.gateway._register_blind_spot_topics`), so they are
    created in calfkit's single managed provisioning pass alongside the node
    topics + reply topic, before the raw state consumer's group joins or the
    discovery ping publishes.
    """
    return list(_SHARED_CONTROL_PLANE_TOPICS)


def agent_infra_topics(agent_ids: Iterable[str]) -> list[str]:
    """Non-node topics the agents runner must ensure exist before ``broker.start()``.

    The shared control-plane pair (``bridge.discovery`` subscribed by the raw
    control sink, ``agent.state`` published at boot before the bridge may be up)
    plus one ``agent.{id}.control.in`` per hosted agent (each a raw control-sink
    subscriber).

    Declared into the client's startup ensurer from the agents runner's
    ``Worker.on_startup`` hook (pre-broker-start), so they are created in
    calfkit's single managed provisioning pass alongside the node topics +
    reply topic, before any raw control sink consumes or the presence publish
    fires.
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


async def provision_extra_topics(server_urls: str | Iterable[str], topics: Iterable[str]) -> None:
    """Create ``topics`` that calfkit's startup ensurer cannot declare.

    A no-op when ``topics`` is empty (no admin client is constructed). Otherwise
    de-duplicates (first-seen order) and creates them with :data:`PROVISIONING`
    via calfkit's :class:`~calfkit.provisioning.TopicProvisioner`, against
    ``server_urls`` — the same bootstrap the caller passes to ``Client.connect``
    (calfkit 0.6.0 removed ``Client.server_urls``, so the URL(s) must be threaded
    through explicitly). calfcord configures no broker security, so no
    ``security_kwargs`` are forwarded; the standalone provisioner connects with a
    plaintext admin client like the rest of calfcord. Idempotent — already-existing
    topics are reported, not recreated. These are plain data topics, so
    ``framework_topics`` is empty.
    """
    topic_list = list(dict.fromkeys(topics))
    if not topic_list:
        return
    provisioner = TopicProvisioner.from_connection(
        server_urls=server_urls,
        config=PROVISIONING,
    )
    report = await provisioner.provision(topic_list, framework_topics=set())
    # calfkit's provisioner does NOT raise when the broker authorizes the
    # connection but DENIES create (ACL code 29): it records the topic in
    # ``report.unauthorized`` and only logs a warning. Swallowing that lets the
    # runner come up and then silently stall on the wire (a raw subscriber or a
    # publish to a topic that never gets created), so raise loudly instead —
    # calfcord's infra-failure-raises rule. Operators must pre-create these
    # out-of-band when the broker enforces CREATE ACLs.
    if report.unauthorized:
        raise RuntimeError(
            f"topic provisioning unauthorized for {sorted(report.unauthorized)} on "
            f"broker {server_urls}: the broker denies CREATE (ACLs). Pre-create these "
            f"out-of-band; a consumer/publisher would otherwise stall on the missing "
            f"topic. (created={sorted(report.created)}, existing={sorted(report.existing)})"
        )


async def provision_and_start_broker(
    client: Client,
    server_urls: str | Iterable[str],
    topics: Iterable[str],
) -> None:
    """Provision ``topics`` the ensurer can't see, then bring the broker up.

    For the **hand-rolled** control-plane probe only: it wires a raw
    ``broker.subscriber(...)`` and then a bare ``broker.start()``, bypassing the
    managed Worker lifecycle — so calfkit's ``_on_startup`` ensurer never fires
    and does NOT provision its topics. On a broker that does not auto-create
    topics that bare start blocks forever unless every subscribed topic already
    exists, so the caller passes its blind-spot set here and they are created
    BEFORE the bare start.

    The client reply topic is intentionally NOT included: calfkit 0.6.0's
    connect-hook auto-provisions it on every start path (calf-ai/calfkit-sdk#180),
    so the broker.start() below creates it for free.

    Idempotent on an auto-creating broker (every create reports "existing"). The
    ``broker.running`` guard avoids a non-idempotent second ``start()`` if the
    broker was already brought up (e.g. by an earlier lazy first publish).

    The Worker-hosted runners do NOT use this helper: tools/mcp/router/agents run
    via ``Worker.run()`` and the bridge via the embedded ``Worker.start()``,
    whose managed lifecycle auto-provisions reply + node topics; the router
    provisions only its publish-only blind spot via :func:`provision_extra_topics`,
    and the agents runner and bridge declare their blind-spot topics into the
    startup ensurer from an ``on_startup`` hook — all letting the managed broker
    start do the provisioning.
    """
    await provision_extra_topics(server_urls, topics)
    if not client.broker.running:
        await client.broker.start()
