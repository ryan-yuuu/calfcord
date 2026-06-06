"""Unit tests for the heartbeat refresher (design §4.2 / §12.1).

A long-lived runner refreshes its heartbeat every few seconds, but ONLY while it
is healthy: the refresher takes a ``is_healthy`` predicate and writes a beat IFF
it returns ``True``. When the runner goes silently unhealthy (e.g. the bridge's
Discord gateway drops), the refresher *skips* the write, ``last_beat`` stops
advancing, and the beat goes stale within the TTL — which the readiness probe
reads as "not ready" (§12.1: "heartbeat must reflect Discord connection state").

Everything here is deterministic and offline: ``now`` is injected (aware UTC) so
freshness is exact, ``is_healthy``/``identity`` are plain callables, and the loop
driver's ``sleep`` is injected so beat counts are asserted with zero wall-clock
sleeps.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from calfcord.health.heartbeat import read_beat
from calfcord.health.refresher import refresh_once, run_refresher

_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)


def _always(value: bool):
    """A constant predicate ``() -> value`` (a stand-in connection-state check)."""
    return lambda: value


def _identity(name: str | None):
    """A constant identity getter ``() -> name`` (a stand-in bot-identity getter)."""
    return lambda: name


def test_refresh_once_writes_when_healthy(tmp_path: Path) -> None:
    # Healthy → a beat is written and the return value reports the write happened.
    wrote = refresh_once(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity("Calfbot#1234"),
        now=_NOW,
    )
    assert wrote is True
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.last_beat == _NOW


def test_refresh_once_skips_when_unhealthy(tmp_path: Path) -> None:
    # Unhealthy → NO write (so last_beat stops advancing and the beat goes stale,
    # which the readiness probe reads as "not ready" — the whole point of §12.1).
    wrote = refresh_once(
        tmp_path,
        "bridge",
        is_healthy=_always(False),
        identity=_identity("Calfbot#1234"),
        now=_NOW,
    )
    assert wrote is False
    assert read_beat(tmp_path, "bridge") is None


def test_refresh_once_does_not_advance_existing_beat_when_unhealthy(
    tmp_path: Path,
) -> None:
    # A previously-written beat must NOT be refreshed once the runner is unhealthy;
    # last_beat stays pinned to the last healthy tick so freshness can expire.
    refresh_once(tmp_path, "bridge", is_healthy=_always(True), identity=_identity(None), now=_NOW)
    later = _NOW + timedelta(seconds=3)
    wrote = refresh_once(
        tmp_path, "bridge", is_healthy=_always(False), identity=_identity(None), now=later
    )
    assert wrote is False
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.last_beat == _NOW  # not advanced to `later`


def test_refresh_once_uses_the_identity_and_status(tmp_path: Path) -> None:
    # The identity callable and status are threaded through to the persisted beat.
    refresh_once(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity("Calfbot#1234"),
        status="connected",
        now=_NOW,
    )
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.identity == "Calfbot#1234"
    assert beat.status == "connected"


def test_refresh_once_status_defaults_to_healthy(tmp_path: Path) -> None:
    refresh_once(tmp_path, "bridge", is_healthy=_always(True), identity=_identity(None), now=_NOW)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.status == "healthy"


def test_refresh_once_advances_last_beat_while_preserving_started_at(
    tmp_path: Path,
) -> None:
    # Across healthy ticks last_beat advances but started_at stays pinned to the
    # first beat (write_beat's invariant) — so status can later detect flapping.
    refresh_once(tmp_path, "bridge", is_healthy=_always(True), identity=_identity(None), now=_NOW)
    later = _NOW + timedelta(seconds=2)
    refresh_once(tmp_path, "bridge", is_healthy=_always(True), identity=_identity(None), now=later)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.started_at == _NOW
    assert beat.last_beat == later


@pytest.mark.asyncio
async def test_run_refresher_writes_n_beats_over_n_ticks(tmp_path: Path) -> None:
    # The loop driver ticks then awaits sleep(interval), repeating until cancelled.
    # An injected sleep counts the awaits and cancels after N, with a monotonically
    # advancing injected clock — so we can assert the persisted beat reflects N
    # ticks (last_beat == the Nth clock value) with zero real wall-clock sleeps.
    n = 3
    interval = 2.0
    times = [_NOW + timedelta(seconds=interval * i) for i in range(n)]
    clock_calls = iter(times)
    sleep_count = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= n:
            raise asyncio.CancelledError

    await run_refresher(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity(None),
        interval_seconds=interval,
        clock=lambda: next(clock_calls),
        sleep=fake_sleep,
    )

    # N ticks happened (one per sleep await before the cancel landed).
    assert sleep_count == n
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.started_at == times[0]
    assert beat.last_beat == times[n - 1]


@pytest.mark.asyncio
async def test_run_refresher_returns_cleanly_on_cancel(tmp_path: Path) -> None:
    # A CancelledError from the injected sleep must return cleanly (not propagate),
    # so shutting the runner down does not surface a spurious task error.
    async def cancel_immediately(_seconds: float) -> None:
        raise asyncio.CancelledError

    # Must not raise.
    await run_refresher(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity(None),
        clock=lambda: _NOW,
        sleep=cancel_immediately,
    )


@pytest.mark.asyncio
async def test_run_refresher_stops_writing_when_unhealthy_mid_loop(tmp_path: Path) -> None:
    # Going unhealthy mid-loop stops the beat advancing while the loop keeps
    # spinning (so it can recover) — last_beat freezes at the last healthy tick.
    states = iter([True, False, False])
    times = [_NOW + timedelta(seconds=2 * i) for i in range(3)]
    clock_calls = iter(times)
    sleep_count = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            raise asyncio.CancelledError

    await run_refresher(
        tmp_path,
        "bridge",
        is_healthy=lambda: next(states),
        identity=_identity(None),
        clock=lambda: next(clock_calls),
        sleep=fake_sleep,
    )

    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    # Only the first (healthy) tick wrote; last_beat frozen at times[0].
    assert beat.last_beat == times[0]


@pytest.mark.asyncio
async def test_run_refresher_uses_real_clock_by_default(tmp_path: Path) -> None:
    # With no injected clock the driver stamps an aware-UTC datetime.now(UTC); one
    # tick then cancel proves the default clock path writes a tz-aware beat.
    async def cancel_after_first(_seconds: float) -> None:
        raise asyncio.CancelledError

    await run_refresher(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity(None),
        sleep=cancel_after_first,
    )
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.last_beat.tzinfo is not None  # default clock is aware UTC


@pytest.mark.asyncio
async def test_run_refresher_survives_a_write_error_mid_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient write failure on one tick must NOT kill the refresher task — else
    # the beat stops forever and the bridge looks permanently unhealthy even after
    # the filesystem recovers. The loop swallows the per-tick error and keeps going.
    import calfcord.health.refresher as refresher_mod

    times = [_NOW + timedelta(seconds=2 * i) for i in range(2)]
    clock_calls = iter(times)
    real_write = refresher_mod.write_beat
    write_calls = 0

    def flaky_write(*args: object, **kwargs: object) -> object:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            raise OSError("disk full")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(refresher_mod, "write_beat", flaky_write)

    sleep_count = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise asyncio.CancelledError

    # Must NOT raise OSError — the loop tolerates the per-tick failure and continues.
    await run_refresher(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity(None),
        clock=lambda: next(clock_calls),
        sleep=fake_sleep,
    )

    # The first tick's write raised (skipped); the second tick wrote.
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.last_beat == times[1]


@pytest.mark.asyncio
async def test_run_refresher_passes_interval_to_sleep(tmp_path: Path) -> None:
    # Pin the tick→sleep(interval) contract: a wrong/literal interval would slip by.
    seen: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        seen.append(seconds)
        raise asyncio.CancelledError

    await run_refresher(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity(None),
        interval_seconds=7.5,
        clock=lambda: _NOW,
        sleep=fake_sleep,
    )
    assert seen == [7.5]


@pytest.mark.asyncio
async def test_run_refresher_threads_status_through_to_the_beat(tmp_path: Path) -> None:
    async def cancel_after_first(_seconds: float) -> None:
        raise asyncio.CancelledError

    await run_refresher(
        tmp_path,
        "bridge",
        is_healthy=_always(True),
        identity=_identity(None),
        status="connected",
        clock=lambda: _NOW,
        sleep=cancel_after_first,
    )
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.status == "connected"


# The refresher runs inside every long-lived runner (incl. the agent/tools hosts),
# so importing it must never pull in the bridge-only MCP loader
# (``calfcord.mcp.config`` expands ``$VAR`` secrets from mcp.json — design §12.3).
# A fresh interpreter gives a clean ``sys.modules`` to assert against; mirrors
# ``tests/health/test_heartbeat.py``.
_ISOLATION_SCRIPT = """
import sys

import calfcord.health.refresher  # noqa: F401

leaked = "calfcord.mcp.config" in sys.modules
assert not leaked, (
    "health.refresher transitively imported the bridge-only MCP loader "
    "(all calfcord.mcp.*: "
    + repr([m for m in sys.modules if m.startswith("calfcord.mcp")])
    + ")"
)
print("ISOLATION_OK")
"""


def test_refresher_does_not_import_mcp_config() -> None:
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
        "isolation subprocess exited 0 but did not run to completion "
        f"(no ISOLATION_OK sentinel)\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
