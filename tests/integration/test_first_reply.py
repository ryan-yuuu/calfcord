"""Gated REAL-broker test for first-reply detection.

:func:`wait_for_first_reply` watches ``discord.outbox`` for the FIRST reply from
a target agent — the init wizard's live-finish "the org answered" signal — by
registering a calfkit consumer in its own group (so the agent identity is
recovered from the Kafka headers, exactly as the bridge's outbox consumer does)
and bounding the wait. This exercises that wiring end-to-end against a live
no-auto-create broker: a synthetic agent ``ReturnCall`` envelope is published to
``discord.outbox`` with the ``x-calf-emitter`` / ``x-calf-emitter-kind`` headers
an agent reply carries, and the watcher must detect it.

Gated behind ``CALF_TEST_KAFKA`` (mirrors the probe-roster test): with no such
broker configured it skips cleanly. Point it at native Tansu to run for real::

    docker run -d -p 9092:9092 ghcr.io/tansu-io/tansu:latest broker
    CALF_TEST_KAFKA=1 CALF_TEST_KAFKA_BOOTSTRAP=localhost:9092 uv run pytest \\
        tests/integration/test_first_reply.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from calfkit import Client
from calfkit._protocol import HDR_EMITTER, HDR_EMITTER_KIND
from calfkit.models import State, TextPart
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    SessionRunContext,
    WorkflowState,
)

from calfcord._provisioning import PROVISIONING, provision_and_start_broker
from calfcord.control_plane.first_reply import wait_for_first_reply
from calfcord.topics import DISCORD_OUTBOX_TOPIC

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a NO-auto-create broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")


def _agent_reply_envelope(text: str) -> Envelope:
    """A synthetic agent ``ReturnCall`` envelope carrying a final text reply.

    Matches the shape an assistant agent emits: ``final_output_parts`` holds the
    reply text (the watcher's gate requires it non-empty) inside an otherwise
    minimal session context. The emitter identity is NOT in this body — it rides
    the Kafka headers stamped at publish time (see ``_publish_reply``)."""
    state = State()
    state.final_output_parts = [TextPart(text=text)]
    call_stack = CallFrameStack()
    call_stack.push(
        CallFrame(
            target_topic=DISCORD_OUTBOX_TOPIC,
            callback_topic=DISCORD_OUTBOX_TOPIC,
        )
    )
    return Envelope(
        internal_workflow_state=WorkflowState(call_stack=call_stack),
        context=SessionRunContext(state=state, deps={}),
    )


async def _publish_reply_repeatedly(
    agent_id: str, *, rounds: int, every_s: float
) -> None:
    """Publish synthetic agent replies for ``agent_id`` across the watch window.

    Republishing (rather than once) makes the test robust to consumer-group join
    latency: whenever the watcher's ``latest`` subscriber becomes live, a fresh
    reply lands within its window. Each publish stamps the emitter headers an
    agent's ``ReturnCall`` carries, so the watcher recovers the identity exactly
    as it would from a real reply.
    """
    envelope = _agent_reply_envelope("Hello from the org!")
    headers = {HDR_EMITTER: agent_id, HDR_EMITTER_KIND: "agent"}
    async with Client.connect(BOOTSTRAP, provisioning=PROVISIONING) as pub:
        # Hand-rolled publisher: only publishes to discord.outbox (a blind spot
        # for the ensurer, which sees no Worker nodes here). The reply topic
        # auto-provisions on the bare start inside the helper.
        await provision_and_start_broker(pub, BOOTSTRAP, [DISCORD_OUTBOX_TOPIC])
        for _ in range(rounds):
            await pub._connection.publish(
                envelope.model_dump(mode="json"),
                topic=DISCORD_OUTBOX_TOPIC,
                headers=headers,
            )
            await asyncio.sleep(every_s)


async def test_detects_reply_from_target_agent() -> None:
    agent_id = "itest-firstreply-assistant"
    publisher = asyncio.create_task(
        _publish_reply_repeatedly(agent_id, rounds=20, every_s=0.3)
    )
    try:
        detected = await wait_for_first_reply(
            BOOTSTRAP, agent_id=agent_id, timeout_s=6.0
        )
    finally:
        publisher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await publisher

    assert detected is True


async def test_times_out_when_no_reply_from_target_agent() -> None:
    """A reply from a DIFFERENT agent must not satisfy a watch scoped to ours;
    with nobody else replying either, the watcher returns False on a clean,
    bounded timeout (never hangs)."""
    other = asyncio.create_task(
        _publish_reply_repeatedly("itest-firstreply-other", rounds=10, every_s=0.3)
    )
    try:
        detected = await wait_for_first_reply(
            BOOTSTRAP, agent_id="itest-firstreply-absent", timeout_s=3.0
        )
    finally:
        other.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await other

    assert detected is False
