"""Bridge-local SQLite store for per-turn agent step transcripts.

This is the project's first persistence layer. It backs the
step-transcript feature (see
``docs/design/step-transcripts-and-live-streaming-plan.md`` §4.1, §5, §6):
each completed agent turn that used tools writes ONE row holding the
serialized structured slice of that turn's ``message_history`` (the
``delta_json`` blob produced by pydantic-ai's
``ModelMessagesTypeAdapter``). The toggle UI and the next-turn replay
hydration both read those rows back.

**Single connection, single process.** The bridge runs on a single
asyncio event loop and is a hard singleton (the design doc spells out
why: ``container_name`` pins it, and one gateway connection per shard
funnels every event and button interaction to the one bridge). So this
store holds exactly ONE long-lived :class:`aiosqlite.Connection` for its
lifetime. The outbox consumer is the sole writer; the toggle callback
and the replay post-pass are readers — all in the same process. There is
no cross-process or cross-thread contention to guard against.

**Pragmas.** WAL journaling + ``synchronous=NORMAL`` keep the single
writer fast without blocking readers; ``busy_timeout`` is belt-and-
braces (there is no second writer); ``temp_store=MEMORY`` keeps sort/
temp scratch off disk.

Thread safety: aiosqlite runs the underlying ``sqlite3`` connection on
its own dedicated thread, so DB calls never block the bridge's event
loop. Do not share a :class:`TranscriptStore` across event loops; one
instance per bridge process.

Scope: this module is intentionally minimal — point lookups, a batch
join helper for replay, and an age-based prune. No retention-by-count,
no migration framework, no global singleton.
"""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)

# SQLite caps the number of host parameters in a single statement (the
# historical default ``SQLITE_MAX_VARIABLE_NUMBER`` is 999). Chunk batch
# ``IN (...)`` lookups well under that so a large id list never trips the
# limit; the per-chunk results are merged by the caller-facing method.
_IN_CHUNK_SIZE = 900

# Explicit column order, shared by the schema, every SELECT, and the
# row-tuple → :class:`TranscriptRow` mapper. Never ``SELECT *`` — column
# order is load-bearing for ``_row_from_tuple``.
_COLUMNS = (
    "correlation_id",
    "conversation_key",
    "agent_id",
    "final_message_id",
    "delta_json",
    "created_at",
)
_COLUMN_LIST = ", ".join(_COLUMNS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
  correlation_id    TEXT PRIMARY KEY,
  conversation_key  TEXT NOT NULL,
  agent_id          TEXT NOT NULL,
  final_message_id  TEXT NOT NULL,
  delta_json        TEXT NOT NULL,
  created_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_transcripts_final ON transcripts(final_message_id);
"""


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    """One persisted agent turn's structured step transcript.

    ``slots=True`` catches typo-creates-new-attribute bugs; ``frozen``
    makes a row an immutable value once read out of (or built for) the
    store.

    Fields mirror the ``transcripts`` table one-to-one:

    * ``correlation_id`` — idempotency key for outbox retries (PK).
    * ``conversation_key`` — the replay read scope
      (``wire.source_channel_id``).
    * ``agent_id`` — the outbox-resolved real emitter.
    * ``final_message_id`` — the posted reply's id; the replay join key
      and the toggle host message id (UNIQUE).
    * ``delta_json`` — ``ModelMessagesTypeAdapter.dump_json`` of the
      turn's structured slice.
    * ``created_at`` — epoch seconds at write time (retention key).

    Snowflake ids are stored as TEXT to preserve precision.
    """

    correlation_id: str
    conversation_key: str
    agent_id: str
    final_message_id: str
    delta_json: str
    created_at: int


def _row_from_tuple(row: tuple[object, ...]) -> TranscriptRow:
    """Map a DB row tuple (in :data:`_COLUMNS` order) to a :class:`TranscriptRow`."""
    correlation_id, conversation_key, agent_id, final_message_id, delta_json, created_at = row
    return TranscriptRow(
        correlation_id=str(correlation_id),
        conversation_key=str(conversation_key),
        agent_id=str(agent_id),
        final_message_id=str(final_message_id),
        delta_json=str(delta_json),
        created_at=int(created_at),  # type: ignore[arg-type]
    )


class TranscriptStore:
    """A single long-lived ``aiosqlite`` connection over the ``transcripts`` table.

    Open with :meth:`connect` (idempotent) and release with
    :meth:`close` (safe when not connected), or use the instance as an
    async context manager. All DB methods are async and assume the store
    is connected; calling them before :meth:`connect` raises
    :class:`RuntimeError`.
    """

    # A live, persistent store: transcripts, replay, and the expand toggle
    # are all active. The :class:`NullTranscriptStore` substitute reports
    # ``False`` so the WRITER (the outbox) can gate the toggle-attach + the
    # transcript write off when the real store failed to open. READERS
    # (steps_toggle, ingress replay) do NOT check this flag — they call the
    # read methods unconditionally and rely on the Null store's no-op
    # returns. A class attribute (not a property) since it is a fixed truth
    # for every real instance.
    enabled = True

    def __init__(self, db_path: pathlib.Path) -> None:
        """Record the DB path. The connection is NOT opened here — call
        :meth:`connect` (or use the async-context-manager form) so the
        connection's lifetime is tied to an explicit, awaitable step."""
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection, set pragmas, and create the schema.

        Idempotent: a second call while already connected is a no-op.
        Creates the DB file's parent directory if missing. The schema is
        created with ``IF NOT EXISTS`` so reconnecting to an existing DB
        is safe.
        """
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._db_path)
        try:
            # WAL + NORMAL: fast single-writer, non-blocking reads. The
            # busy_timeout is defensive (there is no second writer);
            # temp_store=MEMORY keeps temp/sort scratch off disk.
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA temp_store=MEMORY")
            await conn.executescript(_SCHEMA)
            await conn.commit()
        except BaseException:
            # Don't leak the half-initialized connection if pragma/schema
            # setup fails; surface the original error to the caller.
            await conn.close()
            raise
        self._conn = conn
        logger.debug("transcript store connected db_path=%s", self._db_path)

    async def close(self) -> None:
        """Close the connection if open. Safe to call when not connected."""
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        await conn.close()
        logger.debug("transcript store closed db_path=%s", self._db_path)

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("TranscriptStore is not connected; call connect() first")
        return self._conn

    async def write_turn(self, row: TranscriptRow) -> None:
        """Upsert a transcript row, keyed on ``correlation_id``.

        Uses ``INSERT ... ON CONFLICT(correlation_id) DO UPDATE`` so an
        outbox retry that re-posts under the **same** ``correlation_id``
        overwrites the prior row in place (PK-idempotent for retries),
        rather than erroring or duplicating. The query is fully
        parameterized.

        Unlike the previous ``INSERT OR REPLACE``, a conflict on the
        *secondary* ``UNIQUE`` index ``ix_transcripts_final`` — i.e. a
        **different** ``correlation_id`` reusing an existing
        ``final_message_id`` — is NOT silently resolved by evicting the
        conflicting row. The conflict target names only the
        ``correlation_id`` PK, so any other uniqueness violation propagates
        as :class:`sqlite3.IntegrityError`. That makes a final-message-id
        collision (which should never happen — Discord ids are unique)
        loud rather than silently dropping a prior turn's transcript.
        """
        conn = self._require_conn()
        await conn.execute(
            f"INSERT INTO transcripts ({_COLUMN_LIST}) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(correlation_id) DO UPDATE SET "
            "conversation_key=excluded.conversation_key, "
            "agent_id=excluded.agent_id, "
            "final_message_id=excluded.final_message_id, "
            "delta_json=excluded.delta_json, "
            "created_at=excluded.created_at",
            (
                row.correlation_id,
                row.conversation_key,
                row.agent_id,
                row.final_message_id,
                row.delta_json,
                row.created_at,
            ),
        )
        await conn.commit()

    async def get_by_final_message_id(self, final_message_id: str) -> TranscriptRow | None:
        """Return the row whose ``final_message_id`` matches, or ``None``."""
        conn = self._require_conn()
        async with conn.execute(
            f"SELECT {_COLUMN_LIST} FROM transcripts WHERE final_message_id = ?",
            (final_message_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_from_tuple(row)

    async def get_by_final_message_ids(self, final_message_ids: Sequence[str]) -> dict[str, TranscriptRow]:
        """Batch lookup keyed by ``final_message_id``.

        Returns ``{}`` for empty input WITHOUT issuing a query. Missing
        ids are simply absent from the result. The id list is chunked
        under SQLite's host-parameter limit (:data:`_IN_CHUNK_SIZE`) and
        the per-chunk results are merged; placeholders are built from the
        chunk length (parameterized — the ids themselves are never
        string-interpolated into the SQL).
        """
        if not final_message_ids:
            return {}
        conn = self._require_conn()
        result: dict[str, TranscriptRow] = {}
        for start in range(0, len(final_message_ids), _IN_CHUNK_SIZE):
            chunk = final_message_ids[start : start + _IN_CHUNK_SIZE]
            placeholders = ", ".join("?" * len(chunk))
            async with conn.execute(
                f"SELECT {_COLUMN_LIST} FROM transcripts WHERE final_message_id IN ({placeholders})",
                tuple(chunk),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                mapped = _row_from_tuple(row)
                result[mapped.final_message_id] = mapped
        return result

    async def prune_older_than(self, cutoff_created_at: int) -> int:
        """Delete rows with ``created_at`` strictly less than the cutoff.

        Returns the number of rows deleted.
        """
        conn = self._require_conn()
        cursor = await conn.execute(
            "DELETE FROM transcripts WHERE created_at < ?",
            (cutoff_created_at,),
        )
        deleted = cursor.rowcount
        await cursor.close()
        await conn.commit()
        return deleted

    async def __aenter__(self) -> TranscriptStore:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class NullTranscriptStore:
    """No-op stand-in substituted when the real store fails to open.

    A failed store open must NOT abort the bridge (which would kill all
    Discord routing). The gateway degrades to this Null-Object so the
    bridge keeps running with transcripts, tool-call replay, and the
    expand toggle DISABLED for the run: ``enabled`` is ``False``, writes
    are dropped, and reads return empty. Mirrors the read/write surface
    callers use against :class:`TranscriptStore` so no caller needs to
    branch on which store it holds.

    **The contract is NOT "every caller gates on ``enabled``".** It is
    split by role:

    * **Writers** (the outbox) gate the toggle-attach + the
      :meth:`write_turn` call on :attr:`enabled` — so a disabled run never
      shows a dead toggle with no row behind it.
    * **Readers** (steps_toggle's click callback, ingress's replay
      hydration) do NOT check :attr:`enabled`. They call the read methods
      unconditionally and rely on this store's no-op returns —
      :meth:`get_by_final_message_id` ⇒ ``None`` and
      :meth:`get_by_final_message_ids` ⇒ ``{}`` — to degrade to "nothing
      to show / nothing to replay" without a per-call branch.
    """

    enabled = False

    async def write_turn(self, row: TranscriptRow) -> None:
        """No-op: the failed-open store drops every write."""
        return None

    async def get_by_final_message_id(self, final_message_id: str) -> TranscriptRow | None:
        """Always misses — there is nothing persisted to read."""
        return None

    async def get_by_final_message_ids(self, final_message_ids: Sequence[str]) -> dict[str, TranscriptRow]:
        """Always empty — no rows to join against (⇒ no replay hydration)."""
        return {}

    async def prune_older_than(self, cutoff_created_at: int) -> int:
        """Nothing to prune; reports zero rows deleted."""
        return 0

    async def close(self) -> None:
        """No-op: there is no connection to release."""
        return None


# Either the real store or its no-op substitute. Callers type their
# transcript-store parameters/fields as this union rather than branching on
# which concrete class they hold. The ``enabled`` flag is consulted only by
# WRITERS (the outbox gates the toggle-attach + write on it); READERS
# (steps_toggle, ingress replay) call the read methods unconditionally and
# lean on the Null store's no-op ``None`` / ``{}`` returns.
TranscriptStoreLike = TranscriptStore | NullTranscriptStore
