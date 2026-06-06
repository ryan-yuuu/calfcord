"""Filesystem heartbeat: write/read/freshness for the runner liveness probe.

A long-lived runner refreshes ``<home>/state/health/<component>.json`` every few
seconds; the ``calfcord _healthcheck`` exec probe (run by Process Compose on the
agent/tools hosts) reads it to gate readiness. The contract (design §4.2 / §12.1):

* the beat carries ``{component, pid, started_at, last_beat, status, identity}``;
  ``identity`` is a *display* string (a bot name / numeric id) — never a token
  (§12.3), and never required;
* :func:`write_beat` is **atomic** (the shared :func:`calfcord._atomic.atomic_write_text`
  — same-dir temp file + :func:`os.replace`) so a probe never observes a
  half-written beat, and creates ``state/health/`` on demand;
* :func:`read_beat` **never raises** on a missing or corrupt file — a stale or
  partial beat must read as "no fresh beat", not crash the readiness probe;
* the clock is **injected** (``now``) everywhere, so freshness boundaries are
  exact and tests are deterministic.

Kept pure-filesystem and dependency-light on purpose: this module must be safe to
import on a host with no shared filesystem and must not pull in the bridge-only
secrets loader (see the package docstring).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

from calfcord._atomic import atomic_write_text

# TTL pinned relative to the runner's ~2s refresh interval (§12.1): a few missed
# beats tolerate a slow GC pause / scheduler hiccup without flapping the probe,
# while still catching a wedged or killed process within seconds.
_DEFAULT_TTL_SECONDS = 10


class Heartbeat(BaseModel):
    """One liveness beat written by a runner and read by the health probe.

    ``started_at`` is stamped once (process start) and preserved across refreshes
    so ``status`` can surface flapping; ``last_beat`` advances every write.
    ``identity`` is an optional display string (never a secret).
    """

    component: str
    pid: int
    started_at: datetime
    last_beat: datetime
    status: str
    identity: str | None = None


def _beat_path(home: str | os.PathLike[str], component: str) -> Path:
    return Path(home) / "state" / "health" / f"{component}.json"


def write_beat(
    home: str | os.PathLike[str],
    component: str,
    *,
    status: str,
    identity: str | None = None,
    pid: int | None = None,
    now: datetime,
) -> Heartbeat:
    """Atomically write ``component``'s heartbeat under ``<home>/state/health/``.

    ``pid`` defaults to :func:`os.getpid`; ``now`` is injected (UTC) so tests are
    deterministic. ``started_at`` is taken from the existing beat when one is
    present so it stays stable across the periodic refresh (a runner calls this
    repeatedly); the first beat stamps ``started_at = now``. The parent directory
    is created on demand, and the write is atomic (temp file in the same directory
    + :func:`os.replace`) so a concurrent probe never reads a half-written file.

    Returns the :class:`Heartbeat` that was persisted. ``now`` MUST be
    timezone-aware UTC (matching the probe's ``datetime.now(UTC)``) so a stored
    ``last_beat`` never trips :func:`is_fresh`'s subtraction on a naive/aware
    mismatch; a naive ``now`` is rejected here, at the writer, rather than crashing
    a downstream reader.
    """
    if now.tzinfo is None:
        raise ValueError("write_beat requires a timezone-aware `now` (UTC)")

    path = _beat_path(home, component)

    existing = read_beat(home, component)
    started_at = existing.started_at if existing is not None else now

    beat = Heartbeat(
        component=component,
        pid=os.getpid() if pid is None else pid,
        started_at=started_at,
        last_beat=now,
        status=status,
        identity=identity,
    )

    # Atomic same-dir tmp + os.replace (see calfcord._atomic): a concurrent probe
    # never observes a half-written beat. No mode is passed — a beat holds no
    # secret, so it keeps the writer's default (mkstemp 0o600), matching the
    # pre-extraction behaviour.
    atomic_write_text(path, beat.model_dump_json())

    return beat


def read_beat(home: str | os.PathLike[str], component: str) -> Heartbeat | None:
    """Read ``component``'s heartbeat, or ``None`` if it is absent or unreadable.

    A readiness probe must degrade to "no fresh beat" rather than crash on a
    missing, partial, or corrupt file, so every failure mode — file not found,
    invalid JSON, or a payload that does not satisfy the schema — returns
    ``None`` instead of raising.
    """
    path = _beat_path(home, component)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # OSError = missing/permission/is-a-dir; ValueError covers UnicodeDecodeError
        # from a byte-corrupt / torn beat. Both mean "no fresh beat" — never crash
        # the readiness probe (and write_beat reads here too, so a corrupt prior
        # beat must not break the refresh).
        return None
    try:
        return Heartbeat.model_validate_json(raw)
    except ValidationError:
        return None


def is_fresh(beat: Heartbeat, *, now: datetime, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> bool:
    """Return ``True`` iff ``beat`` was written within ``ttl_seconds`` of ``now``.

    Freshness is ``now - last_beat <= ttl`` (the boundary is inclusive). ``now``
    is injected so callers control the clock; default TTL is pinned to the
    runner's refresh interval (§12.1).
    """
    return now - beat.last_beat <= timedelta(seconds=ttl_seconds)
