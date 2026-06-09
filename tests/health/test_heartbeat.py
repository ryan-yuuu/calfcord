"""Unit tests for the filesystem heartbeat (design §4.2 / §12.1).

The heartbeat is pure filesystem — write an atomic JSON beat, read it back, and
judge freshness against an injected clock — so every test here is deterministic
and offline (no broker, no real wall-clock). Time is injected everywhere via
``now`` so freshness boundaries are exact, and the corrupt/missing-file paths
assert that a stale or partial beat reads as "no fresh beat" rather than crashing
the readiness probe.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from calfcord.health import heartbeat as heartbeat_mod
from calfcord.health.heartbeat import Heartbeat, is_fresh, read_beat, write_beat

_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)


def _health_dir(home: Path) -> Path:
    return home / "state" / "health"


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    write_beat(tmp_path, "bridge", status="healthy", pid=4242, now=_NOW)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.component == "bridge"
    assert beat.status == "healthy"
    assert beat.pid == 4242
    assert beat.last_beat == _NOW
    assert beat.started_at == _NOW


def test_write_beat_returns_the_persisted_record(tmp_path: Path) -> None:
    returned = write_beat(tmp_path, "tools", status="healthy", pid=7, now=_NOW)
    assert returned == read_beat(tmp_path, "tools")


def test_write_beat_creates_parent_dirs(tmp_path: Path) -> None:
    # state/health/ does not exist yet — the writer must create it on demand.
    assert not _health_dir(tmp_path).exists()
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    assert (_health_dir(tmp_path) / "bridge.json").is_file()


def test_pid_defaults_to_current_process(tmp_path: Path) -> None:
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.pid == os.getpid()


def test_write_beat_rejects_naive_now(tmp_path: Path) -> None:
    # last_beat must be timezone-aware UTC so is_fresh's `now - last_beat` never
    # hits a naive/aware TypeError at the probe; reject a naive clock at the writer.
    naive = datetime(2026, 6, 5, 12, 0, 0)  # deliberately naive (no tzinfo)
    with pytest.raises(ValueError):
        write_beat(tmp_path, "bridge", status="healthy", now=naive)


def test_read_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_beat(tmp_path, "never-written") is None


def test_read_corrupt_json_returns_none_without_raising(tmp_path: Path) -> None:
    # A partial/garbled beat (e.g. a torn write outside our atomic path, or a
    # truncated file) must read as "no fresh beat", never crash the probe.
    path = _health_dir(tmp_path)
    path.mkdir(parents=True)
    (path / "bridge.json").write_text("{not valid json", encoding="utf-8")
    assert read_beat(tmp_path, "bridge") is None


def test_read_schema_violation_returns_none(tmp_path: Path) -> None:
    # Valid JSON, wrong shape (missing required fields) — still "no fresh beat".
    path = _health_dir(tmp_path)
    path.mkdir(parents=True)
    (path / "bridge.json").write_text('{"component": "bridge"}', encoding="utf-8")
    assert read_beat(tmp_path, "bridge") is None


def test_read_invalid_utf8_returns_none_without_raising(tmp_path: Path) -> None:
    # A byte-corrupt beat (invalid UTF-8 from a torn/truncated write) makes
    # read_text raise UnicodeDecodeError (a ValueError) — it must still degrade to
    # None, not crash the readiness probe.
    path = _health_dir(tmp_path)
    path.mkdir(parents=True)
    (path / "bridge.json").write_bytes(b"\xff\xfe torn")
    assert read_beat(tmp_path, "bridge") is None


def test_write_beat_survives_a_corrupt_prior_beat(tmp_path: Path) -> None:
    # The refresher reads the prior beat for started_at; a byte-corrupt prior file
    # must not crash the write (it would self-perpetuate, since the write fails
    # before os.replace and never repairs the file). started_at resets to now.
    path = _health_dir(tmp_path)
    path.mkdir(parents=True)
    (path / "bridge.json").write_bytes(b"\xff\xfe torn")
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.started_at == _NOW


def test_started_at_is_preserved_across_refreshes(tmp_path: Path) -> None:
    # The refresher calls write_beat repeatedly; started_at must stay pinned to
    # the first beat (so status can detect flapping) while last_beat advances.
    later = _NOW + timedelta(seconds=3)
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    write_beat(tmp_path, "bridge", status="healthy", now=later)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.started_at == _NOW
    assert beat.last_beat == later


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW + timedelta(seconds=2))
    entries = list(_health_dir(tmp_path).iterdir())
    # Only the final JSON beat — no leftover ".bridge.*.tmp" from temp+replace.
    assert [p.name for p in entries] == ["bridge.json"]


def test_failed_replace_cleans_up_temp_file_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the atomic swap fails mid-write, the half-written temp beat must be
    # removed (so a probe never finds it) and the real error must propagate —
    # not be swallowed into a silently-missing heartbeat.
    boom = RuntimeError("disk full")

    def _explode(_src: str, _dst: object) -> None:
        raise boom

    monkeypatch.setattr(heartbeat_mod.os, "replace", _explode)

    with pytest.raises(RuntimeError, match="disk full"):
        write_beat(tmp_path, "bridge", status="healthy", now=_NOW)

    # No temp leftovers and no committed beat — the write failed cleanly.
    assert list(_health_dir(tmp_path).iterdir()) == []


def test_identity_round_trips_when_set(tmp_path: Path) -> None:
    write_beat(tmp_path, "bridge", status="healthy", identity="Calfbot#1234", now=_NOW)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.identity == "Calfbot#1234"


def test_identity_is_optional_and_defaults_to_none(tmp_path: Path) -> None:
    write_beat(tmp_path, "bridge", status="healthy", now=_NOW)
    beat = read_beat(tmp_path, "bridge")
    assert beat is not None
    assert beat.identity is None


def test_is_fresh_under_ttl(tmp_path: Path) -> None:
    beat = Heartbeat(
        component="bridge", pid=1, started_at=_NOW, last_beat=_NOW, status="healthy"
    )
    assert is_fresh(beat, now=_NOW + timedelta(seconds=5), ttl_seconds=10) is True


def test_is_fresh_at_the_ttl_boundary_is_inclusive(tmp_path: Path) -> None:
    # now - last_beat == ttl exactly is still fresh (boundary is inclusive).
    beat = Heartbeat(
        component="bridge", pid=1, started_at=_NOW, last_beat=_NOW, status="healthy"
    )
    assert is_fresh(beat, now=_NOW + timedelta(seconds=10), ttl_seconds=10) is True


def test_is_fresh_over_ttl_is_stale(tmp_path: Path) -> None:
    beat = Heartbeat(
        component="bridge", pid=1, started_at=_NOW, last_beat=_NOW, status="healthy"
    )
    assert is_fresh(beat, now=_NOW + timedelta(seconds=11), ttl_seconds=10) is False


def test_is_fresh_default_ttl_is_ten_seconds(tmp_path: Path) -> None:
    # §12.1 pins the default TTL relative to the ~2s refresh interval.
    beat = Heartbeat(
        component="bridge", pid=1, started_at=_NOW, last_beat=_NOW, status="healthy"
    )
    assert is_fresh(beat, now=_NOW + timedelta(seconds=10)) is True
    assert is_fresh(beat, now=_NOW + timedelta(seconds=11)) is False
