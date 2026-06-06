"""Resumable setup checkpoint for ``calfcord init`` (§12.7).

``init`` is *one continuous, resumable* session: a crash, a Ctrl-C, or the
unavoidable browser detour to authorize the Discord bot must resume ("Welcome
back — provider and agent done; let's finish Discord") rather than restart from
the top. This module is the tiny persistence layer that records *which steps are
done* so the wizard knows where to resume.

Two properties are deliberate and load-bearing:

* **Advisory, not authoritative.** :func:`load` records *progress only*; it is
  the wizard's job to RE-VERIFY the real artifact for each step it intends to
  skip (token still valid? agent ``.md`` still parses? ``CALF_HOST_URL`` set?).
  The checkpoint therefore only chooses *where* to resume — the world is ground
  truth. To make that contract impossible to misuse, :func:`load` *never raises*:
  a missing, corrupt, unreadable, non-object, or schema-mismatched file all read
  back as ``None`` (== "no usable checkpoint, start fresh"). A stale checkpoint
  can thus never cause a step to be *wrongly skipped* — at worst the wizard
  re-walks a step whose artifact already exists, which is harmless and idempotent.

* **Strict schema gate.** The file carries :data:`SETUP_SCHEMA_VERSION`; on read,
  *any* mismatch (older or newer) yields ``None``. We do not migrate: an older
  reader cannot know how a newer writer reinterpreted fields, and re-verification
  means nothing is lost by restarting. This is intentionally stricter than the
  ``extra="ignore"`` policy used for long-lived runtime state — a setup
  checkpoint is short-lived and cheap to rebuild, so safety beats compatibility.

The write is **atomic** (the shared :func:`calfcord._atomic.atomic_write_text`
— same-dir temp file + :func:`os.replace`) so a concurrent reader never sees a
half-written file, and ``chmod 0600`` to match the sibling state writers — the
file holds no secrets (only step markers and the
non-secret guild/channel IDs the operator already picked from a menu), but the
install's ``state/`` dir is uniformly owner-only and a stray temp file or
world-readable artifact in there is needless surface.

The clock (``now``) and install ``home`` are injected so the recorded timestamp
is deterministic in tests and the path resolves under a temp dir without a TTY,
a real ``$CALFCORD_HOME``, or the wall clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from calfcord._atomic import atomic_write_text

_CHECKPOINT_FILENAME = "setup.json"
_STATE_DIRNAME = "state"
_SECRET_FILE_MODE = 0o600

# Bump on a breaking change to the checkpoint shape (field rename/removal or a
# changed meaning). Any on-disk version != this reads back as ``None`` (fresh).
SETUP_SCHEMA_VERSION = 1


class SetupCheckpoint(BaseModel):
    """Which ``init`` steps are done, plus the non-secret IDs already picked.

    Every field defaults to "not done" so a freshly-constructed checkpoint is
    indistinguishable from "no checkpoint" — both mean "start from the top".
    The wizard advances this and re-saves after each completed phase.

    No field holds a secret: the Discord token and provider key live only in
    ``.env`` (via :mod:`calfcord.cli._envfile`). ``guild_id`` / ``channel_id``
    are the non-secret identifiers the operator chose from the discovery
    pick-lists, kept here so a re-run can default to the working binding rather
    than re-prompting (don't clobber a working guild/channel, §12.7).
    """

    schema_version: int = SETUP_SCHEMA_VERSION
    provider_done: bool = False
    agent_name: str | None = None
    discord_done: bool = False
    broker_done: bool = False
    guild_id: str | None = None
    channel_id: str | None = None


def checkpoint_path(home: Path | None) -> Path:
    """Resolve the checkpoint path: ``<home>/state/setup.json`` (dev: ``state/``).

    Mirrors the install layout used by the supervisor and per-agent state: a
    native install keeps it under ``$CALFCORD_HOME/state``; a dev run (no home)
    falls back to the project-local ``state/`` dir, matching the non-shim
    defaults the rest of the CLI uses.
    """
    base = home / _STATE_DIRNAME if home is not None else Path(_STATE_DIRNAME)
    return base / _CHECKPOINT_FILENAME


def load(path: Path) -> SetupCheckpoint | None:
    """Read the checkpoint, returning ``None`` for any unusable file.

    Advisory contract (§12.7): this NEVER raises. A missing file, corrupt JSON,
    a non-object value, a missing/mismatched ``schema_version``, or an
    unreadable path all read back as ``None`` so a stale checkpoint can never
    cause a step to be wrongly skipped. The caller treats ``None`` as "no usable
    progress — start fresh", and re-verifies the real artifact for any step it
    *does* skip on the strength of a returned checkpoint.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # Missing file, a directory in its place, or any other read failure: the
        # advisory contract is "never raise on read", so fall back to fresh.
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Truncated / garbled (e.g. a crash mid-write on a non-atomic writer).
        return None

    if not isinstance(data, dict):
        # A JSON list/number/string is not a checkpoint object.
        return None

    # Strict version gate BEFORE model validation: without a matching version we
    # cannot trust the field meanings, so refuse rather than guess. This also
    # rejects a partial object that omits ``schema_version`` entirely.
    if data.get("schema_version") != SETUP_SCHEMA_VERSION:
        return None

    try:
        return SetupCheckpoint.model_validate(data)
    except ValidationError:
        # Right version but a malformed field (wrong type) — still unusable.
        return None


def save(path: Path, checkpoint: SetupCheckpoint, *, now: Callable[[], datetime] | None = None) -> None:
    """Atomically persist ``checkpoint`` to ``path``, stamping ``updated_at``.

    The write is temp-file → :func:`os.replace` (atomic, so a concurrent reader
    never sees a half-written file) and ``chmod 0600``. The parent directory is
    created on demand. ``updated_at`` is taken from the injected ``now`` (default
    :func:`datetime.now` in UTC) so tests are deterministic; it is recorded for
    human display ("last touched") and is not part of the resume decision.

    On any failure (including a :func:`os.replace` error or a Ctrl-C mid-write),
    the temp file is unlinked so crashed runs do not accumulate ``.tmp`` orphans
    in the install's ``state/`` dir.
    """
    clock = now or (lambda: datetime.now(UTC))
    payload = checkpoint.model_copy(
        update={"schema_version": SETUP_SCHEMA_VERSION}
    ).model_dump()
    payload["updated_at"] = clock().isoformat()
    body = json.dumps(payload, indent=2)

    # Atomic same-dir tmp + os.replace, chmod 0600 (the shared
    # calfcord._atomic.atomic_write_text): a concurrent reader never sees a
    # half-written file, the parent dir is created on demand, and a crashed write
    # leaves no ``.tmp`` orphan in the install's owner-only ``state/`` dir.
    atomic_write_text(path, body, mode=_SECRET_FILE_MODE)
