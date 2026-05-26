"""Disk cache for openai/codex prompt artifacts.

This module persists the verbatim ``models.json`` and ``prompt.md`` files
that the Codex CLI ships, alongside the HTTP ``ETag`` returned by GitHub
raw URLs. The cache enables conditional ``If-None-Match`` requests so the
common case is a cheap ``304 Not Modified`` and also gives us a safety
fallback when upstream is unreachable.

Concurrency model
-----------------
The fetcher in :mod:`prompts` is async and may run from multiple workers
on the same host (e.g. tools + agent runners share the cache dir). Each
cached name has a sibling ``<name>.lock`` file mediated by
``filelock.FileLock`` so concurrent ``save`` calls serialise without
risk of half-written meta/body pairs.

Atomicity
---------
Bodies and meta files are written to ``<name>.tmp.<pid>.<random>`` first
using ``os.open`` with ``O_CREAT | O_EXCL | O_WRONLY`` and mode ``0o600``
(no chmod-after-write race), then ``os.replace``\\ d into place. Same
filesystem guarantees the replace is atomic.

Locations
---------
Default base dir: ``~/.calfcord/codex_prompts/``. Override via the
``CALFCORD_PROMPT_CACHE_DIR`` environment variable. The directory is
created with mode ``0o700`` on POSIX so other users on the host can't
read a body that may eventually contain proprietary upstream content.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from filelock import FileLock

logger = logging.getLogger(__name__)

_ENV_OVERRIDE = "CALFCORD_PROMPT_CACHE_DIR"
_DEFAULT_BASE_DIR = Path.home() / ".calfcord" / "codex_prompts"
_LOCK_TIMEOUT_SECONDS = 30.0


class CachedEntry(NamedTuple):
    """A single cached upstream artifact.

    Attributes:
        name: Cache key (e.g. ``"models.json"``).
        body: Raw bytes as fetched from upstream.
        etag: HTTP ``ETag`` header from the last successful 200, or ``None``
            if upstream omitted it (or the meta file was corrupt and we
            recovered the body without it).
        fetched_at: UTC timestamp of the last write, or ``None`` if meta
            was missing/corrupt.
    """

    name: str
    body: bytes
    etag: str | None
    fetched_at: datetime | None


def _resolve_default_base_dir() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    return Path(override) if override else _DEFAULT_BASE_DIR


class PromptCache:
    """Atomic, file-locked disk cache for openai/codex prompt artifacts.

    Construct with an explicit ``base_dir`` for tests; production code
    should call without arguments to honour the env-var override.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir is not None else _resolve_default_base_dir()
        self._ensure_base_dir()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    # ----- public API ----------------------------------------------------

    def load(self, name: str) -> CachedEntry | None:
        """Return the cached entry for ``name`` or ``None`` if unavailable.

        Returns ``None`` when the body file is missing or unreadable.
        A missing or corrupt meta file is tolerated: the returned entry
        has ``etag=None`` and ``fetched_at=None``, which simply forces an
        unconditional GET on the next fetch.
        """
        body_path = self._body_path(name)
        meta_path = self._meta_path(name)

        try:
            body = body_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Cannot read cached body %s: %s", body_path, exc)
            return None

        etag: str | None = None
        fetched_at: datetime | None = None
        try:
            meta_raw = meta_path.read_text(encoding="utf-8")
            meta = json.loads(meta_raw)
        except FileNotFoundError:
            logger.warning("Cached body present but meta missing for %s", name)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Corrupt meta for %s (%s); treating as missing etag", meta_path, exc)
        else:
            raw_etag = meta.get("etag")
            if isinstance(raw_etag, str):
                etag = raw_etag
            raw_fetched = meta.get("fetched_at")
            if isinstance(raw_fetched, str):
                try:
                    fetched_at = datetime.fromisoformat(raw_fetched)
                except ValueError:
                    logger.warning("Corrupt fetched_at in %s; ignoring", meta_path)

        return CachedEntry(name=name, body=body, etag=etag, fetched_at=fetched_at)

    def save(self, name: str, body: bytes, etag: str | None) -> None:
        """Atomically write the body + meta for ``name``.

        Serialised across processes via ``<name>.lock``. Safe to call
        concurrently from multiple workers on the same host.
        """
        # Re-ensure the directory in case it was cleared between init and now.
        self._ensure_base_dir()
        body_path = self._body_path(name)
        meta_path = self._meta_path(name)
        lock_path = self._lock_path(name)

        meta_payload = json.dumps(
            {
                "etag": etag,
                "fetched_at": datetime.now(tz=_utc()).isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")

        with FileLock(str(lock_path), timeout=_LOCK_TIMEOUT_SECONDS):
            self._atomic_write_bytes(body_path, body)
            self._atomic_write_bytes(meta_path, meta_payload)

    def clear(self) -> None:
        """Remove every cached body and meta file in the cache dir.

        ``.lock`` files are preserved so a concurrent writer in another
        process (holding ``<name>.lock`` via filelock) doesn't get its
        coordination file pulled out from under it. On POSIX unlinking a
        held lock file silently creates a new inode at the same path on
        next save, racing the in-flight writer; on Windows the unlink
        would raise. Leaving them in place is harmless — they're empty
        coordination artifacts that get reused.

        Safe to call on a missing directory.
        """
        if not self._base_dir.exists():
            return
        for entry in self._base_dir.iterdir():
            if not entry.is_file() or entry.suffix == ".lock":
                continue
            try:
                entry.unlink()
            except OSError as exc:
                logger.warning("Could not remove cache file %s: %s", entry, exc)

    def files(self) -> list[CachedEntry]:
        """Return every cached entry currently on disk.

        Files without a paired meta file are still listed (with ``etag=None``).
        Used by the ``calfkit-auth prompt-status`` CLI command.
        """
        if not self._base_dir.exists():
            return []
        results: list[CachedEntry] = []
        for entry in sorted(self._base_dir.iterdir()):
            if not entry.is_file():
                continue
            # Skip meta + lock siblings; iterate only by body names.
            if entry.suffix in (".meta", ".lock"):
                continue
            if ".tmp." in entry.name:
                # Leftover from an interrupted write — ignore.
                continue
            loaded = self.load(entry.name)
            if loaded is not None:
                results.append(loaded)
        return results

    # ----- internals -----------------------------------------------------

    def _body_path(self, name: str) -> Path:
        return self._base_dir / name

    def _meta_path(self, name: str) -> Path:
        return self._base_dir / f"{name}.meta"

    def _lock_path(self, name: str) -> Path:
        return self._base_dir / f"{name}.lock"

    def _ensure_base_dir(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                self._base_dir.chmod(0o700)
            except OSError as exc:
                logger.warning("Could not chmod cache dir %s to 0o700: %s", self._base_dir, exc)

    def _atomic_write_bytes(self, target: Path, payload: bytes) -> None:
        """Write ``payload`` to ``target`` atomically via tmp + ``os.replace``.

        The tmp file is created with ``O_CREAT | O_EXCL | O_WRONLY`` at
        mode ``0o600`` so there's no chmod-after-write window where the
        file is world-readable.
        """
        tmp_name = f"{target.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
        tmp_path = target.parent / tmp_name
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):  # pragma: no cover - Windows only
            flags |= os.O_BINARY
        fd = os.open(tmp_path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            # Best-effort cleanup on failure; don't mask the original error.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        os.replace(tmp_path, target)


def _utc():
    """Indirection so the timezone import is centralised in one place."""
    from datetime import timezone

    return timezone.utc
