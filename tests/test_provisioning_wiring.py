"""Tests for calfcord's opt-in topic-provisioning policy + blind-spot helpers.

calfkit 0.5.x creates the topics a Worker's nodes reference, but it walks node
``subscribe_topics``/``publish_topic`` only. calfcord has cross-process topics
that are raw FastStream broker subscribers, boot-time publish targets, or
no-subscriber callback topics — invisible to that walk. These tests pin the
exact extra-topic sets each runner must provision, plus the shared policy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from calfcord._provisioning import (
    PROVISIONING,
    agent_infra_topics,
    bridge_infra_topics,
    provision_extra_topics,
    provision_infra,
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


def _fake_client(*, server_urls: str = "h:9092", security_kwargs: dict | None = None) -> MagicMock:
    """A stand-in calfkit Client exposing only what the provisioning helpers read."""
    client = MagicMock()
    client.server_urls = server_urls
    client.security_kwargs = security_kwargs if security_kwargs is not None else {}
    return client


async def test_provision_extra_topics_noop_on_empty_never_touches_kafka(monkeypatch) -> None:
    import calfcord._provisioning as mod

    def boom(*_a, **_k):  # pragma: no cover - asserts it is NOT called
        raise AssertionError("must not construct a provisioner for an empty topic set")

    monkeypatch.setattr(mod.TopicProvisioner, "from_connection", classmethod(lambda cls, **k: boom()))
    await provision_extra_topics(_fake_client(), [])


async def test_provision_extra_topics_dedups_and_forwards_client_creds(monkeypatch) -> None:
    from calfkit.provisioning import ProvisionReport

    import calfcord._provisioning as mod

    captured: dict = {}

    class FakeProvisioner:
        @classmethod
        def from_connection(cls, *, server_urls, config, security_kwargs):
            captured["server_urls"] = server_urls
            captured["config"] = config
            captured["security_kwargs"] = security_kwargs
            return cls()

        async def provision(self, topics, *, framework_topics):
            captured["topics"] = list(topics)
            captured["framework_topics"] = framework_topics
            return ProvisionReport()

    monkeypatch.setattr(mod, "TopicProvisioner", FakeProvisioner)
    # Bootstrap + security kwargs are sourced from the connected client, so the
    # extras hit the same broker with the same credentials as the Worker's pass.
    client = _fake_client(server_urls="h:9092", security_kwargs={"security_protocol": "SASL_SSL"})
    await provision_extra_topics(client, ["a", "b", "a"])

    assert captured["server_urls"] == "h:9092"
    assert captured["config"] is PROVISIONING
    assert captured["security_kwargs"] == {"security_protocol": "SASL_SSL"}
    assert captured["topics"] == ["a", "b"]  # de-duplicated, first-seen order


def test_runner_reply_topics_are_pairwise_distinct() -> None:
    """Each runner's reply dispatcher needs its OWN topic provisioned before its
    broker.start() (router/tools/mcp pass these explicitly; the bridge reuses
    discord.outbox, its outbox-consumer node topic). A collision would cross-wire
    reply delivery between processes — pin distinctness so a copy-paste can't.
    """
    from calfcord.mcp.runner import _REPLY_TOPIC as MCP
    from calfcord.router.runner import _REPLY_TOPIC as ROUTER
    from calfcord.tools.runner import _REPLY_TOPIC as TOOLS

    # bridge's _REPLY_TOPIC is "discord.outbox" (the outbox consumer's inbox).
    assert len({ROUTER, TOOLS, MCP, "discord.outbox"}) == 4


async def test_provision_extra_topics_propagates_provisioner_failure(monkeypatch) -> None:
    """A provisioning failure must abort startup LOUDLY (calfcord's
    infra-failure-raises rule), never be swallowed — a runner that cannot create
    its reply/inbox topics must not come up and then silently stall on the wire
    (the exact failure mode this migration prevents).
    """
    import calfcord._provisioning as mod

    class FailingProvisioner:
        @classmethod
        def from_connection(cls, *, server_urls, config, security_kwargs):
            return cls()

        async def provision(self, topics, *, framework_topics):
            raise RuntimeError("broker unreachable")

    monkeypatch.setattr(mod, "TopicProvisioner", FailingProvisioner)
    with pytest.raises(RuntimeError, match="broker unreachable"):
        await provision_extra_topics(_fake_client(), ["some.topic"])


async def test_provision_extra_topics_raises_on_unauthorized_report(monkeypatch) -> None:
    """A broker that authorizes the connection but DENIES create (ACL code 29)
    comes back in ``report.unauthorized`` — calfkit logs a warning but does NOT
    raise. Swallowing that would let a runner proceed to ``worker.run()``'s direct
    ``broker.start()`` and hang forever on the un-created reply topic (calfkit#180),
    so ``provision_extra_topics`` must raise loudly rather than return cleanly.
    """
    from types import SimpleNamespace

    import calfcord._provisioning as mod

    class UnauthorizedProvisioner:
        @classmethod
        def from_connection(cls, *, server_urls, config, security_kwargs):
            return cls()

        async def provision(self, topics, *, framework_topics):
            # Mirrors calfkit's ProvisionReport: the denied topic lands in
            # ``unauthorized`` and provision() returns normally (does not raise).
            return SimpleNamespace(created=[], existing=[], unauthorized=["calf.reply"])

    monkeypatch.setattr(mod, "TopicProvisioner", UnauthorizedProvisioner)
    with pytest.raises(RuntimeError, match=r"unauthorized.*calf\.reply"):
        await provision_extra_topics(_fake_client(), ["calf.reply"])


# --- provision_infra: provision-only seam for the 0.5.4 worker.run() path ---
# Under the managed worker.run()/start() lifecycle the Worker owns broker.start(),
# so provision_infra ONLY fills the calfkit blind spots (the client reply topic for
# calf-ai/calfkit-sdk#180, plus calfcord's raw-subscriber/boot-publish extras). It
# must never touch the broker — that is the worker's job now.


async def test_provision_infra_provisions_reply_topic_only_by_default(monkeypatch) -> None:
    import calfcord._provisioning as mod

    captured: dict = {}

    async def _record_provision(client, topics) -> None:
        captured["topics"] = list(topics)

    monkeypatch.setattr(mod, "provision_extra_topics", _record_provision)
    client = _fake_client()
    client.reply_topic = "calf.reply"

    await provision_infra(client)

    # Default: just the client reply topic (the #180 blind spot).
    assert captured["topics"] == ["calf.reply"]


async def test_provision_infra_prepends_reply_topic_before_extras(monkeypatch) -> None:
    import calfcord._provisioning as mod

    captured: dict = {}

    async def _record_provision(client, topics) -> None:
        captured["topics"] = list(topics)

    monkeypatch.setattr(mod, "provision_extra_topics", _record_provision)
    client = _fake_client()
    client.reply_topic = "calf.reply"

    await provision_infra(client, extra_topics=["x.topic", "y.topic"])

    # Reply topic first, then the runner's blind-spot extras, in order.
    assert captured["topics"] == ["calf.reply", "x.topic", "y.topic"]


async def test_provision_infra_never_starts_the_broker(monkeypatch) -> None:
    """The 0.5.4 contract: the Worker owns broker.start(). provision_infra must
    NOT touch the broker — a stray start here would re-introduce the #180 reply-topic
    hang (a direct start before any invoke) on a no-auto-create broker."""
    import calfcord._provisioning as mod

    monkeypatch.setattr(mod, "provision_extra_topics", AsyncMock())
    client = _fake_client()
    client.reply_topic = "calf.reply"
    client.broker.start = AsyncMock()

    await provision_infra(client)

    client.broker.start.assert_not_awaited()
