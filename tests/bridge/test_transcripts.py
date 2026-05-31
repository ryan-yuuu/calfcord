"""Unit tests for :class:`TranscriptStore` (bridge-local SQLite transcript store).

Each test gets its own DB file under the ``tmp_path`` fixture, so the
tests are isolated and never touch a shared on-disk store. The repo runs
under ``asyncio_mode = "auto"`` (see ``pyproject.toml``), so ``async def
test_...`` functions are collected and run without an explicit
``@pytest.mark.asyncio`` decorator.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from calfkit_organization.bridge.transcripts import (
    NullTranscriptStore,
    TranscriptRow,
    TranscriptStore,
)

# A realistic ModelMessagesTypeAdapter-style blob: the structured slice
# of a turn's message_history with a tool call + return, JSON-escaped
# quotes and a unicode char, to exercise exact round-trip fidelity.
_DELTA_JSON = (
    '[{"parts":[{"part_kind":"tool-call","tool_name":"search",'
    '"args":"{\\"q\\": \\"caf\\u00e9 hours\\"}","tool_call_id":"call_1"}],'
    '"kind":"response"},{"parts":[{"part_kind":"tool-return",'
    '"tool_name":"search","content":"open until 9pm","tool_call_id":"call_1"}],'
    '"kind":"request"}]'
)


def _row(
    *,
    correlation_id: str = "corr-1",
    conversation_key: str = "chan-100",
    agent_id: str = "scheduler",
    final_message_id: str = "msg-9001",
    delta_json: str = _DELTA_JSON,
    created_at: int = 1000,
) -> TranscriptRow:
    return TranscriptRow(
        correlation_id=correlation_id,
        conversation_key=conversation_key,
        agent_id=agent_id,
        final_message_id=final_message_id,
        delta_json=delta_json,
        created_at=created_at,
    )


def _db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    # Nest under a missing subdir so we also exercise parent-dir creation.
    return tmp_path / "state" / "transcripts.sqlite3"


async def test_connect_creates_schema_and_file(tmp_path: pathlib.Path) -> None:
    db_path = _db_path(tmp_path)
    assert not db_path.exists()
    store = TranscriptStore(db_path)
    await store.connect()
    try:
        assert db_path.exists()
        # A write/read round-trips, proving the schema + index exist.
        await store.write_turn(_row())
        assert await store.get_by_final_message_id("msg-9001") is not None
    finally:
        await store.close()


async def test_reconnect_to_existing_db_is_fine(tmp_path: pathlib.Path) -> None:
    db_path = _db_path(tmp_path)
    store1 = TranscriptStore(db_path)
    await store1.connect()
    await store1.write_turn(_row())
    await store1.close()

    # Reconnecting against the existing file (IF NOT EXISTS schema) must
    # not error and must see the previously written row.
    store2 = TranscriptStore(db_path)
    await store2.connect()
    try:
        fetched = await store2.get_by_final_message_id("msg-9001")
        assert fetched is not None
        assert fetched.correlation_id == "corr-1"
    finally:
        await store2.close()


async def test_double_connect_is_idempotent(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    await store.connect()
    try:
        # Second connect is a no-op; the store stays usable.
        await store.connect()
        await store.write_turn(_row())
        assert await store.get_by_final_message_id("msg-9001") is not None
    finally:
        await store.close()


async def test_close_when_not_connected_is_safe(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    # No connect(); close() must be a harmless no-op.
    await store.close()
    # And a redundant close after a real connect cycle is also safe.
    await store.connect()
    await store.close()
    await store.close()


async def test_methods_before_connect_raise_runtime_error(tmp_path: pathlib.Path) -> None:
    # A store that was never connected has no live aiosqlite connection, so
    # every DB method must fail fast with RuntimeError (via _require_conn)
    # rather than dereferencing a None connection — guarding the documented
    # "call connect() first" contract on BOTH a read and a write path.
    store = TranscriptStore(_db_path(tmp_path))
    with pytest.raises(RuntimeError):
        await store.get_by_final_message_id("msg-9001")
    with pytest.raises(RuntimeError):
        await store.write_turn(_row())


async def test_write_then_get_round_trips_all_fields(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        row = _row(
            correlation_id="corr-rt",
            conversation_key="chan-777",
            agent_id="finance",
            final_message_id="msg-rt-1",
            delta_json=_DELTA_JSON,
            created_at=1717000000,
        )
        await store.write_turn(row)
        fetched = await store.get_by_final_message_id("msg-rt-1")
    assert fetched == row
    # Spell out a couple of fields to guard against an accidental
    # column-order mismatch in the mapper that a == happened to mask.
    assert fetched is not None
    assert fetched.delta_json == _DELTA_JSON
    assert fetched.created_at == 1717000000
    assert isinstance(fetched.created_at, int)


async def test_insert_or_replace_idempotent_latest_wins(tmp_path: pathlib.Path) -> None:
    # write_turn now uses ON CONFLICT(correlation_id) DO UPDATE; a retry
    # under the SAME correlation_id must still overwrite the prior row in
    # place (PK-idempotent), latest values winning.
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        await store.write_turn(_row(correlation_id="corr-dup", final_message_id="msg-A", created_at=10))
        # Same correlation_id (the idempotency key); newer values.
        await store.write_turn(
            _row(
                correlation_id="corr-dup",
                final_message_id="msg-B",
                delta_json='[{"v":2}]',
                created_at=20,
            )
        )
        # Exactly one row remains, carrying the latest values.
        assert await store.get_by_final_message_id("msg-A") is None
        latest = await store.get_by_final_message_id("msg-B")
        assert latest is not None
        assert latest.delta_json == '[{"v":2}]'
        assert latest.created_at == 20
        # Confirm the table truly holds a single row, not two.
        count = await store.get_by_final_message_ids(["msg-A", "msg-B"])
        assert list(count) == ["msg-B"]


async def test_get_by_final_message_ids_batch(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        await store.write_turn(_row(correlation_id="c1", final_message_id="m1"))
        await store.write_turn(_row(correlation_id="c2", final_message_id="m2"))
        await store.write_turn(_row(correlation_id="c3", final_message_id="m3"))

        result = await store.get_by_final_message_ids(["m1", "m3", "missing"])
    assert set(result) == {"m1", "m3"}
    assert result["m1"].correlation_id == "c1"
    assert result["m3"].correlation_id == "c3"
    assert "missing" not in result
    assert "m2" not in result


async def test_get_by_final_message_ids_empty_returns_empty(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        assert await store.get_by_final_message_ids([]) == {}


async def test_get_by_final_message_ids_chunks_large_input(tmp_path: pathlib.Path) -> None:
    # More ids than the per-query chunk size forces the chunk/merge path.
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        ids = [f"m{i}" for i in range(2000)]
        for i, fmid in enumerate(ids):
            await store.write_turn(_row(correlation_id=f"c{i}", final_message_id=fmid))
        # Also include some ids that don't exist.
        query_ids = [*ids, "nope-1", "nope-2"]
        result = await store.get_by_final_message_ids(query_ids)
    assert set(result) == set(ids)
    assert len(result) == 2000


async def test_get_by_final_message_id_unknown_returns_none(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        await store.write_turn(_row())
        assert await store.get_by_final_message_id("does-not-exist") is None


async def test_unique_final_message_id_index_enforced(tmp_path: pathlib.Path) -> None:
    # The schema declares ix_transcripts_final UNIQUE. A *plain* INSERT
    # (ABORT conflict mode) of a second, different correlation_id reusing
    # an existing final_message_id must therefore raise — this is the
    # direct proof the unique index exists and is enforced.
    #
    # NB: ``write_turn`` uses INSERT ... ON CONFLICT(correlation_id) DO
    # UPDATE — its conflict target is only the correlation_id PK, so a
    # secondary-index (final_message_id) collision is NOT resolved by it
    # and propagates as IntegrityError too; that behavior is asserted
    # directly via ``write_turn`` below. Here we go straight to the
    # connection with a plain INSERT to exercise the index in isolation.
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        await store.write_turn(_row(correlation_id="corr-X", final_message_id="shared-msg"))
        conn = store._require_conn()
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO transcripts "
                "(correlation_id, conversation_key, agent_id, final_message_id, delta_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("corr-Y", "chan-100", "scheduler", "shared-msg", _DELTA_JSON, 1000),
            )


async def test_write_turn_rejects_duplicate_final_message_id_across_correlations(tmp_path: pathlib.Path) -> None:
    # write_turn's ON CONFLICT target is only the correlation_id PK, so a
    # DIFFERENT correlation_id that reuses an existing final_message_id
    # violates the secondary UNIQUE index and is rejected LOUDLY with an
    # IntegrityError — rather than the old INSERT OR REPLACE behavior that
    # silently evicted the prior row (and its transcript) to make room.
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        await store.write_turn(_row(correlation_id="corr-X", final_message_id="shared-msg", created_at=1))
        with pytest.raises(sqlite3.IntegrityError):
            await store.write_turn(_row(correlation_id="corr-Y", final_message_id="shared-msg", created_at=2))

        # The original row is untouched — nothing was silently evicted.
        surviving = await store.get_by_final_message_id("shared-msg")
        assert surviving is not None
        assert surviving.correlation_id == "corr-X"
        assert surviving.created_at == 1


async def test_prune_older_than_deletes_strictly_older(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    async with store:
        await store.write_turn(_row(correlation_id="old1", final_message_id="o1", created_at=100))
        await store.write_turn(_row(correlation_id="old2", final_message_id="o2", created_at=199))
        await store.write_turn(_row(correlation_id="edge", final_message_id="e1", created_at=200))
        await store.write_turn(_row(correlation_id="new1", final_message_id="n1", created_at=300))

        # cutoff=200 deletes the two strictly-older rows; the row AT the
        # cutoff and the newer row survive.
        deleted = await store.prune_older_than(200)
        assert deleted == 2

        surviving = await store.get_by_final_message_ids(["o1", "o2", "e1", "n1"])
        assert set(surviving) == {"e1", "n1"}

        # Pruning again with the same cutoff deletes nothing.
        assert await store.prune_older_than(200) == 0


async def test_journal_mode_is_wal_after_connect(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    await store.connect()
    try:
        conn = store._require_conn()
        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert str(row[0]).lower() == "wal"
    finally:
        await store.close()


async def test_async_context_manager_opens_and_closes(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(_db_path(tmp_path))
    async with store as entered:
        # __aenter__ returns the store itself, opened and usable.
        assert entered is store
        await store.write_turn(_row())
        assert await store.get_by_final_message_id("msg-9001") is not None
    # After the context exits the connection is released; methods now raise.
    with pytest.raises(RuntimeError):
        await store.get_by_final_message_id("msg-9001")


def test_real_store_reports_enabled_true(tmp_path: pathlib.Path) -> None:
    # The real store is always enabled — transcripts, replay, and the toggle
    # are active for it. (No connection needed; ``enabled`` is a class-level
    # truth.)
    store = TranscriptStore(_db_path(tmp_path))
    assert store.enabled is True


def test_null_store_reports_enabled_false() -> None:
    # The Null-Object substitute reports disabled so callers gate transcript/
    # replay/toggle behaviour off.
    assert NullTranscriptStore().enabled is False


async def test_null_store_write_is_noop_and_reads_empty() -> None:
    store = NullTranscriptStore()
    # write_turn is a no-op (returns None, persists nothing).
    assert await store.write_turn(_row()) is None
    # Reads always miss — a single lookup returns None and a batch returns {}.
    assert await store.get_by_final_message_id("msg-9001") is None
    assert await store.get_by_final_message_ids(["msg-9001", "msg-2"]) == {}
    # A prior write left nothing behind to read.
    assert await store.get_by_final_message_id("msg-9001") is None


async def test_null_store_prune_returns_zero() -> None:
    assert await NullTranscriptStore().prune_older_than(999_999) == 0


async def test_null_store_close_is_noop() -> None:
    # No connection to release; close is a harmless no-op (idempotent).
    store = NullTranscriptStore()
    assert await store.close() is None
    assert await store.close() is None


def test_null_store_mirrors_real_store_surface() -> None:
    # The Null-Object substitute must mirror the REAL store's caller-facing
    # method surface so no caller has to branch on which concrete class it
    # holds (the documented TranscriptStoreLike contract). If a new public
    # method is added to TranscriptStore without a NullTranscriptStore
    # counterpart, an ingress/toggle reader (which calls read methods
    # unconditionally and relies on the Null store's no-op returns) would
    # blow up with AttributeError at runtime on a failed-open run. This guard
    # makes that a unit-test failure instead.
    #
    # ``connect`` is excluded: it is the real store's open step (replaced by
    # the gateway's degrade-to-Null path), not part of the surface callers
    # invoke against the union.
    real = {
        n
        for n in vars(TranscriptStore)
        if callable(getattr(TranscriptStore, n)) and not n.startswith("_") and n != "connect"
    }
    null = {n for n in vars(NullTranscriptStore) if not n.startswith("_")}
    missing = real - null
    assert real <= null, f"NullTranscriptStore is missing real-store method(s): {sorted(missing)}"
