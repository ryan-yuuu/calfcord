"""Tests for the neutral atomic text writer (:mod:`calfcord._atomic`).

``atomic_write_text`` is the one same-dir-tmp → ``os.replace`` writer the
heartbeat probe, the setup checkpoint, and the dotenv upserter share, so it owns
the invariants those call sites depend on:

* a **round-trip** — what you write is what reads back, parent dir created on
  demand — so callers can write under a not-yet-existing ``state/`` tree;
* an optional ``mode`` that ``chmod``s the result (the secret writers pass
  ``0o600``; the heartbeat passes nothing and keeps the default tmp mode);
* a **never-partial** swap — if the rename fails mid-write the original target
  is untouched (or absent) and no ``.tmp`` orphan is left behind, with the
  original exception re-raised — so a concurrent reader never sees a torn file.

The module is deliberately off the ``cli`` package and dependency-light: the
heartbeat imports it and must stay safe on a host with no shared filesystem and
no bridge-only secrets loader.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from calfcord._atomic import atomic_write_text


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    atomic_write_text(path, "hello world")
    assert path.read_text(encoding="utf-8") == "hello world"


def test_creates_parent_dirs_on_demand(tmp_path: Path) -> None:
    # Callers write under a not-yet-existing state/ tree (the heartbeat's
    # state/health/, the checkpoint's state/), so the parent is made on demand.
    path = tmp_path / "deeply" / "nested" / "file.txt"
    assert not path.parent.exists()
    atomic_write_text(path, "x")
    assert path.read_text(encoding="utf-8") == "x"


def test_overwrites_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    atomic_write_text(path, "first")
    atomic_write_text(path, "second")
    assert path.read_text(encoding="utf-8") == "second"


def test_mode_chmods_the_result(tmp_path: Path) -> None:
    # The secret writers (.env, setup.json) pass 0o600 so the persisted file is
    # owner-only regardless of umask or the tmp file's default mode.
    path = tmp_path / "secret"
    atomic_write_text(path, "tok", mode=0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_mode_none_forces_no_chmod(tmp_path: Path) -> None:
    # The heartbeat passes no mode and never chmods. Because os.replace adopts
    # the tmp file's mode, the persisted file lands at mkstemp's default 0o600
    # (the pre-refactor heartbeat behaviour) — the contract is that NO explicit
    # chmod is issued when mode is None, which we pin by asserting os.chmod is
    # never called on this path.
    path = tmp_path / "beat.json"
    seen: list[object] = []
    real_chmod = os.chmod

    def _record(target: object, *args: object, **kwargs: object) -> None:
        seen.append(target)
        real_chmod(target, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(os, "chmod", _record)
    try:
        atomic_write_text(path, "a", mode=None)
    finally:
        monkeypatch.undo()

    assert seen == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_re_chmod_on_overwrite_keeps_mode(tmp_path: Path) -> None:
    # A fresh tmp file each call must not let the persisted mode drift: passing
    # 0o600 again keeps it 0o600 even after a second write.
    path = tmp_path / "secret"
    atomic_write_text(path, "v1", mode=0o600)
    atomic_write_text(path, "v2", mode=0o600)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_temp_file_left_behind_on_success(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    atomic_write_text(path, "x")
    # Only the final file — no leftover ".file.txt.*.tmp" from temp+replace.
    assert [p.name for p in tmp_path.iterdir()] == ["file.txt"]


def test_failed_replace_is_not_partial_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the swap fails mid-write the target must be untouched (here: never
    # created), no .tmp orphan may remain, and the original error must propagate
    # rather than be swallowed into a silently-missing file.
    path = tmp_path / "file.txt"

    def _boom(_src: str, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_text(path, "x")

    assert list(tmp_path.iterdir()) == []


def test_failed_replace_preserves_existing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed overwrite must leave the prior contents intact (the rename is the
    # commit point; the original file is never truncated in place).
    path = tmp_path / "file.txt"
    atomic_write_text(path, "original")

    def _boom(_src: str, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_text(path, "new contents")

    assert path.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["file.txt"]
