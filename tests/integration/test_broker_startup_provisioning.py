"""Gated REAL-broker regression test for the provision-before-broker.start()
invariant — the bug that hung calfcord runners on Tansu.

The router/tools/mcp/agents runners bring their reply dispatcher up with a
DIRECT ``await client.broker.start()``. On a broker that does NOT auto-create
topics, that start() blocks forever if the client's reply topic does not yet
exist: calfkit registers a reply-dispatcher subscriber on ``client.reply_topic``
at connect, but only provisions that topic lazily on the first ``_invoke`` —
which these processes never do. The fix is to provision the reply topic (and
each runner's node/infra topics) BEFORE the direct broker.start(). This module
exercises that mechanism against a live no-auto-create broker.

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

from calfcord._provisioning import provision_extra_topics

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a NO-auto-create broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")
_START_OK_TIMEOUT = 15.0  # a healthy start returns in well under a second
_HANG_PROBE_TIMEOUT = 6.0  # long enough to distinguish a hang from a clean start


async def _build_hand_rolled(*, provision_reply: bool) -> Any:
    """Replicate a runner's hand-rolled lifecycle up to (not including) start.

    Connect with an auto-generated reply topic (as the agents runner does),
    register a node, provision node topics, and — only when ``provision_reply``
    — provision the client reply topic. Returns the connected client; the caller
    owns the direct ``broker.start()`` and cleanup.
    """
    inbox = f"itest.in-{uuid.uuid4().hex[:8]}"
    client = Client.connect(BOOTSTRAP, provisioning=ProvisioningConfig(enabled=True))

    @consumer(subscribe_topics=inbox)
    def sink(_result: Any) -> None:  # NodeResult
        pass

    worker = Worker(client, [sink])
    worker.register_handlers()
    await worker.provision_topics()
    await provision_extra_topics(BOOTSTRAP, [client.reply_topic] if provision_reply else [])
    return client


async def test_hand_rolled_start_succeeds_when_reply_topic_provisioned() -> None:
    """The fix: with the client reply topic provisioned, a direct broker.start()
    returns cleanly on a no-auto-create broker."""
    client = await _build_hand_rolled(provision_reply=True)
    try:
        await asyncio.wait_for(client.broker.start(), timeout=_START_OK_TIMEOUT)
        assert client.broker.running
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def test_hand_rolled_start_hangs_without_reply_topic_provisioning() -> None:
    """The bug this fix guards against: without provisioning the reply topic, the
    direct broker.start() blocks on the missing topic. Asserting the hang both
    documents the failure mode and acts as a canary — if calfkit ever starts
    provisioning reply topics eagerly, this stops timing out and the per-runner
    workaround can be removed."""
    client = await _build_hand_rolled(provision_reply=False)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.broker.start(), timeout=_HANG_PROBE_TIMEOUT)
    finally:
        with contextlib.suppress(Exception):
            await client.close()
