"""Gated REAL-broker test for the production broker-reachability healthcheck.

The Process Compose readiness probe (``disco _healthcheck broker``) must prove
the broker can *serve metadata*, not just that a port is bound — Tansu is
no-auto-create, so a bound socket alone does not mean it can answer (design §12.1
/ §13.2). :func:`calfcord.health.check.default_broker_probe` does a real
``list_topics`` metadata fetch; this exercises it against a live broker and
asserts a healthy (``0``) verdict via the same :func:`~calfcord.health.check.healthcheck`
the CLI runs.

Gated behind ``CALF_TEST_KAFKA`` (mirrors ``test_broker_startup_provisioning.py``
and calfkit's integration lane): with no such broker configured it skips cleanly.
Point it at a real broker to run::

    docker run -d -p 9092:9092 ghcr.io/tansu-io/tansu:latest broker
    CALF_TEST_KAFKA=1 CALF_TEST_KAFKA_BOOTSTRAP=localhost:9092 uv run pytest \\
        tests/integration/test_healthcheck_broker.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from calfcord.health.check import default_broker_probe, healthcheck

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_KAFKA"),
    reason="set CALF_TEST_KAFKA=1 (+ CALF_TEST_KAFKA_BOOTSTRAP) against a real broker (e.g. Tansu)",
)

BOOTSTRAP = os.getenv("CALF_TEST_KAFKA_BOOTSTRAP", "localhost:9092")


async def test_production_broker_probe_reports_reachable_against_live_broker() -> None:
    """The real metadata-fetch probe answers True against a live broker."""
    probe = default_broker_probe(BOOTSTRAP)
    assert await probe() is True


async def test_healthcheck_broker_is_healthy_against_live_broker() -> None:
    """End-to-end: the CLI's healthcheck(broker) returns 0 with the production
    probe against a live broker — the exact verdict the exec probe ships."""
    probe = default_broker_probe(BOOTSTRAP)
    code = await healthcheck(".", "broker", now=datetime.now(UTC), broker_probe=probe)
    assert code == 0
