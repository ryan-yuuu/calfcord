"""Gated REAL-broker self-enforcing canary for calf-ai/calfkit-sdk#180 — the
upstream gap that still forces calfcord to pre-provision the client reply topic
before a worker-owned ``broker.start()`` on Tansu.

Under the 0.5.4 managed lifecycle ``Worker.run()``/``Worker.start()`` performs a
DIRECT ``broker.start()``. On a broker that does NOT auto-create topics, that
start() blocks forever if the client's reply topic does not yet exist: calfkit
registers a reply-dispatcher subscriber on ``client.reply_topic`` at connect, but
only provisions that topic lazily on the first ``_invoke`` — which a direct start
never triggers. calfcord works around this in
:func:`calfcord._provisioning.provision_infra` by creating the reply topic before
handing the lifecycle to the worker.

This module pins #180 as a **self-announcing exit gate** rather than asserting the
*broken* behavior reproduces (a red test after a calfkit bump is the kind of noise
that gets xfail'd and forgotten, leaving the workaround as permanent cruft).
:func:`test_direct_start_succeeds_without_reply_topic_provisioning` asserts the
behavior we WANT (a direct start succeeds without the pre-provision) and is marked
``xfail(strict=True)``: while #180 is open the start hangs, the body times out, and
the test is an expected failure (silent); the moment calfkit fixes #180 the start
returns cleanly, the test XPASSes, and ``strict=True`` turns that XPASS into a hard
failure — forcing removal of ``provision_infra``'s reply-topic line (see the
``TODO(calfkit#180)`` there).

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

from calfcord._provisioning import PROVISIONING, provision_infra

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a NO-auto-create broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")
_START_OK_TIMEOUT = 15.0  # a healthy start returns in well under a second


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


@pytest.mark.xfail(
    strict=True,
    reason="calf-ai/calfkit-sdk#180: a direct broker.start() hangs without the reply "
    "topic pre-provisioned. XPASSes (hard-fails, by strict=True) when #180 lands — "
    "drop provision_infra's reply-topic line then.",
)
async def test_direct_start_succeeds_without_reply_topic_provisioning() -> None:
    """Self-enforcing #180 canary: provision ONLY the worker's node topics (NOT
    the client reply topic), then a direct ``broker.start()`` — the path
    ``Worker.run()``/``start()`` takes. We assert it SUCCEEDS within a generous
    timeout; while #180 is open it hangs and times out (expected failure). When
    calfkit provisions reply topics eagerly this returns cleanly, the test
    XPASSes, and ``strict=True`` flips it red — forcing removal of
    ``provision_infra``'s reply-topic workaround."""
    client = Client.connect(BOOTSTRAP, provisioning=ProvisioningConfig(enabled=True))
    worker = _build_worker(client)
    await worker.provision_topics()  # node topics only — reply topic left missing
    try:
        await asyncio.wait_for(client.broker.start(), timeout=_START_OK_TIMEOUT)
        assert client.broker.running
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def test_provision_infra_lets_managed_start_succeed_on_no_autocreate_broker() -> None:
    """Companion to the #180 canary above: with calfcord's ``provision_infra``
    pre-creating the client reply topic, the managed ``Worker.start()`` boot path
    (a DIRECT ``broker.start()`` — exactly what the migrated runners take via
    ``worker.run()``) comes up cleanly on a no-auto-create broker. This is the
    positive proof the workaround actually fixes the hang the canary reproduces,
    and it must PASS (not xfail) while #180 is open."""
    reply = f"itest.reply-{uuid.uuid4().hex[:8]}"
    client = Client.connect(BOOTSTRAP, reply_topic=reply, provisioning=PROVISIONING)

    inbox = f"itest.in-{uuid.uuid4().hex[:8]}"

    @consumer(subscribe_topics=inbox)
    def sink(_result: Any) -> None:  # NodeResult
        pass

    worker = Worker(client, [sink])
    # The migration's workaround: create the reply topic (+ any blind-spot
    # extras) BEFORE handing the lifecycle to the worker's direct broker.start().
    await provision_infra(client)
    try:
        # worker.start() registers handlers, provisions node topics, then starts
        # the broker directly — with the reply topic already present this must
        # NOT hang (the failure mode the canary above pins).
        await asyncio.wait_for(worker.start(), timeout=_START_OK_TIMEOUT)
        assert client.broker.running
    finally:
        with contextlib.suppress(Exception):
            await worker.stop()
        with contextlib.suppress(Exception):
            await client.close()
