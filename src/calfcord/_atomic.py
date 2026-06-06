"""Neutral, dependency-light atomic text write shared across the package.

A handful of small state files — the runner heartbeat
(:mod:`calfcord.health.heartbeat`), the ``init`` setup checkpoint
(:mod:`calfcord.cli.setup_state`), and the dotenv upserter
(:mod:`calfcord.cli._envfile`) — all need the *same* guarantee: a concurrent
reader (a readiness probe, a re-run of the wizard, a process loading ``.env``)
must never observe a half-written file. They each reimplemented the same
same-directory-tmp → :func:`os.replace` dance; :func:`atomic_write_text` is that
one implementation.

This module lives at the package root, **off the ``cli`` package**, on purpose:
``health.heartbeat`` must stay safe to import on a host with no shared
filesystem and must not pull in anything under ``cli`` (its package docstring
pins this), so the shared writer cannot live under ``cli/``. It imports only the
stdlib, so it adds no dependency to those import-isolated paths.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | os.PathLike[str], body: str, *, mode: int | None = None) -> None:
    """Atomically write ``body`` to ``path`` via a same-dir tmp file + rename.

    The bytes are written to a temp file in ``path``'s parent directory, then
    swapped into place with :func:`os.replace` — an atomic rename on POSIX — so a
    concurrent reader sees either the old file or the complete new one, never a
    torn write. The parent directory is created on demand (callers write under a
    not-yet-existing ``state/`` tree). When ``mode`` is given the temp file is
    ``chmod``-ed to it *before* the swap, so the persisted file lands with that
    mode regardless of umask or :func:`tempfile.mkstemp`'s default (the secret
    writers pass ``0o600``); ``mode=None`` forces no chmod, leaving the tmp
    file's default and never mutating an existing target's mode.

    On *any* failure — including a :func:`os.replace` error or a
    ``KeyboardInterrupt`` mid-write — the temp file is unlinked and the original
    exception re-raised, so a crashed write leaves neither a partial target nor a
    ``.tmp`` orphan behind. ``BaseException`` (not ``Exception``) is caught so a
    Ctrl-C during the write still cleans up; the cleanup's own ``OSError`` (a
    temp file already consumed by a successful ``os.replace``) is suppressed so it
    cannot mask the real exception being propagated.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
