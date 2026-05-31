# Step Transcripts, Live Step Streaming & Tool-Call Replay — Implementation Plan

**Status**: Finalized v2 — reviewed by 4 agents; decision D-1a chosen (2026-05-30)
**Scope**: bridge process only. Introduces the project's first persistence layer
(local SQLite). Replaces the step-thread transcript with a live-updating progress
message + an on-demand inline expand/collapse toggle, and hydrates each agent's
reconstructed history with the tool calls it made on prior turns.
**Touches**: `bridge/steps.py`, `bridge/steps_state.py`, `bridge/outbox.py`,
`bridge/gateway.py`, `bridge/history.py`, `bridge/ingress.py`,
`discord/persona.py`, `discord/settings.py`, new `bridge/transcripts.py`,
`docker-compose.yml`, and a near-total rewrite of `tests/bridge/test_steps.py`.

> **v2 changelog (from review):** single-blob store (was two tables) — resolves
> the `seq`-vs-cursor blocker; toggle moved to the **final reply** + transient
> progress message (was a persistent in-channel status message) — eliminates the
> pollution filter and the two-writer lock; `defer()` in the toggle callback;
> `agent_id`/row creation deferred to post-time; deploy volume + gateway wiring +
> test-rewrite breaks called out. **Decision D-1a chosen (§8): toggle on the
> final reply, transient progress deleted — no pollution filter, no edit lock.**
>
> **v3 (post-merge-review) — supersedes the inline toggle in §7.5/§8:** the
> expand/collapse toggle was replaced by **ephemeral step display**. Clicking the
> reply's button sends the turn's steps as an **ephemeral** message visible only
> to the clicker (`defer(thinking=True, ephemeral=True)` then an ephemeral
> followup; an oversized transcript is attached as `steps.md`, untruncated). The
> reply is never edited — removing the sentinel-collision, 2000-char-clamp,
> chunked-reply, and collapse-state failure modes the review surfaced. Also added
> a **Null-Object store-open degrade**: if the SQLite store can't open, the
> gateway logs a loud ERROR and substitutes a `NullTranscriptStore` so
> transcripts/replay/toggle disable instead of the bridge aborting; the outbox
> suppresses the button when the store is not `enabled`.

---

## 1. Goals

1. **Durable step transcripts** — persist each turn's intermediary tool calls +
   interim text (the structured slice) to bridge-local SQLite, surviving restarts.
2. **Live step streaming, minimized** — while an agent works, a single compact
   in-channel progress message under the agent persona (`⚙ running… N steps`),
   edited live (debounced).
3. **On-demand inline expand** — a toggle reveals the steps inline (truncated to
   the Discord limit) and collapses back.
4. **Tool-call replay** — on later turns, hydrate the agent's reconstructed
   `message_history` window with prior turns' structured tool calls/returns.

## 2. Non-goals (deferred)

- Components V2 / `LayoutView` (legacy 2000-char message is fine; expand truncates).
- Multi-bridge DB consistency (single bridge; DB is process-local, same as today's
  in-memory state).
- Token-budget replay trimming (`history_turns` already bounds replay).
- A2A `private_chat` / `egress.py` transcripts (separate surface, unchanged).
- Mid-run forensic durability (replay only reads *completed* turns — see §4).

## 3. Background — current state (verified)

| Concern | Today | File:line |
|---|---|---|
| Step surface | Lazily-created **thread**; one message per rendered part | `steps.py:365,493,611` |
| Step source | `agent.steps`; each hop's `NodeResult.message_history` **cumulative** (append-only), rendered by `_render_delta` | `steps.py:250`; `factory.py:382`; calfkit `state.py:35` ("append-only") |
| This-turn slice | `message_history[initial_len:]`; `initial_len` from `PendingEntry` | `pending_wires.py:96`; `ingress.py:359` |
| Multi-part hops | One `ModelResponse` can render to **N parts** (text + each tool call) | `steps.py:271-284`; `test_steps.py:273` |
| Terminal | `is_terminal = bool(result.output_parts)` | `steps.py:525` |
| Final reply | Outbox, `discord.outbox`, posts to channel with a **link** reply button via a `view=` already supported by `persona.send` | `outbox.py:148,192`; `persona.py:347` |
| History window | `channel.history(limit=history_turns)`, trimmed `[-history_turns:]`, projected POV-aware (self→`ModelResponse`, others→`ModelRequest`) | `ingress.py:645,648`; `history.py:617` |
| Window bound | `AgentDefinition.history_turns` (default **30**, 0–100); router `self_agent_id=None` ⇒ no "self" ⇒ no replay | `definition.py:152`; `ingress.py:494` |
| `/clear` | Fetcher truncates history at the most recent marker (before record build) | `history.py:503-509` |
| Persistence | **None** (in-memory LRU; Discord is source of truth) | `steps_state.py`, `pending_wires.py` |
| Topology | One gateway `_GatewayClient`; persona/REST senders `Intents.none()`; **all in one `asyncio.run` loop** | `gateway.py:338,494`; `persona.py:281` |
| Components | Only link buttons; no `custom_id`/callback/`add_view`/`on_interaction` | `persona.py:213` |

## 4. Load-bearing decisions (post-review)

| Decision | Why |
|---|---|
| **Single-blob store, written once on the terminal hop** | The terminal `message_history` is cumulative, so `message_history[initial_len:-1]` **is** the whole turn's structured transcript — serialize it with pydantic-ai's `ModelMessagesTypeAdapter` into one `delta_json`. Removes the second table, the `seq` counter (which couldn't be derived from `history_cursor` — they have different cardinality), the append path, and all "rebuild from rows" logic. Replay = `validate_json` + splice. Trade-off: a mid-run crash loses *that incomplete turn's* transcript — but replay only ever reads completed turns (it needs `final_message_id`), so this is functionally lossless. |
| **Reuse `_render_delta` as the only renderer** | Parse the blob → `list[ModelMessage]` → `_render_delta` for both the live counter (`len`) and the expanded view (`"\n\n".join(...)` truncated). One parser, one renderer, three call sites. |
| **Toggle on the final reply; live progress is transient** (see **D-1**) | Putting the durable toggle on the reply (one message, the `final_message_id` we already need) and **deleting** the transient progress message on the terminal hop means no permanent *extra* channel message — so **no history-pollution filter** and **no two-writer lock** are needed. |
| **`history_turns` bounds replay; no backstop** | Agents see ≤`history_turns` messages; only oversized individual tool returns are truncated, via a *new* `REPLAY_TOOL_RETURN_MAX_CHARS` (not the 1500-char Discord-render cap, which would lobotomize tool context). |
| **Replay is a join against fetcher output, never a DB scan** | The fetcher already truncates at the `/clear` marker before building records; joining spliced steps onto surviving records keeps `/clear` correct for free. A DB scan by `conversation_key` would resurrect pre-clear tool calls — forbidden. |

## 4.1 Data locality & deployment (distributed-safe by construction)

calfcord's tools/agents/router are independently deployable across hosts, but they
communicate **only over Kafka** and never read application state directly. An
embedded DB is compatible with that because **all four of its touchpoints live
inside the single bridge process**:

| Touchpoint | Where | DB |
|---|---|---|
| Write transcript (terminal hop) | steps consumer (bridge) | write |
| Set `final_message_id` | outbox consumer (bridge) | write |
| Expand/collapse toggle | gateway client callback (bridge) | read |
| Replay hydration | `_build_slash_message_history` (bridge), **before** the wire is published to Kafka | read |

**The load-bearing decision: hydration happens bridge-side.** Agents receive an
already-hydrated `message_history` over Kafka — they never fetch transcripts — so a
remote/distributed agent or tool host needs no DB access. (The original goal "feed
prior tool calls into the LLM" is met by the bridge enriching the history it ships,
not by the agent reading a DB.)

The bridge is a **hard singleton**, so a bridge-local DB has exactly one
reader/writer:
- `container_name: calfcord-bridge` (`docker-compose.yml:107`) prevents `--scale`
  (only the stateless `agent` service is scalable — and it never touches the DB).
- A Discord bot holds **one gateway connection per shard**, so all events *and* all
  button-click interactions for a guild are delivered to the one bridge — there is no
  second consumer to be "distributed away" from the data.

**Operational requirements:** (1) the bridge needs a **persistent volume** for the DB
file (Phase 0 — today only `agent` mounts `state/`); (2) never run two bridge
instances (Discord rejects a duplicate gateway connection anyway). If the bridge is
ever sharded for scale, the DB shards *with* it (a guild's transcripts and its
interactions live on the same shard); only a hypothetical cross-shard read would need
a distributed store (see §6 alternatives).

## 5. Architecture (v2)

```
agent ──Kafka──> calfkit-bridge (one process, one event loop)
  agent.steps (per hop) ─▶ STEPS consumer (steps.py)   [pure live-UI, NO DB]
        • keep cursor/peer-gating/monotonicity (unchanged invariants)
        • post/edit ONE transient progress msg "⚙ running… N steps" (debounced)
        • on TERMINAL: cancel debounce → DELETE the progress message
  discord.outbox (terminal) ─▶ OUTBOX consumer (outbox.py)   [SOLE DB writer]
        • post final reply; if the turn used tools, attach the steps toggle to it
        • write the COMPLETE row in ONE INSERT: delta_json =
          dump_json(message_history[initial_len:-1]), final_message_id = sent.id,
          conversation_key/agent_id/created_at — all known here, after a successful post
  gateway _on_ready ─▶ client.add_view(StepsToggleView())   ← one registered instance
        click → defer(message_update) → read transcripts by interaction.message.id
              → render (_render_delta, truncated) → edit_original_response (flip label)
  ingress projection (next turn, inside _build_slash_message_history):
        for each fetched record matching transcripts.final_message_id (agent==self):
          replace its projected ModelResponse with validate_json(delta_json) (+ trunc)
        — splice BEFORE len(message_history) is snapshotted at ingress.py:359
```

## 6. Data model — `bridge/transcripts.py` (one table)

```sql
CREATE TABLE transcripts (
  correlation_id    TEXT PRIMARY KEY,  -- idempotency key for outbox retries
  conversation_key  TEXT NOT NULL,     -- = wire.source_channel_id (replay read scope)
  agent_id          TEXT NOT NULL,     -- the outbox-resolved real emitter
  final_message_id  TEXT NOT NULL,     -- the posted reply; replay join key + toggle host id
  delta_json        TEXT NOT NULL,     -- ModelMessagesTypeAdapter.dump_json(history[initial_len:-1])
  created_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX ix_transcripts_final ON transcripts(final_message_id);
-- NOTE: deliberately NO conversation_key index used for replay (join-only; see §4).
```

- **Single writer = the outbox**, on the terminal hop, *after* a successful reply
  post — so every column (incl. `final_message_id`) is known at write time and the
  whole row is one `INSERT OR REPLACE` (idempotent on `correlation_id` for retries).
  No two-writer race, no nullable columns, no UPSERT-merge. This is what lets the
  steps consumer stay pure live-UI with no DB access, and it resolves review Majors 3
  (peer phantom rows — the outbox already resolves the real emitter) and 7
  (final_message_id only on success — we write only after a successful post).
- IDs as TEXT (snowflake precision).
- **Engine: SQLite** — correct shape for this workload (embedded, ACID, single-file,
  zero-ops; point lookups by `correlation_id`/`final_message_id`, the replay
  `WHERE final_message_id IN (...)` join, and JSON blobs). **Library: `aiosqlite`**
  (`uv add aiosqlite`) — one long-lived connection held for the bridge's lifetime,
  run on a background thread so a DB call never blocks the bridge's single event loop
  (the decisive property — *not* raw throughput; write volume is ~one row per agent
  turn, and the Discord network edits dominate latency). Pragmas: `journal_mode=WAL`,
  `synchronous=NORMAL`, `busy_timeout=5000`, `temp_store=MEMORY`, `mmap_size` set.
  Single writer (the outbox consumer) ⇒ no write contention.
- **Alternatives considered & rejected:** **DuckDB** — OLAP/columnar, tuned for
  analytical scans, wrong shape for frequent small point reads/writes. **LMDB/RocksDB**
  — faster pure-KV but we'd hand-maintain a `final_message_id` secondary index and lose
  SQL for the replay join. **stdlib `sqlite3` + `asyncio.to_thread`** — viable zero-dep
  fallback (aiosqlite is essentially this, battle-tested). **`apsw`** — fastest SQLite
  binding; unnecessary at this volume. **Future distribution only:** libSQL/Turso
  (SQLite fork with embedded replicas that sync to a remote primary) or rqlite/dqlite
  (Raft-replicated SQLite); **Litestream** streams the WAL to object storage for
  DR/backup with no code change. None needed under the singleton bridge.
- **Phase 0 gate:** confirm `ModelMessagesTypeAdapter` (importable from
  `calfkit._vendor.pydantic_ai.messages`) round-trips tool-call/return parts
  losslessly, including non-JSON-native return payloads.

## 7. Component changes

### 7.1 `discord/settings.py`
`transcript_db_path: Path` (env `DISCORD_TRANSCRIPT_DB_PATH`, default
`state/transcripts.sqlite3`).

### 7.2 `discord/persona.py`
- Add `edit_message(channel_id, message_id, *, content, view=MISSING)` →
  `webhook.edit_message(...)`. **Omit `view` to retain components on content-only
  edits; pass `view=` only when the button label must change.**
- Generalize `send` to accept a `view=` **independent of the `reply_to` branch**
  (don't regress the existing reply-button path).
- **View hygiene (API gotcha):** pass a *throwaway* `StepsToggleView()` instance to
  each `send`/`edit_message` (to emit component JSON); register **one separate**
  instance via `add_view` on the gateway client for dispatch. Never reconstruct the
  webhook via `Webhook.partial`/`from_url` (that yields `_WebhookState` and breaks
  interactable components).

### 7.3 `bridge/steps.py` (re-sink to a transient progress message — NO DB)
- **Keep unchanged invariants:** `_consume` cursor, peer-mirror gating + monotonic
  cursor (`steps.py:573,588`), `is_terminal`, `_render_delta`,
  `STEP_CONTENT_MAX_CHARS`.
- **Per hop:** post/edit ONE transient progress message (debounced ~1s trailing)
  showing the live step count `⚙ running… N steps`, under the per-hop resolved emitter
  persona. The count is derived from the rendered delta — **no DB write**.
- **Terminal hop:** cancel the pending debounce timer and **delete** the progress
  message (the outbox's reply + toggle supersede it); then pop/mark-completed. The
  debounce task handle lives on the entry and is cancelled here.
- **Retire** `_create_thread`, `_archive_thread`, thread routing, `THREAD_*`,
  `StepsEntry.thread_id`. `StepsState` keeps {progress_message_id, debounce handle,
  completed-guard, cursor}; final call in Phase 2.

### 7.4 `bridge/outbox.py` (the sole transcript writer)
- Post the reply as today. Inspect `message_history[initial_len:-1]` (the turn's
  delta, `initial_len` from `PendingEntry`): if it contains any `ToolCallPart` or
  non-empty interim `TextPart`, the turn **used tools** → attach the steps toggle to
  the reply and write a transcript row; otherwise (pure text) do neither.
- **Write the complete row in one `INSERT OR REPLACE`** keyed by `correlation_id`:
  `delta_json = ModelMessagesTypeAdapter.dump_json(history[initial_len:-1])`,
  `final_message_id = sent.id`, `conversation_key = wire.source_channel_id`,
  `agent_id = emitter`, `created_at = now`. Done at the **single success point** (the
  `sent` returned by `_send_with_one_retry_on_outage`, ~`outbox.py:192`); for the
  chunked-fallback path use the **first** chunk's id. Turns that drop / stay
  retry-pending write no row ⇒ no replay splice (documented degradation).

### 7.5 `bridge/gateway.py` — toggle UI
- `StepsToggleView(discord.ui.View, timeout=None)`, one button, static
  `custom_id="steps:toggle"`. Register once in `_on_ready` via
  `self._client.add_view(...)`.
- **Wiring change (gap):** `DiscordIngressGateway.__init__` today takes only
  `settings, ingress, registry, calfkit_client`. Thread the `TranscriptStore` in so
  `_on_ready` can build the view and the callback can read it.
- **Callback:** `await interaction.response.defer()` (→ `deferred_message_update`,
  invisible) **first**, then read `transcripts` by `interaction.message.id`, render
  (truncated), `interaction.edit_original_response(content=, view=)` flipping the
  label. Deferring removes the 3s-deadline coupling to the DB read + render.
  Handle three states: no row ("steps unavailable / expired"); row present;
  `final_message_id IS NULL` legacy/partial ("⚠ incomplete — bridge restarted").

### 7.6 `bridge/history.py` + `bridge/ingress.py` — replay
- Apply hydration as a **post-pass inside `_build_slash_message_history`**, mutating
  the projected `message_history` **before** `ingress.py:359` snapshots its length
  (keeps `initial_message_history_length` and the steps cursor consistent — extend
  `TestInitialCursorSeed`). Do **not** push DB access into the pure
  `project_history` (would touch the router/ambient POV paths).
- For each surviving fetched record whose `message_id == transcripts.final_message_id`
  **and** `agent_id == self_agent_id`: replace that record's projected
  `ModelResponse` with `validate_json(delta_json)` (truncating oversized tool returns
  to `REPLAY_TOOL_RETURN_MAX_CHARS`). Router (`self_agent_id=None`) and peers are
  untouched. `conversation_key = wire.source_channel_id`.

## 8. Resolved decision — **D-1a chosen: toggle on the final reply**

The review surfaced that the *single biggest source of complexity* was whether the
live progress message **persists** in the channel as the toggle host. **Decision:
D-1a** — this plan builds it; D-1b is recorded only as the rejected alternative.

- **D-1a (CHOSEN): transient progress + toggle on the reply.** Progress
  message streams live, then is **deleted** on the terminal hop; the durable toggle
  rides the **final reply** (relabel its button `⤵ N steps`). Eliminates the
  history-pollution filter (no permanent extra message) and the two-writer lock
  (live edits and toggle edits hit *different* messages). End state: one anchored
  artifact `[reply] [⤵ N steps]`. Diverges slightly from the earlier preview (no
  standalone `✓ N steps` line).
- **D-1b: persistent status message as toggle host** (matches the earlier preview
  `✓ 5 steps [⤵ Show steps]`). **Adds back:** (1) a history-**pollution filter** —
  exclude the status message from projection, made robust with a content/persona
  sentinel (id-join alone has a write-after-read race) and plumbed explicitly into
  the fetch path; (2) a **second id** (`steps_message_id`) on the row; (3) a
  **per-correlation edit guard** since live edits and toggle edits now share one
  message (derive expand-state from the button label, or a small `expanded` flag;
  never hold a lock across the network edit / interaction 3s deadline).

This plan implements **D-1a**. **D-1b** (a persistent status message as the toggle
host) was considered and rejected: it re-adds a history-pollution filter, a
`steps_message_id` column, and a shared-message edit guard for no functional gain.

## 9. Edge cases

- Pure-text turn: no progress message, no toggle, no row; reply unchanged.
- Restart mid-run: completed rows persist; an incomplete turn writes no row (replay
  ignores it); a stale progress message may linger (cosmetic, swept on boot by TTL).
- Outbox/steps either order: UPSERT by `correlation_id` is idempotent.
- Toggle on missing/partial row: explicit callback states (§7.5).
- Truncated expand: "… (rendered M steps, truncated)".
- Co-tenant peers: replay splices only `agent_id == self` turns.

## 10. Phased plan

- **Phase 0** — verify `ModelMessagesTypeAdapter` round-trip; verify
  `webhook.edit_message` + `interaction.response.defer`+`edit_original_response` on a
  persona webhook message with a `custom_id` button; add the setting; **add a
  `./state` (or named) volume mount to the `bridge` service in `docker-compose.yml`**
  (today only `agent` mounts `state/`, so the DB would be ephemeral and defeat Goal 1);
  `uv add aiosqlite`.
- **Phase 1** — `TranscriptStore` (single table, WAL, async) + unit tests.
- **Phase 2** — steps re-sink: transient progress message (debounced, terminal
  flush/cancel) + blob write; retire thread machinery; decide `StepsState` fate.
- **Phase 3** — `StepsToggleView` + `add_view` + gateway wiring + defer-based callback.
- **Phase 4** — outbox `final_message_id` UPSERT + conditional toggle attach.
- **Phase 5** — replay post-pass in `_build_slash_message_history` (join-only).
- **Phase 6** — retention (TTL/row-cap) + **rewrite `tests/bridge/test_steps.py` /
  `test_steps_state.py`** (they pin thread machinery via imports + mocks and will
  fail collection on `THREAD_FALLBACK_NAME` / `StepsEntry(thread_id=...)`) and add the
  new invariant + replay + pollution(if D-1b) tests.

## 11. Risks & open questions

- **Q-1** serialization fidelity (Phase 0 gate).
- **Q-2** `StepsState` removal vs shrink (Phase 2).
- **Q-3** retention defaults (age TTL vs per-conversation cap); also whether `/clear`
  writes a tombstone (not required for replay correctness under join-only, but tidy
  for retention).
- **Q-4** per-channel **webhook bucket** is shared by co-tenant agents (5 req / 2 s);
  ~1s debounce is fine for one stream — tighten under heavy same-channel fan-out and
  lean on discord.py's automatic 429 backoff.
- **Q-5** hydrated history enlarges the Kafka `PendingEntry` envelope (it already
  carries channel history; tool calls add to it, bounded by `history_turns` ×
  `REPLAY_TOOL_RETURN_MAX_CHARS`). Confirm it stays under the broker's max message
  size; the per-return truncation is the lever.

## 12. Testing

- Unit: `TranscriptStore` upsert/round-trip; `_render_delta` truncation; replay
  splice + `initial_message_history_length` invariant; final-flush-cancels-debounce.
- Rewritten `test_steps.py`: re-pin cursor-monotonicity / per-hop-persona / early-skip
  / outbox-retry-dedup against the new sink.
- Integration (gated): live progress edit, expand/collapse via toggle, and a two-turn
  replay showing tool calls in turn 2's context.
