"""Minimal, position-preserving dotenv reader/writer for ``calfcord init``.

The install's ``config/.env`` is the *seeded* ``.env.example``: it is
heavily commented and its ordering is meaningful documentation for the
operator. A general-purpose dotenv library would happily rewrite that file
(reordering keys, dropping comments, normalizing quoting), so we hand-roll
the trivial ``KEY=VALUE`` format here and guarantee an in-place upsert that
leaves every comment, blank line, and unrelated key exactly where it was.

Writes are atomic (temp file + :func:`os.replace`) and ``chmod 0600`` because
the file holds API keys and the Discord bot token — a partial write or a
world-readable secrets file is a real hazard, so both are handled here in the
one place that touches the file.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

_SECRET_FILE_MODE = 0o600


def _strip_quotes(value: str) -> str:
    """Drop one layer of matching surrounding quotes from a dotenv value.

    Operators (and some tooling) wrap values in quotes; the runtime loader
    strips them, so the reader must too or a re-run would show ``"abc"`` as
    the "current" value and never recognise it as already-set.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_env(path: Path) -> dict[str, str]:
    """Parse ``path`` into a ``{KEY: VALUE}`` dict; missing file yields ``{}``.

    Blank lines and comment lines (first non-space char ``#``) are ignored, as
    are lines without ``=``. Keys and values are stripped of surrounding
    whitespace and the value of one layer of matching quotes. A later
    assignment of the same key wins (mirrors dotenv last-wins semantics), which
    keeps re-runs of ``init`` consistent with what the process would actually
    load.
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        result[key] = _strip_quotes(value.strip())
    return result


def upsert(path: Path, updates: Mapping[str, str]) -> None:
    """Idempotently set each key in ``updates``, preserving the rest of the file.

    For every key already present, its ``KEY=...`` line is replaced **in
    place** so all comments, ordering, and unrelated lines survive untouched;
    keys not yet present are appended (one per line) after the existing
    content. The file (and its parent directory) is created if absent. The
    write is atomic (temp file in the same directory + :func:`os.replace`) and
    the result is ``chmod 0600`` because it holds secrets. An empty ``updates``
    is a no-op, so callers can upsert unconditionally without a guard.

    Running this twice with the same ``updates`` produces byte-identical
    output: the first run sets the keys, the second finds them already on their
    lines and rewrites the same bytes.
    """
    if not updates:
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines()

    remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        replaced = False
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.partition("=")[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                replaced = True
        if not replaced:
            new_lines.append(line)

    # Append keys that were never present, in the caller's iteration order.
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    # Re-join. Always terminate with a newline: secrets files are line-oriented
    # and a missing final newline trips naive `grep '^KEY='` style readers (the
    # shim's set-broker uses exactly that). With no content lines, preserve the
    # original's blank/empty shape rather than inventing one. (Kept as an
    # if/else rather than a nested ternary for readability — see SIM108.)
    if new_lines:  # noqa: SIM108
        body = "\n".join(new_lines) + "\n"
    else:
        body = "\n" if original.endswith("\n") else ""

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(tmp_name, _SECRET_FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        # Don't leave a half-written temp file behind on any failure (including
        # KeyboardInterrupt during a long write). A missing temp file (already
        # replaced) must not mask the original exception being re-raised.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
