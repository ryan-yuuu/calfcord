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

Scope: values are single-line ``KEY=VALUE`` pairs (tokens, keys, ids, urls). A
value containing a newline is rejected (it would split into a second, malformed
line and silently corrupt the file), and :func:`read_env` matches the runtime
dotenv loaders (python-dotenv / ``uv run --env-file``) on the parsing details
that matter — surrounding whitespace, an ``export`` prefix, quotes, and inline
comments — so the wizard's "current value" never disagrees with what the
processes actually load.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

_SECRET_FILE_MODE = 0o600


def _parse_value(value: str) -> str:
    """Decode one already-whitespace-stripped dotenv value the way the loaders do.

    Mirrors python-dotenv / ``uv run --env-file`` so ``init``'s "current value"
    display agrees with what the processes load: a value wrapped in one matching
    pair of quotes is taken verbatim (a ``#`` inside quotes is literal); an
    unquoted value ends at the first `` #`` (whitespace + hash), which begins an
    inline comment. Operators (and tooling) quote values and append comments, so
    the reader must honour both or a re-run would mis-read an already-set key.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    comment = value.find(" #")
    if comment != -1:
        value = value[:comment].rstrip()
    return value


def read_env(path: Path) -> dict[str, str]:
    """Parse ``path`` into a ``{KEY: VALUE}`` dict; missing file yields ``{}``.

    Blank lines and comment lines (first non-space char ``#``) are ignored, as
    are lines without ``=``. A leading ``export `` is part of the dotenv/shell
    syntax (the key is what follows it), values are stripped of surrounding
    whitespace and decoded by :func:`_parse_value` (quotes / inline comments), and
    a later assignment of the same key wins (dotenv last-wins) — all so a re-run of
    ``init`` reads a key exactly as the process would.
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        result[key] = _parse_value(value.strip())
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

    Values must be single-line: a value containing a newline raises
    :class:`ValueError` rather than splitting into a second, malformed line that
    would silently corrupt the file (and, on a re-run, append it again). For
    single-line values, running this twice with the same ``updates`` produces
    byte-identical output: the first run sets the keys, the second finds them
    already on their lines and rewrites the same bytes.

    Raises:
        ValueError: a value contains a newline (``\\n`` / ``\\r``).
    """
    if not updates:
        return

    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(
                f"value for {key!r} contains a newline; .env values must be single-line "
                "(check for a stray newline in a pasted secret)"
            )

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
