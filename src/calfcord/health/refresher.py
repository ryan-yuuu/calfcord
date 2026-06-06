"""Health-gated heartbeat refresher for the long-lived runners (design §12.1).

A runner must refresh its heartbeat every few seconds, but **only while it is
healthy**. The point of §12.1 is that a beat stamped on a bare timer lies: it
stays green while a revoked token or a dropped Discord gateway leaves the bot
silent. The fix is to gate every refresh on a liveness predicate and *skip the
write* when it is false — so ``last_beat`` stops advancing, the beat goes stale
within the TTL, and the readiness probe correctly reads "not ready". The
refresher never *demotes* the beat (writes a "down" status); it simply stops
feeding it, which is what freshness is built to detect.

Both the predicate (``is_healthy``) and the display ``identity`` are injected as
callables so the bridge can pass a live connection-state check and a bot-identity
getter that only resolves once the gateway is ready. The clock and ``sleep`` are
injected too, so the loop is fully unit-testable with no wall-clock waits.

Kept import-light on purpose (heartbeat + stdlib + asyncio only): this runs inside
every runner, including the agent/tools hosts, and must never transitively pull in
the bridge-only secrets loader (``calfcord.mcp.config`` — see the package
docstring).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from calfcord.health.heartbeat import write_beat

logger = logging.getLogger(__name__)

# Pinned relative to the TTL in heartbeat.py (10s): ~2s gives a few beats of slack
# before the TTL trips, so a single slow tick does not flap the readiness probe.
_DEFAULT_INTERVAL_SECONDS = 2.0

IsHealthy = Callable[[], bool]
Identity = Callable[[], str | None]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def refresh_once(
    home: str | os.PathLike[str],
    component: str,
    *,
    is_healthy: IsHealthy,
    identity: Identity,
    status: str = "healthy",
    now: datetime,
) -> bool:
    """Write one heartbeat IFF ``is_healthy()`` is true; return whether it wrote.

    When healthy, persist a beat via :func:`calfcord.health.heartbeat.write_beat`
    (which advances ``last_beat`` to ``now`` while preserving ``started_at``).
    When unhealthy, **skip the write entirely** so ``last_beat`` stays pinned to
    the last healthy tick and the beat ages past the TTL — the §12.1 contract that
    turns a silent failure into a "not ready" verdict at the probe.

    ``is_healthy`` and ``identity`` are evaluated *here, per tick*, so the beat
    always reflects the connection state and bot identity at the moment it is
    written. ``now`` must be timezone-aware UTC (enforced by ``write_beat``).
    """
    if not is_healthy():
        return False

    write_beat(home, component, status=status, identity=identity(), now=now)
    return True


async def run_refresher(
    home: str | os.PathLike[str],
    component: str,
    *,
    is_healthy: IsHealthy,
    identity: Identity,
    status: str = "healthy",
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    clock: Clock = lambda: datetime.now(UTC),
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Refresh ``component``'s heartbeat on a timer until cancelled.

    Each iteration ticks (:func:`refresh_once`, gated on ``is_healthy``) then
    awaits ``sleep(interval_seconds)``, repeating forever. The runner cancels the
    task on shutdown: :class:`asyncio.CancelledError` returns cleanly here (rather
    than propagating a spurious task error) because a cancelled refresher is an
    expected, orderly stop, not a fault.

    ``clock`` defaults to an aware-UTC ``datetime.now(UTC)`` (matching the probe's
    clock so freshness never trips a naive/aware mismatch); ``sleep`` defaults to
    :func:`asyncio.sleep`. Both are injected so tests drive the loop deterministically
    with zero wall-clock waits.
    """
    try:
        while True:
            try:
                refresh_once(
                    home,
                    component,
                    is_healthy=is_healthy,
                    identity=identity,
                    status=status,
                    now=clock(),
                )
            except Exception:
                # A transient write failure (disk full / read-only volume) must not
                # kill the refresher task — that would freeze the beat forever and
                # leave the runner permanently "not ready" even after the fault
                # clears. Log and keep ticking; the missed beat ages toward the TTL,
                # which is the correct "not ready" signal until a write succeeds.
                # (CancelledError is a BaseException, so it is NOT caught here.)
                logger.warning(
                    "heartbeat refresh failed for %s; retrying next tick",
                    component,
                    exc_info=True,
                )
            await sleep(interval_seconds)
    except asyncio.CancelledError:
        # Orderly shutdown: a cancelled refresher is the normal stop path, so
        # swallow the cancellation and return instead of surfacing a task error.
        return
