"""Tests for calfcord's opt-in topic-provisioning policy + blind-spot helpers.

calfkit 0.6.0 auto-provisions the client reply topic on EVERY broker-start path
(a connect-time pre-start hook) and the worker's node topics on the managed
``Worker.run()``/``start()``/``async with`` paths. What it still cannot see are
calfcord's cross-process topics that are raw FastStream broker subscribers,
boot-time publish targets, or no-subscriber callback topics — and, on the
hand-rolled ``register_handlers()`` + bare ``broker.start()`` paths (bridge,
probe), the node topics too (no managed lifecycle fires the ensurer). These
tests pin the exact extra-topic sets each runner must provision, plus the shared
policy and the provision-before-bare-start ordering.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from calfcord._provisioning import (
    PROVISIONING,
    agent_infra_topics,
    bridge_infra_topics,
    provision_extra_topics,
)
from calfcord.control_plane.topics import (
    AGENT_STATE_TOPIC,
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)

_SERVERS = "h:9092"


def test_provisioning_policy_enabled_single_partition() -> None:
    # Enabled so calfkit creates referenced topics on a no-auto-create broker.
    assert PROVISIONING.enabled is True
    # agent.steps REQUIRES a single partition (ordering); nothing local needs more.
    assert PROVISIONING.num_partitions == 1
    assert PROVISIONING.replication_factor == 1


def test_bridge_infra_topics_are_the_control_plane_subscriber_and_boot_publish() -> None:
    # agent.state: raw state-consumer subscriber. bridge.discovery: published at
    # boot (discovery ping) before any agent may be up — bridge must ensure both.
    assert set(bridge_infra_topics()) == {AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC}


def test_agent_infra_topics_add_one_control_topic_per_agent() -> None:
    topics = agent_infra_topics(["alpha", "beta"])
    assert AGENT_STATE_TOPIC in topics
    assert BRIDGE_DISCOVERY_TOPIC in topics
    assert control_topic_for("alpha") in topics
    assert control_topic_for("beta") in topics


def test_agent_infra_topics_with_no_agents_is_just_the_shared_pair() -> None:
    assert set(agent_infra_topics([])) == {AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC}


async def test_provision_extra_topics_noop_on_empty_never_touches_kafka(monkeypatch) -> None:
    import calfcord._provisioning as mod

    def boom(*_a, **_k):  # pragma: no cover - asserts it is NOT called
        raise AssertionError("must not construct a provisioner for an empty topic set")

    monkeypatch.setattr(mod.TopicProvisioner, "from_connection", classmethod(lambda cls, **k: boom()))
    await provision_extra_topics(_SERVERS, [])


async def test_provision_extra_topics_dedups_and_forwards_explicit_server_urls(monkeypatch) -> None:
    """The bootstrap URL(s) are now passed in explicitly (calfkit 0.6.0 removed
    ``Client.server_urls``). calfcord uses no security, so no ``security_kwargs``
    are forwarded — the provisioner reuses the broker's plaintext connection."""
    from calfkit.provisioning import ProvisionReport

    import calfcord._provisioning as mod

    captured: dict = {}

    class FakeProvisioner:
        @classmethod
        def from_connection(cls, *, server_urls, config):
            captured["server_urls"] = server_urls
            captured["config"] = config
            return cls()

        async def provision(self, topics, *, framework_topics):
            captured["topics"] = list(topics)
            captured["framework_topics"] = framework_topics
            return ProvisionReport()

    monkeypatch.setattr(mod, "TopicProvisioner", FakeProvisioner)
    await provision_extra_topics(_SERVERS, ["a", "b", "a"])

    assert captured["server_urls"] == _SERVERS
    assert captured["config"] is PROVISIONING
    assert captured["topics"] == ["a", "b"]  # de-duplicated, first-seen order
    assert captured["framework_topics"] == set()  # plain data topics


def test_runner_reply_topics_are_pairwise_distinct() -> None:
    """Each runner's reply dispatcher needs its OWN topic. calfkit 0.6.0
    auto-provisions each at broker start, but a collision would still cross-wire
    reply delivery between processes — pin distinctness so a copy-paste can't.
    """
    from calfcord.mcp.runner import _REPLY_TOPIC as MCP
    from calfcord.tools.runner import _REPLY_TOPIC as TOOLS

    # "discord.outbox" is the bridge's outbox topic — its agents' ``reply_to``
    # return address and the outbox consumer's inbox. The bridge client itself no
    # longer names it as a reply inbox (it takes its own auto-generated inbox),
    # but the tools/mcp reply inboxes must still not collide with it.
    assert len({TOOLS, MCP, "discord.outbox"}) == 3


async def test_provision_extra_topics_propagates_provisioner_failure(monkeypatch) -> None:
    """A provisioning failure must abort startup LOUDLY (calfcord's
    infra-failure-raises rule), never be swallowed — a runner that cannot create
    its blind-spot topics must not come up and then silently stall on the wire.
    """
    import calfcord._provisioning as mod

    class FailingProvisioner:
        @classmethod
        def from_connection(cls, *, server_urls, config):
            return cls()

        async def provision(self, topics, *, framework_topics):
            raise RuntimeError("broker unreachable")

    monkeypatch.setattr(mod, "TopicProvisioner", FailingProvisioner)
    with pytest.raises(RuntimeError, match="broker unreachable"):
        await provision_extra_topics(_SERVERS, ["some.topic"])


async def test_provision_extra_topics_raises_on_unauthorized_report(monkeypatch) -> None:
    """A broker that authorizes the connection but DENIES create (ACL code 29)
    returns the topic in ``report.unauthorized`` — calfkit logs a warning but does
    NOT raise. Swallowing that lets the runner come up and then silently stall on
    the wire (a raw subscriber/publish to a topic that never gets created), so
    ``provision_extra_topics`` must raise loudly (calfcord's infra-failure-raises
    rule) rather than return cleanly.
    """
    from calfkit.provisioning import ProvisionReport

    import calfcord._provisioning as mod

    class UnauthorizedProvisioner:
        @classmethod
        def from_connection(cls, *, server_urls, config):
            return cls()

        async def provision(self, topics, *, framework_topics):
            return ProvisionReport(unauthorized=["calf.reply"])

    monkeypatch.setattr(mod, "TopicProvisioner", UnauthorizedProvisioner)
    with pytest.raises(RuntimeError, match=r"unauthorized.*calf\.reply"):
        await provision_extra_topics(_SERVERS, ["calf.reply"])


# --- provision_and_start_broker: the provision-BEFORE-bare-broker.start() invariant ---
# Regression fence for the hand-rolled (bridge/probe) paths: on those, the
# managed Worker lifecycle never fires, so calfcord must provision the node +
# blind-spot topics itself BEFORE the bare broker.start(). (The reply topic now
# auto-provisions via the connect-hook, so it is no longer this helper's job.)
# Centralizing the ordering here makes it unit-testable without a real broker;
# the gated integration test proves the same end-to-end against live Tansu.


def _client_with_recording_broker(order: list[str], *, running: bool = False) -> MagicMock:
    client = MagicMock()
    client.broker.running = running

    async def _start() -> None:
        order.append("start")

    client.broker.start = _start
    return client


async def test_provision_and_start_broker_orders_topics_then_start(monkeypatch) -> None:
    import calfcord._provisioning as mod

    order: list[str] = []

    async def _record_provision(server_urls, topics) -> None:
        order.append(f"provision:{server_urls}:{list(topics)}")

    monkeypatch.setattr(mod, "provision_extra_topics", _record_provision)
    client = _client_with_recording_broker(order)

    await mod.provision_and_start_broker(client, _SERVERS, ["node.in", "x.topic"])

    # blind-spot/node topics provisioned first, THEN bare broker.start().
    assert order == [f"provision:{_SERVERS}:['node.in', 'x.topic']", "start"]


async def test_provision_and_start_broker_skips_start_when_already_running(monkeypatch) -> None:
    import calfcord._provisioning as mod

    monkeypatch.setattr(mod, "provision_extra_topics", AsyncMock())
    client = MagicMock()
    client.broker.running = True
    client.broker.start = AsyncMock()

    await mod.provision_and_start_broker(client, _SERVERS, ["node.in"])

    client.broker.start.assert_not_awaited()
