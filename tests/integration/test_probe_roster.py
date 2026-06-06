"""Gated REAL-broker test for the control-plane roster probe.

``probe_live_roster`` reconstructs the live agent roster from any host by talking
only to the broker: it subscribes ``agent.state`` at ``auto_offset_reset="latest"``,
broadcasts a discovery ping, collects responses for a bounded window, and reduces
them (no dependency on the bridge's in-memory registry). This exercises that
end-to-end wiring against a live no-auto-create broker.

Gated behind ``CALF_TEST_KAFKA`` (mirrors the broker-startup test): with no such
broker configured it skips cleanly. Point it at native Tansu to run for real::

    docker run -d -p 9092:9092 ghcr.io/tansu-io/tansu:latest broker
    CALF_TEST_KAFKA=1 CALF_TEST_KAFKA_BOOTSTRAP=localhost:9092 uv run pytest \\
        tests/integration/test_probe_roster.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime

import pytest
from calfkit import Client

from calfcord._provisioning import PROVISIONING, provision_and_start_broker
from calfcord.control_plane.probe import probe_live_roster
from calfcord.control_plane.publish import publish_state_event
from calfcord.control_plane.schema import AgentStateEvent
from calfcord.control_plane.topics import AGENT_STATE_TOPIC

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a NO-auto-create broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")


def _state_event(agent_id: str) -> AgentStateEvent:
    return AgentStateEvent(
        agent_id=agent_id,
        display_name=agent_id.capitalize(),
        description="Integration-test agent.",
        role="assistant",
        history_turns=20,
        provider="anthropic",
        emitted_at=datetime.now(UTC),
        cause="discovery_response",
    )


async def _announce_repeatedly(agent_ids: list[str], *, rounds: int, every_s: float) -> None:
    """Publish state events for ``agent_ids`` repeatedly, simulating live agents.

    Publishing across the whole probe window (rather than once) makes the test
    robust to consumer-group join latency: whenever the probe's ``latest``
    subscriber becomes live, a fresh announcement lands within its window.
    """
    async with Client.connect(BOOTSTRAP, provisioning=PROVISIONING) as pub:
        await provision_and_start_broker(pub, extra_topics=[AGENT_STATE_TOPIC])
        for _ in range(rounds):
            for agent_id in agent_ids:
                await publish_state_event(pub, _state_event(agent_id))
            await asyncio.sleep(every_s)


async def test_probe_collects_agents_announcing_in_its_window() -> None:
    agent_ids = ["itest-sage", "itest-scribe"]
    announcer = asyncio.create_task(
        _announce_repeatedly(agent_ids, rounds=14, every_s=0.5)
    )
    try:
        roster = await probe_live_roster(BOOTSTRAP, timeout_s=4.0)
    finally:
        announcer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await announcer

    got = {definition.agent_id for definition in roster}
    # Subset assertion: a shared broker may carry other real agents too.
    assert set(agent_ids) <= got
