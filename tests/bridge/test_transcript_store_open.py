"""Unit tests for the gateway's ``_open_transcript_store`` degrade path.

A failed store open must NOT abort the bridge (which would take down all
Discord routing, not just transcripts). The gateway's
:func:`_open_transcript_store` async context manager opens the real
:class:`TranscriptStore`, and on any open failure logs a loud ERROR and
substitutes a :class:`NullTranscriptStore` (``enabled=False``) so the run
continues with transcripts / replay / the expand toggle disabled.

These tests cover both arms:

* success → a connected real store with ``enabled is True``;
* failure → a ``NullTranscriptStore`` (``enabled is False``) and the
  context manager does NOT raise; the ERROR is logged.

The repo runs under ``asyncio_mode = "auto"`` (see ``pyproject.toml``), so
``async def test_...`` functions run without an explicit marker.
"""

from __future__ import annotations

import logging
import pathlib
from types import SimpleNamespace

import pytest

from calfkit_organization.bridge.gateway import _open_transcript_store
from calfkit_organization.bridge.transcripts import (
    NullTranscriptStore,
    TranscriptStore,
)


def _settings(db_path: pathlib.Path) -> SimpleNamespace:
    """A minimal settings stand-in: ``_open_transcript_store`` reads only
    ``transcript_db_path``."""
    return SimpleNamespace(transcript_db_path=db_path)


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
