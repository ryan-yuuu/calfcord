"""Unit tests for the gateway's transcript-store lifecycle helpers.

Two module-level helpers are covered:

* :func:`_open_transcript_store` — a failed store open must NOT abort the
  bridge (which would take down all Discord routing, not just transcripts).
  The async context manager opens the real :class:`TranscriptStore`, and on
  any open failure logs a loud ERROR and substitutes a
  :class:`NullTranscriptStore` (``enabled=False``) so the run continues with
  transcripts / replay / the expand toggle disabled. Covered for both arms:

  - success → a connected real store with ``enabled is True``;
  - failure → a ``NullTranscriptStore`` (``enabled is False``) and the
    context manager does NOT raise; the ERROR is logged.

* :func:`_prune_on_startup` — the best-effort startup retention sweep,
  hoisted out of ``main()``'s nested ``_run()`` closure so it is testable.
  Covered: a positive prune count is awaited with the right cutoff and
  logged; a prune that raises is swallowed (never aborts startup) and
  logged; ``retention_days <= 0`` skips the prune entirely.

The repo runs under ``asyncio_mode = "auto"`` (see ``pyproject.toml``), so
``async def test_...`` functions run without an explicit marker.
"""

from __future__ import annotations

import logging
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from calfkit_organization.bridge import gateway as gateway_mod
from calfkit_organization.bridge.gateway import (
    _open_transcript_store,
    _prune_on_startup,
)
from calfkit_organization.bridge.transcripts import (
    NullTranscriptStore,
    TranscriptStore,
)


def _settings(db_path: pathlib.Path) -> SimpleNamespace:
    """A minimal settings stand-in: ``_open_transcript_store`` reads only
    ``transcript_db_path``."""
    return SimpleNamespace(transcript_db_path=db_path)


def _retention_settings(days: int) -> SimpleNamespace:
    """A minimal settings stand-in for ``_prune_on_startup``, which reads
    only ``transcript_retention_days``."""
    return SimpleNamespace(transcript_retention_days=days)


async def test_open_success_yields_connected_real_store(tmp_path: pathlib.Path) -> None:
    db_path = tmp_path / "state" / "transcripts.sqlite3"
    settings = _settings(db_path)

    async with _open_transcript_store(settings) as store:  # type: ignore[arg-type]
        # The real store opened: it is enabled and a live connection backs
        # it (a round-trip read succeeds rather than raising the
        # "not connected" RuntimeError).
        assert isinstance(store, TranscriptStore)
        assert store.enabled is True
        assert db_path.exists()
        # A read against the live connection works.
        assert await store.get_by_final_message_id("nope") is None

    # The context closed the connection on exit; methods now raise.
    with pytest.raises(RuntimeError):
        await store.get_by_final_message_id("nope")


async def test_open_failure_yields_null_store_and_logs_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path = tmp_path / "state" / "transcripts.sqlite3"
    settings = _settings(db_path)

    async def _boom(self: TranscriptStore) -> None:
        raise OSError("disk on fire")

    # Force the real open to fail; the context manager must degrade rather
    # than propagate.
    monkeypatch.setattr(TranscriptStore, "connect", _boom)

    with caplog.at_level(logging.ERROR, logger="calfkit_organization.bridge.gateway"):
        async with _open_transcript_store(settings) as store:  # type: ignore[arg-type]
            # Degraded to the no-op store: disabled, and the no-op surface
            # is usable without raising.
            assert isinstance(store, NullTranscriptStore)
            assert store.enabled is False
            assert await store.get_by_final_message_id("nope") is None

    # A loud, operator-actionable ERROR naming the path + the disabled
    # features was logged.
    matching = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "transcript store failed to open" in r.message and "DISABLED" in r.message
    ]
    assert matching, "expected a loud ERROR naming the failed open + disabled features"


async def test_open_failure_does_not_raise(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degrade path must swallow the open failure entirely — entering
    and exiting the context manager raises nothing."""
    settings = _settings(tmp_path / "state" / "transcripts.sqlite3")

    async def _boom(self: TranscriptStore) -> None:
        raise RuntimeError("cannot open")

    monkeypatch.setattr(TranscriptStore, "connect", _boom)

    # No exception escapes; the Null store's close() on exit is also a
    # harmless no-op.
    async with _open_transcript_store(settings) as store:  # type: ignore[arg-type]
        assert isinstance(store, NullTranscriptStore)


# --- _prune_on_startup --------------------------------------------------------


async def test_prune_on_startup_prunes_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A positive retention window prunes rows older than ``now - days`` and
    logs the deleted count. The cutoff passed to ``prune_older_than`` is
    ``int(now) - days * 86400`` for a frozen ``now``."""
    # Freeze time so the cutoff is exact and assertable.
    frozen_now = 1_700_000_000
    monkeypatch.setattr(gateway_mod.time, "time", lambda: frozen_now + 0.5)

    store = SimpleNamespace(prune_older_than=AsyncMock(return_value=3))
    settings = _retention_settings(days=30)

    with caplog.at_level(logging.INFO, logger="calfkit_organization.bridge.gateway"):
        await _prune_on_startup(store, settings)  # type: ignore[arg-type]

    expected_cutoff = frozen_now - 30 * 86400
    store.prune_older_than.assert_awaited_once_with(expected_cutoff)
    # The non-zero deletion is logged with the count + window.
    pruned_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "pruned" in r.message and "transcript row" in r.message
    ]
    assert pruned_logs, "expected an INFO log naming the pruned-row count"


async def test_prune_on_startup_zero_pruned_logs_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A zero deletion is still a successful prune — it just emits no INFO
    line (the ``if pruned:`` guard)."""
    monkeypatch.setattr(gateway_mod.time, "time", lambda: 1_700_000_000)
    store = SimpleNamespace(prune_older_than=AsyncMock(return_value=0))

    with caplog.at_level(logging.INFO, logger="calfkit_organization.bridge.gateway"):
        await _prune_on_startup(store, _retention_settings(days=7))  # type: ignore[arg-type]

    store.prune_older_than.assert_awaited_once()
    assert not [r for r in caplog.records if "pruned" in r.message]


async def test_prune_on_startup_swallows_prune_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prune that raises must NOT escape — retention is housekeeping, and a
    failure (read-only volume, disk full, …) must never abort bridge startup.
    The exception is logged and swallowed."""
    store = SimpleNamespace(prune_older_than=AsyncMock(side_effect=OSError("disk full")))

    with caplog.at_level(logging.ERROR, logger="calfkit_organization.bridge.gateway"):
        # Must not raise.
        await _prune_on_startup(store, _retention_settings(days=30))  # type: ignore[arg-type]

    failure_logs = [
        r for r in caplog.records if r.levelno == logging.ERROR and "retention prune failed" in r.message
    ]
    assert failure_logs, "expected an ERROR log when the startup prune raises"


async def test_prune_on_startup_disabled_skips_prune() -> None:
    """``transcript_retention_days <= 0`` means keep forever: the prune is
    skipped entirely (``prune_older_than`` is never awaited)."""
    store = SimpleNamespace(prune_older_than=AsyncMock(return_value=99))

    await _prune_on_startup(store, _retention_settings(days=0))  # type: ignore[arg-type]
    store.prune_older_than.assert_not_awaited()

    # Negative is treated the same as zero (keep forever).
    await _prune_on_startup(store, _retention_settings(days=-5))  # type: ignore[arg-type]
    store.prune_older_than.assert_not_awaited()
