"""Unit tests for the readiness healthcheck (design §12.1 / §13.2).

:func:`calfcord.health.check.healthcheck` is the logic behind the
``disco _healthcheck <component>`` exec probe Process Compose runs on the
agent/tools hosts. It returns a POSIX exit code — ``0`` healthy, ``1`` not — so
the readiness tests assert the int code, not an exception.

Only **two** components carry a readiness signal, so only two are probeable
(design §12.1): the broker (metadata reachability) and the bridge (heartbeat
freshness, which the bridge only beats once Discord is connected). Each source is
isolated and injected so the tests stay offline and deterministic:

* the broker is judged by an injected ``broker_probe`` coroutine (the production
  default does a real metadata fetch; here a stub stands in), so "broker
  reachable" never needs a live Kafka;
* the bridge is judged by heartbeat freshness against an injected ``now`` and a
  tmp home, so freshness boundaries are exact.

Any other component (an agent id, ``tools``, ``router``) has no
readiness signal — those roster runners never beat — so probing one is a
programming/config bug, and the probe raises rather than fabricating a verdict.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from calfcord.health.check import default_broker_probe, healthcheck
from calfcord.health.heartbeat import write_beat

_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)


async def _reachable() -> bool:
    return True


async def _unreachable() -> bool:
    return False


async def test_broker_component_healthy_when_probe_reports_reachable(tmp_path: Path) -> None:
    code = await healthcheck(tmp_path, "broker", now=_NOW, broker_probe=_reachable)
    assert code == 0


async def test_broker_component_unhealthy_when_probe_reports_unreachable(tmp_path: Path) -> None:
    code = await healthcheck(tmp_path, "broker", now=_NOW, broker_probe=_unreachable)
    assert code == 1


async def test_bridge_component_healthy_with_fresh_beat(tmp_path: Path) -> None:
    # A heartbeat written ttl/2 ago is fresh -> the bridge is ready. The broker
    # probe must NOT be consulted for the bridge, so a stub that would flip the
    # verdict (reachable) proves the heartbeat path is what decides.
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    code = await healthcheck(
        tmp_path,
        "bridge",
        now=_NOW + timedelta(seconds=5),
        ttl_seconds=10,
        broker_probe=_unreachable,
    )
    assert code == 0


async def test_bridge_component_unhealthy_with_stale_beat(tmp_path: Path) -> None:
    # A beat older than ttl is stale (a wedged/killed bridge stops refreshing) ->
    # not ready, even though the file exists.
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    code = await healthcheck(
        tmp_path,
        "bridge",
        now=_NOW + timedelta(seconds=11),
        ttl_seconds=10,
        broker_probe=_unreachable,
    )
    assert code == 1


async def test_bridge_component_unhealthy_when_beat_missing(tmp_path: Path) -> None:
    # No heartbeat ever written (bridge not up, or another host) -> not ready,
    # without crashing the probe.
    code = await healthcheck(tmp_path, "bridge", now=_NOW, broker_probe=_unreachable)
    assert code == 1


async def test_unrecognized_component_raises_with_context(tmp_path: Path) -> None:
    # Only the broker and the bridge carry a readiness signal; the roster runners
    # (agents, tools, router) go through run_worker_until_signal and never
    # beat, so they have no heartbeat to read. Probing one is a programming/config
    # bug — Process Compose only ever generates broker/bridge probes — so the
    # function must RAISE with the offending component (the error convention's
    # loud-raise-for-an-infra/programming-bug, not a fabricated "not ready" verdict
    # that would lie about a readiness signal that does not exist).
    with pytest.raises(RuntimeError, match="some-agent"):
        await healthcheck(tmp_path, "some-agent", now=_NOW, broker_probe=_unreachable)


class _FakeAdmin:
    """A stand-in for aiokafka's AIOKafkaAdminClient (the metadata-fetch seam).

    Records the lifecycle the production probe drives — ``start`` → metadata fetch
    → ``close`` — so tests can assert the round-trip happens AND the client is
    always closed, even on failure.
    """

    closed = False

    def __init__(self, *, fail_on: str | None = None, **kwargs: object) -> None:
        self.fail_on = fail_on
        self.kwargs = kwargs

    async def start(self) -> None:
        if self.fail_on == "start":
            raise OSError("connection refused")

    async def list_topics(self) -> list[str]:
        if self.fail_on == "list_topics":
            raise OSError("metadata timeout")
        return ["agent.state"]

    async def close(self) -> None:
        type(self).closed = True


async def test_default_broker_probe_reports_reachable_on_metadata_fetch() -> None:
    # A successful metadata round-trip (list_topics) -> reachable. The admin-client
    # constructor is injected so no live Kafka is needed; production lazy-imports it.
    _FakeAdmin.closed = False
    probe = default_broker_probe("localhost:9092", admin_factory=_FakeAdmin)
    assert await probe() is True
    assert _FakeAdmin.closed is True


async def test_default_broker_probe_unreachable_when_start_fails() -> None:
    # A bound-but-dead broker (or a missing one) fails at connect/start -> the
    # probe must return False, NOT raise, so the exec probe reports "not ready".
    def factory(**kwargs: object) -> _FakeAdmin:
        return _FakeAdmin(fail_on="start", **kwargs)

    probe = default_broker_probe("localhost:9092", admin_factory=factory)
    assert await probe() is False


async def test_default_broker_probe_unreachable_when_metadata_fetch_fails() -> None:
    # Port bound and start() ok, but the broker cannot serve metadata (the exact
    # Tansu no-auto-create failure mode bare TCP would miss) -> not reachable, and
    # the client is still closed.
    _FakeAdmin.closed = False

    def factory(**kwargs: object) -> _FakeAdmin:
        return _FakeAdmin(fail_on="list_topics", **kwargs)

    probe = default_broker_probe("localhost:9092", admin_factory=factory)
    assert await probe() is False
    assert _FakeAdmin.closed is True


async def test_default_broker_probe_unreachable_when_admin_construction_raises() -> None:
    # If the admin-client CONSTRUCTOR itself raises (e.g. a malformed bootstrap),
    # the probe must still report False per its never-raises contract — not
    # propagate the exception out of the exec probe.
    def factory(**kwargs: object) -> _FakeAdmin:
        raise ValueError("bad bootstrap")

    probe = default_broker_probe("localhost:9092", admin_factory=factory)
    assert await probe() is False


async def test_default_broker_probe_uses_real_aiokafka_client_and_degrades() -> None:
    # With no injected factory, the probe lazy-imports aiokafka's real admin
    # client and connects. Pointed at an unbound localhost port (no broker), the
    # metadata fetch fails and the probe must report unreachable — never raise —
    # exercising the production default factory + the graceful-degrade path
    # without needing a live broker.
    probe = default_broker_probe("127.0.0.1:1")  # port 1: nothing listens
    assert await probe() is False


# The exec probe runs on the agent/tools hosts, so the admin client (aiokafka)
# must stay lazy so the import is pure-filesystem. A fresh interpreter gives a
# clean ``sys.modules`` to assert against; mirrors ``tests/health/test_heartbeat.py``.
_ISOLATION_SCRIPT = """
import sys

import calfcord.health.check  # noqa: F401

# aiokafka must be lazy-imported inside the broker probe, not at module load.
aiokafka_leaked = any(m == "aiokafka" or m.startswith("aiokafka.") for m in sys.modules)
assert not aiokafka_leaked, "health.check eagerly imported aiokafka (must be lazy in the probe)"
print("ISOLATION_OK")
"""


def test_check_does_not_import_aiokafka() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"isolation subprocess failed (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ISOLATION_OK" in result.stdout, (
        "isolation subprocess exited 0 but did not run to completion\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
