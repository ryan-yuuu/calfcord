"""Opt-in Kafka topic-provisioning policy for the runners.

calfkit auto-provisions topics on broker start (opt-in via ``ProvisioningConfig``
on ``Client.connect``), letting calfcord run on brokers that do not auto-create
topics — notably Tansu — for everything its startup ensurer can declare:

* The **client reply topic** is auto-provisioned on EVERY ``broker.start()``
  path (a connect-time pre-start hook; calf-ai/calfkit-sdk#180). calfcord no
  longer provisions it anywhere.
* A **Worker's node topics** are auto-provisioned on the *managed* run surfaces
  (``Worker.run()`` / ``start()`` / ``async with``), whose ``_on_startup`` hook
  declares :func:`~calfkit.provisioning.topics_for_nodes` into the same ensurer.
  The **tools and agents runners** use the embedded managed ``Worker.start()``
  surface, so every Worker-hosted process gets its node topics for free. The
  **bridge is a pure** :class:`~calfkit.client.Client` (no Worker, no consumers)
  — it hosts no nodes and so declares no node topics; its only topic is the
  client reply topic, covered by the pre-start hook above.

After the calfkit 0.12 migration removed the bespoke control plane, calfcord has
no blind-spot topics left to declare: agent presence and the live roster now ride
calfkit's native mesh (``calf.agents``), which the framework owns end-to-end, and
A2A is native tool/handoff dispatch over ordinary node topics the managed ensurer
already covers. So :data:`PROVISIONING` is the only piece every runner still
needs; :func:`provision_extra_topics` / :func:`provision_and_start_broker` remain
as general-purpose helpers for any future caller that hand-rolls a raw broker
subscriber outside the managed Worker lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from calfkit import ProvisioningConfig
from calfkit.provisioning import TopicProvisioner

if TYPE_CHECKING:
    from calfkit.client import Client

PROVISIONING = ProvisioningConfig(enabled=True, num_partitions=1, replication_factor=1)
"""Shared opt-in provisioning policy passed to every calfcord ``Client.connect``.

``enabled`` so calfkit creates referenced topics on a broker without
auto-creation; ``num_partitions=1`` is the single-partition local/dev default
(nothing calfcord runs locally benefits from more, and a single partition keeps
per-key ordering trivially intact); ``replication_factor=1`` is the single-broker
local/dev default (NOT durable — raise it for a real multi-broker cluster)."""


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

    For any caller that hand-rolls a raw ``broker.subscriber(...)`` followed by a
    bare ``broker.start()``, bypassing the managed Worker lifecycle — so calfkit's
    ``_on_startup`` ensurer never fires and does NOT provision its topics. On a
    broker that does not auto-create topics that bare start blocks forever unless
    every subscribed topic already exists, so the caller passes its blind-spot set
    here and they are created BEFORE the bare start.

    The client reply topic is intentionally NOT included: calfkit's connect-hook
    auto-provisions it on every start path (calf-ai/calfkit-sdk#180), so the
    ``broker.start()`` below creates it for free.

    Idempotent on an auto-creating broker (every create reports "existing"). The
    ``broker.running`` guard avoids a non-idempotent second ``start()`` if the
    broker was already brought up (e.g. by an earlier lazy first publish).

    The production runners do NOT need this helper: the tools/agents runners use
    the managed ``Worker.start()`` surface (whose lifecycle auto-provisions reply
    + node topics), and the bridge is a pure :class:`~calfkit.client.Client` that
    hand-rolls no broker (its reply topic is covered by the connect-time pre-start
    hook). None of them declare blind-spot topics, so nothing here is needed —
    this stays a general-purpose helper for a future raw-broker caller.
    """
    await provision_extra_topics(server_urls, topics)
    if not client.broker.running:
        await client.broker.start()
