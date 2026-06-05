"""Tests for calfcord's opt-in topic-provisioning policy + blind-spot helpers.

calfkit 0.5.x creates the topics a Worker's nodes reference, but it walks node
``subscribe_topics``/``publish_topic`` only. calfcord has cross-process topics
that are raw FastStream broker subscribers, boot-time publish targets, or
no-subscriber callback topics — invisible to that walk. These tests pin the
exact extra-topic sets each runner must provision, plus the shared policy.
"""

from __future__ import annotations

from calfcord._provisioning import (
    PROVISIONING,
    agent_infra_topics,
    bridge_infra_topics,
    provision_extra_topics,
    router_infra_topics,
)
from calfcord.control_plane.topics import (
    AGENT_STATE_TOPIC,
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)
from calfcord.topics import AMBIENT_REPLY_DISCARD_TOPIC


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


def test_router_infra_topics_is_the_no_subscriber_discard_topic() -> None:
    assert router_infra_topics() == [AMBIENT_REPLY_DISCARD_TOPIC]


async def test_provision_extra_topics_noop_on_empty_never_touches_kafka(monkeypatch) -> None:
    import calfcord._provisioning as mod

    def boom(*_a, **_k):  # pragma: no cover - asserts it is NOT called
        raise AssertionError("must not construct a provisioner for an empty topic set")

    monkeypatch.setattr(mod.TopicProvisioner, "from_connection", classmethod(lambda cls, **k: boom()))
    await provision_extra_topics("localhost:9092", [])


async def test_provision_extra_topics_dedups_and_forwards_to_provisioner(monkeypatch) -> None:
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
    await provision_extra_topics("h:9092", ["a", "b", "a"])

    assert captured["server_urls"] == "h:9092"
    assert captured["config"] is PROVISIONING
    assert captured["topics"] == ["a", "b"]  # de-duplicated, first-seen order
