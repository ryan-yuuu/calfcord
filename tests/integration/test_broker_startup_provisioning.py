"""Gated REAL-broker test for the hand-rolled provision-before-bare-start path.

After the calfkit 0.12 migration removed the bespoke control plane, no in-tree
runner hand-rolls a raw ``broker.subscriber(...)`` + BARE ``await
client.broker.start()`` anymore — the agents/tools runners and the bridge all use
the managed ``Worker`` lifecycle (``run()`` / embedded ``start()``), which
declares their node topics for them.
:func:`calfcord._provisioning.provision_and_start_broker` survives as a
general-purpose helper for any future caller that DOES hand-roll that path. On a
broker that does NOT auto-create topics (e.g. Tansu) a bare start blocks forever
if any subscribed topic does not yet exist, so that path must provision its
topics first.

calfkit 0.6.0 changed the provisioning surface this rests on:

* the **client reply topic** is now auto-provisioned on EVERY ``broker.start()``
  path via a connect-time pre-start hook (calf-ai/calfkit-sdk#180), so the old
  "a bare start hangs on the missing reply topic" canary is now FALSE; but
* a **Worker's node topics** are auto-provisioned only on the MANAGED run
  surfaces (``Worker.run()`` / ``start()`` / ``async with``), NOT on the
  hand-rolled ``register_handlers()`` + bare ``broker.start()`` path — so a
  hand-rolled caller must still provision its node topics itself
  (``topics_for_nodes`` over its node list) before the bare start.

These tests pin both halves against a live no-auto-create broker:

1. the hand-rolled path with calfcord's ``provision_and_start_broker`` (node
   topics provisioned, reply topic auto-provisioned) starts cleanly AND the
   worker actually consumes a published message; and
2. calfkit 0.6.0 really does auto-provision the reply topic on a bare start
   (the positive form of the retired #180 canary).

Gated behind ``CALF_TEST_KAFKA`` (mirrors calfkit's integration lane): with no
such broker configured it skips cleanly. Point it at native Tansu to run for
real::

    docker run -d -p 9092:9092 ghcr.io/tansu-io/tansu:latest broker
    CALF_TEST_KAFKA=1 CALF_TEST_KAFKA_BOOTSTRAP=localhost:9092 uv run pytest \\
        tests/integration/test_broker_startup_provisioning.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from typing import Any

import pytest
from calfkit import Client, ProvisioningConfig, Worker
from calfkit.nodes import consumer
from calfkit.provisioning import TopicProvisioner, topics_for_nodes

from calfcord._provisioning import provision_and_start_broker

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a NO-auto-create broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")
_START_OK_TIMEOUT = 15.0  # a healthy start returns in well under a second
_CONSUME_TIMEOUT = 15.0  # join the group + receive one published message


def _build_worker(client: Any, inbox: str) -> Any:
    """Register a consumer node on ``client`` subscribed to ``inbox`` — the
    hand-rolled runner's pre-start state (register_handlers, no Worker.run())."""

    @consumer(subscribe_topics=inbox)
    def sink(_result: Any) -> None:  # ConsumerContext
        pass

    worker = Worker(client, [sink])
    worker.register_handlers()
    return worker


async def test_hand_rolled_start_is_clean_and_consumes_on_no_autocreate_broker() -> None:
    """The new guarantee: a hand-rolled register_handlers() + bare broker.start()
    with calfcord provisioning (node topics via topics_for_nodes; reply topic via
    calfkit's connect-hook) does NOT hang on a no-auto-create broker, and the
    worker actually consumes a message published to its subscribed topic."""
    inbox = f"itest.in-{uuid.uuid4().hex[:8]}"
    consumed = asyncio.Event()

    @consumer(subscribe_topics=inbox)
    async def sink(_message: Any) -> None:
        consumed.set()

    client = Client.connect(BOOTSTRAP, provisioning=ProvisioningConfig(enabled=True))
    worker = Worker(client, [sink])
    worker.register_handlers()
    try:
        # Hand-rolled provisioning: node topics (the bare start would hang on the
        # missing inbox otherwise) + bare start (which auto-creates the reply
        # topic via the connect-hook). Must not hang.
        await asyncio.wait_for(
            provision_and_start_broker(client, BOOTSTRAP, topics_for_nodes([sink])),
            timeout=_START_OK_TIMEOUT,
        )
        assert client.broker.running

        # Prove the subscriber is genuinely live, not merely "start() returned":
        # a separate client publishes to the inbox and the handler must fire.
        async with Client.connect(BOOTSTRAP, provisioning=ProvisioningConfig(enabled=True)) as producer:
            await producer.send("ping", inbox)
            await asyncio.wait_for(consumed.wait(), timeout=_CONSUME_TIMEOUT)
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def test_bare_start_auto_provisions_reply_topic() -> None:
    """calfkit 0.6.0 auto-provisions the client reply topic on a bare
    broker.start() (the connect-time pre-start hook; calf-ai/calfkit-sdk#180).

    The positive form of the retired #180 hang canary: provision ONLY the node
    topics and do a direct broker.start() with NO explicit reply-topic
    provisioning — it returns cleanly (no hang), and the reply topic exists
    afterwards. If this ever regresses, the hand-rolled runners would hang again.
    """
    reply_topic = f"itest-reply-{uuid.uuid4().hex[:8]}"
    client = Client.connect(
        BOOTSTRAP,
        reply_topic=reply_topic,
        provisioning=ProvisioningConfig(enabled=True),
    )
    inbox = f"itest.in-{uuid.uuid4().hex[:8]}"
    worker = _build_worker(client, inbox)
    try:
        # Provision node topics only — the reply topic is deliberately left to
        # calfkit's connect-hook. A bare start must NOT hang on it.
        await TopicProvisioner.from_connection(
            server_urls=BOOTSTRAP, config=ProvisioningConfig(enabled=True)
        ).provision(topics_for_nodes([worker._nodes[0]]), framework_topics=set())
        await asyncio.wait_for(client.broker.start(), timeout=_START_OK_TIMEOUT)
        assert client.broker.running

        # The reply topic now exists: a fresh provisioner reports it "existing",
        # not "created" — direct evidence the connect-hook created it on start.
        report = await TopicProvisioner.from_connection(
            server_urls=BOOTSTRAP, config=ProvisioningConfig(enabled=True)
        ).provision([reply_topic], framework_topics={reply_topic})
        assert reply_topic in report.existing
        assert reply_topic not in report.created
    finally:
        with contextlib.suppress(Exception):
            await client.close()
