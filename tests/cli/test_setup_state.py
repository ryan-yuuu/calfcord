"""Tests for the resumable setup checkpoint (``cli/setup_state.py``, §12.7).

The checkpoint records *which* init steps are done so a crash/close/forced
Discord detour resumes ("Welcome back — provider and agent done; let's finish
Discord") instead of restarting. These tests pin the §12.7 contract:

* **Atomic write** (temp + ``os.replace``) so a probe/re-run never reads a
  half-written file, and ``chmod 0600`` like the sibling state writers.
* A ``schema_version`` field, with a **strict** equality gate: any mismatch
  (older *or* newer) reads back as "fresh" (empty), never a partial migration.
* **Advisory-not-authoritative**: ``load`` *never raises* — missing, corrupt,
  unreadable, or schema-mismatched files all read back as ``None`` so a stale
  checkpoint can never wrongly *skip* a step (the wizard re-verifies the real
  artifact regardless; the checkpoint only chooses where to resume).
* An **injected clock** (``now``) so the recorded ``updated_at`` is deterministic
  in tests, and an **injected home** so the path resolves under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from calfcord.cli import setup_state
from calfcord.cli.setup_state import SetupCheckpoint

_FIXED_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return _FIXED_NOW


# --------------------------------------------------------------------------- #
# Path resolution (injected home)
# --------------------------------------------------------------------------- #


def test_path_for_native_install_is_under_home_state(tmp_path: Path) -> None:
    """A native install stores the checkpoint at ``<home>/state/setup.json``."""
    assert setup_state.checkpoint_path(tmp_path) == tmp_path / "state" / "setup.json"


def test_path_for_dev_run_falls_back_to_local_state(tmp_path: Path) -> None:
    """A dev run (no home) stores it under the project-local ``state/`` dir."""
    assert setup_state.checkpoint_path(None) == Path("state") / "setup.json"


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #


def test_save_then_load_round_trips_all_fields(tmp_path: Path) -> None:
    """Every recorded field survives a save → load cycle byte-for-byte."""
    path = tmp_path / "state" / "setup.json"
    cp = SetupCheckpoint(
        provider_done=True,
        agent_name="assistant",
        discord_done=True,
        broker_done=True,
        guild_id="123",
        channel_id="456",
    )

    setup_state.save(path, cp, now=_now)
    loaded = setup_state.load(path)

    assert loaded == cp


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    """The ``state/`` parent is created on demand (mirrors the .env upsert)."""
    path = tmp_path / "does-not-exist-yet" / "setup.json"
    setup_state.save(path, SetupCheckpoint(provider_done=True), now=_now)
    assert path.exists()


def test_save_stamps_injected_clock_as_updated_at(tmp_path: Path) -> None:
    """``updated_at`` is taken from the injected clock, not the wall clock."""
    path = tmp_path / "setup.json"
    setup_state.save(path, SetupCheckpoint(provider_done=True), now=_now)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["updated_at"] == _FIXED_NOW.isoformat()


def test_save_writes_current_schema_version(tmp_path: Path) -> None:
    """The persisted file carries the module's current ``schema_version``."""
    path = tmp_path / "setup.json"
    setup_state.save(path, SetupCheckpoint(), now=_now)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == setup_state.SETUP_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Advisory contract: load never raises, returns None on any unusable file
# --------------------------------------------------------------------------- #


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    """A first run (no checkpoint) reads back as ``None`` — start fresh."""
    assert setup_state.load(tmp_path / "nope.json") is None


def test_load_corrupt_json_returns_none(tmp_path: Path) -> None:
    """A truncated/garbled file (e.g. crash mid-write on a non-atomic writer)
    reads back as ``None`` instead of raising — the wizard simply restarts."""
    path = tmp_path / "setup.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    assert setup_state.load(path) is None


def test_load_partial_json_object_returns_none(tmp_path: Path) -> None:
    """A JSON object missing the required ``schema_version`` reads as ``None``.

    Without the version we cannot trust the rest of the shape, so we treat it
    as unusable rather than guessing — the safe, advisory behaviour.
    """
    path = tmp_path / "setup.json"
    path.write_text(json.dumps({"provider_done": True}), encoding="utf-8")
    assert setup_state.load(path) is None


def test_load_old_schema_version_returns_none(tmp_path: Path) -> None:
    """A checkpoint from an older schema reads back as ``None`` (fresh start).

    We do not migrate: re-verification means no real work is lost, and a strict
    gate avoids ever interpreting old fields under new semantics.
    """
    path = tmp_path / "setup.json"
    path.write_text(
        json.dumps({"schema_version": setup_state.SETUP_SCHEMA_VERSION - 1, "provider_done": True}),
        encoding="utf-8",
    )
    assert setup_state.load(path) is None


def test_load_newer_schema_version_returns_none(tmp_path: Path) -> None:
    """A checkpoint from a *newer* schema also reads back as ``None``.

    An older reader cannot know how a newer writer reinterpreted fields, so the
    strict equality gate refuses both directions — never a partial read.
    """
    path = tmp_path / "setup.json"
    path.write_text(
        json.dumps({"schema_version": setup_state.SETUP_SCHEMA_VERSION + 1, "provider_done": True}),
        encoding="utf-8",
    )
    assert setup_state.load(path) is None


def test_load_current_version_with_wrong_typed_field_returns_none(tmp_path: Path) -> None:
    """A checkpoint with the CURRENT schema_version but a malformed field type
    reads back as ``None`` instead of raising.

    This exercises the post-version-gate branch: the version matches (so the
    strict gate passes) but ``model_validate`` rejects a wrong-typed field (here
    a bool field set to a dict). The advisory contract (§12.7) says ``load``
    never raises — a corrupt-but-versioned file is still unusable, so it must
    read as fresh, not surface a :class:`ValidationError` to the wizard.
    """
    path = tmp_path / "setup.json"
    path.write_text(
        json.dumps(
            {"schema_version": setup_state.SETUP_SCHEMA_VERSION, "provider_done": {"nope": 1}}
        ),
        encoding="utf-8",
    )
    assert setup_state.load(path) is None


def test_load_non_object_json_returns_none(tmp_path: Path) -> None:
    """A JSON value that is not an object (e.g. a list/number) reads as ``None``."""
    path = tmp_path / "setup.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert setup_state.load(path) is None


def test_load_directory_in_place_of_file_returns_none(tmp_path: Path) -> None:
    """An unreadable path (a directory where the file should be) reads as ``None``.

    The advisory contract is "never raise on read"; an ``IsADirectoryError`` /
    ``OSError`` is swallowed exactly like corruption.
    """
    path = tmp_path / "setup.json"
    path.mkdir()
    assert setup_state.load(path) is None


# --------------------------------------------------------------------------- #
# Atomicity & permissions
# --------------------------------------------------------------------------- #


def test_save_is_atomic_replace_no_tmp_left_behind(tmp_path: Path) -> None:
    """A successful save leaves only the final file — no ``.tmp`` siblings.

    The write goes temp-file → ``os.replace``; a leaked temp file would prove
    the rename path was skipped.
    """
    path = tmp_path / "setup.json"
    setup_state.save(path, SetupCheckpoint(provider_done=True), now=_now)

    siblings = list(tmp_path.iterdir())
    assert siblings == [path]


def test_save_overwrites_existing_checkpoint(tmp_path: Path) -> None:
    """Re-saving replaces the prior checkpoint in place (resume advances)."""
    path = tmp_path / "setup.json"
    setup_state.save(path, SetupCheckpoint(provider_done=True), now=_now)
    setup_state.save(
        path,
        SetupCheckpoint(provider_done=True, agent_name="assistant", discord_done=True),
        now=_now,
    )

    loaded = setup_state.load(path)
    assert loaded is not None
    assert loaded.agent_name == "assistant"
    assert loaded.discord_done is True


def test_save_sets_mode_0600(tmp_path: Path) -> None:
    """The checkpoint is ``chmod 0600`` like the sibling secret/state writers."""
    path = tmp_path / "setup.json"
    setup_state.save(path, SetupCheckpoint(), now=_now)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # Re-save keeps 0600 even though it went through a fresh temp file.
    setup_state.save(path, SetupCheckpoint(provider_done=True), now=_now)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_does_not_leave_tmp_after_replace_failure(tmp_path: Path, monkeypatch) -> None:
    """If ``os.replace`` fails, the error re-raises and the temp file is cleaned up.

    A leaked temp file would accumulate across crashed runs; the writer mirrors
    ``_envfile``/``agents.state`` in unlinking the temp on any failure while
    still surfacing the original exception to the caller.
    """
    path = tmp_path / "setup.json"

    def boom(_src: str, _dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        setup_state.save(path, SetupCheckpoint(provider_done=True), now=_now)

    # The original write target never appeared, and no temp file was left behind.
    leftovers = list(tmp_path.iterdir())
    assert leftovers == []


# --------------------------------------------------------------------------- #
# Defaults — a default checkpoint records no completed step
# --------------------------------------------------------------------------- #


def test_default_checkpoint_has_no_completed_steps(tmp_path: Path) -> None:
    """A freshly constructed checkpoint marks every step as not-yet-done.

    This is what lets the wizard treat "no checkpoint" and "empty checkpoint"
    identically: both mean "start from the top".
    """
    cp = SetupCheckpoint()
    assert cp.provider_done is False
    assert cp.agent_name is None
    assert cp.discord_done is False
    assert cp.broker_done is False
    assert cp.guild_id is None
    assert cp.channel_id is None
