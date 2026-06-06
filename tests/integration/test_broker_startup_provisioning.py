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

from calfcord._provisioning import provision_and_start_broker

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a NO-auto-create broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")
_START_OK_TIMEOUT = 15.0  # a healthy start returns in well under a second
_HANG_PROBE_TIMEOUT = 6.0  # long enough to distinguish a hang from a clean start


def _build_worker(client: Any) -> Any:
    """Register a consumer node on ``client`` — a runner's pre-start state (the
    agents runner connects with an auto-generated reply topic, as here)."""
    inbox = f"itest.in-{uuid.uuid4().hex[:8]}"

    @consumer(subscribe_topics=inbox)
    def sink(_result: Any) -> None:  # NodeResult
        pass

    worker = Worker(client, [sink])
    worker.register_handlers()
    return worker


async def test_provision_and_start_broker_starts_cleanly_on_no_autocreate_broker() -> None:
    """The fix: provision_and_start_broker provisions the client reply topic +
    the worker's node topics, so a hand-rolled direct broker.start() returns
    cleanly on a broker that does not auto-create topics."""
    client = Client.connect(BOOTSTRAP, provisioning=ProvisioningConfig(enabled=True))
    worker = _build_worker(client)
    try:
        await asyncio.wait_for(
            provision_and_start_broker(client, worker=worker),
            timeout=_START_OK_TIMEOUT,
        )
        assert client.broker.running
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def test_direct_start_hangs_without_reply_topic_provisioning() -> None:
    """The bug this fix guards against: provisioning only the node topics (NOT
    the client reply topic) and then a direct broker.start() blocks on the
    missing reply topic. Asserting the hang documents the failure mode and acts
    as a canary — if calfkit ever provisions reply topics eagerly, this stops
    timing out and provision_and_start_broker's reply-topic step can be dropped
    (tracked upstream: calf-ai/calfkit-sdk#180)."""
    client = Client.connect(BOOTSTRAP, provisioning=ProvisioningConfig(enabled=True))
    worker = _build_worker(client)
    await worker.provision_topics()  # node topics only — reply topic left missing
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.broker.start(), timeout=_HANG_PROBE_TIMEOUT)
    finally:
        with contextlib.suppress(Exception):
            await client.close()
