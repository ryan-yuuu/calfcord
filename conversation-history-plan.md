# Conversation History v1 — Implementation Plan

**Status**: Awaiting approval (drafted 2026-05-23)
**Scope**: v1 — Discord-as-source-of-truth, last-N truncation, agent-POV projection
**Touches**: bridge process only (other deployments untouched)

## 1. Goals

Give every agent invocation the recent channel context, projected from that agent's point of view, so that:

- A user can reference earlier exchanges (`"and now do that for next week"`) without restating.
- Multi-agent personas can sustain coherent multi-turn conversations.
- The router can route on conversational context, not just the current message.

## 2. Non-goals (deferred)

- Persistence of any kind (Discord is the source of truth).
- Token-budget cap (v1 is count-based; token budget is v2).
- Edit/delete event handling (next fetch reflects current state).
- A2A `private_chat` history (stateless RPC, confirmed).
- Summarization, vector retrieval, tool-driven recall.
- Multi-bridge cache consistency (single-bridge today; cache is process-local).

## 3. Architectural overview

```
Discord channel
   │  REST: channel.history(limit=N, before=msg_id)
   ▼
┌────────── calfkit-bridge (only process that changes) ──────┐
│                                                            │
│ gateway._on_message / interaction handler                  │
│   │                                                        │
│   ▼                                                        │
│ MessageNormalizer.normalize (+ SlashNormalizer)            │
│   - now also populates wire.source_channel_id              │
│   │                                                        │
│   ▼                                                        │
│ BridgeIngress.handle(wire, *, prefetched_history=None)     │
│   │                                                        │
│   ├─ wire.kind == "slash":                                 │
│   │     records = prefetched_history                       │
│   │              or fetcher.fetch(..., limit=target.N)     │
│   │     history = project_history(records, target_id)      │
│   │     client.invoke_node(message_history=history, ...)   │
│   │                                                        │
│   └─ wire.kind == "message" (ambient):                     │
│         records = fetcher.fetch(..., limit=max_N)          │
│         router_history = project_history(records, None)    │
│                          [-router_N:]                       │
│         envelope = MetadataEnvelope(wire, phonebook,        │
│                                      history=tuple(records))│
│         invoke_node_with_metadata(message_history=          │
│                                    router_history, ...)     │
│                                                            │
│ synthesized.py consumer:                                   │
│   envelope = extract(state.metadata)                       │
│   ingress.handle(envelope.wire,                            │
│                  prefetched_history=envelope.history)      │
└────────────────────────────────────────────────────────────┘
```

A2A `private_chat` (in the tools process) is **unchanged** — stateless RPC.

## 4. Design decisions (load-bearing choices with rationale)

| Decision | Why |
|---|---|
| Discord is the source of truth, fetched on-demand | No persistence layer to maintain; survives any bridge restart; aligns with the project's "Kafka = system of record, Discord = audit" inversion for human-readable conversation state |
| Last-N truncation (count-based) | Simplest mental model. Token-budget cap is v2 |
| Per-target POV projection: self → `ModelResponse`; others → `ModelRequest` with `<author>` prefix | Empirically validated (gpt-4o-mini correctly attributes with the prefix alone). Portable across OpenAI Chat Completions, Responses API, and Anthropic — none of which support a per-message `name` field uniformly |
| Bridge owns the fetcher | Only process with the Discord gateway client; multi-process fetcher would require either shared `discord.Client` (impossible) or its own bot token + cold REST (wasteful) |
| Eager fetch on ambient publish (not lazy at synth-in re-entry) | One Discord call per ambient regardless of fan-out width; gives the router actual channel context for better routing decisions; all fan-out targets share the same snapshot |
| **No `peel_trailing_request`, no in-projection merge** | Pydantic-ai's `_clean_message_history` (`_agent_graph.py:1386–1432`) auto-merges adjacent same-role messages with compatible instructions before the model call. Verified at both `_agent_graph.py:213` (UserPromptNode) and `_agent_graph.py:526` (ModelRequestNode._prepare_request). Our constructed messages all have `instructions=None` and no provider metadata, so merge conditions are always met |
| Drop leading `ModelResponse(s)` from projection | Anthropic requires user-first messages. `_clean_message_history` merges but never drops; this stays our responsibility |
| Drop empty-content records | Pydantic-ai's Anthropic mapper has a `len(user_content_params) > 0` guard at `models/anthropic.py:740` (silently skips empty); OpenAI accepts. Filtering upstream is cleaner and saves bytes on the Kafka envelope |
| Single byte/count cap from frontmatter, no byte budget | Token-budget cap is the next iteration. Discord's per-call REST cap (100 messages) is the hard ceiling for v1 |
| TTL cache 2s, keyed on `(source_channel_id, before_message_id, limit)` | Absorbs router fan-out bursts (one fetch serves N synthesized invocations) without complicating the cache key |
| `discord.HTTPException` / `Forbidden` / `NotFound` → empty history + WARN | Never break the invocation because of a fetch failure. Log-once-per-channel for `Forbidden` to surface misconfiguration without log spam |
| Naïve fetcher — no category filtering | Honest transcript including bridge's own error replies, third-party bots, removed-agent webhook posts. Empty-content drop is the ONLY filter, and it's data hygiene (Anthropic 400 prevention), not category-based |

## 5. Data model changes

### 5.1 New: `HistoryRecord`

```python
# src/calfkit_organization/bridge/history.py
class HistoryRecord(BaseModel):
    """JSON-serializable snapshot of one Discord message for projection.

    Built by ChannelHistoryFetcher at fetch time so identity resolution
    happens once and downstream consumers don't need AgentRegistry access.
    """
    model_config = ConfigDict(frozen=True)

    message_id: int
    created_at: datetime
    content: str                          # may be ""; project_history drops these
    author_display_name: str              # user-visible label for <prefix>
    author_agent_id: str | None           # set if webhook display_name
                                          #   resolved to a registered agent
```

### 5.2 Extended: `MetadataEnvelope.history`

In `src/calfkit_organization/_compat/invoke.py`:

```python
class MetadataEnvelope(BaseModel):
    wire: WireMessage
    phonebook: tuple[PhonebookEntry, ...] | None = None
    history: tuple[HistoryRecord, ...] = ()        # NEW
```

`model_rebuild` at the bottom of the file needs `HistoryRecord` added to `_types_namespace`. Default `()` is non-breaking for any consumer that doesn't read it.

### 5.3 Extended: `WireMessage.source_channel_id`

In `src/calfkit_organization/bridge/wire.py`:

```python
class WireMessage(BaseModel):
    ...
    channel_id: int                       # parent-channel id (topic routing)
    source_channel_id: int | None = None  # NEW: actual landing channel
                                          #   (thread or top-level)
```

Add-only field; no `schema_version` bump per the existing wire-schema policy in `bridge/wire.py:10`. When `None`, the fetcher falls back to `channel_id` (correct for non-thread messages; thread messages would lose history during the rolling deploy window — acceptable).

**Ownership**: both `MessageNormalizer.normalize` and `SlashNormalizer.normalize` must populate this field. `MessageNormalizer` sets it from `message.channel.id`; `SlashNormalizer` sets it from `interaction.channel.id`. These are *separate* from `_resolve_channel_id` (which intentionally flattens threads to parent for topic routing).

### 5.4 Extended: `AgentDefinition.history_turns`

In `src/calfkit_organization/agents/definition.py`:

```python
history_turns: int = Field(default=30, ge=0, le=100)
# 0 disables history for this agent; 100 is Discord's per-call REST cap
```

Frontmatter (optional, default 30):
```yaml
history_turns: 30
```

### 5.5 Extended: `BridgeIngress.handle` signature

```python
async def handle(
    self,
    wire: WireMessage,
    *,
    prefetched_history: Sequence[HistoryRecord] | None = None,
) -> None:
```

Keyword-only, defaults `None` for backward compat. Used by the synthesized-in consumer to forward pre-fetched history without a refetch.

## 6. New module: `src/calfkit_organization/bridge/history.py`

Single self-contained module. Public exports: `HistoryRecord`, `ChannelHistoryFetcher`, `project_history`.

### 6.1 `ChannelHistoryFetcher`

```python
class ChannelHistoryFetcher:
    """Fetches recent Discord channel history as HistoryRecord lists.

    Wraps the bridge's discord.Client. Lives in the bridge process; the
    other deployments (router, agents, tools) receive history via Kafka
    envelopes and never call this.

    Per-channel 2-second TTL cache coalesces fan-out fetch bursts (one
    fetch serves the entire fan-out's synthesized invocations).

    All Discord errors (HTTPException, Forbidden, NotFound) are absorbed
    and turn into an empty list with a WARN log — never raised into the
    invocation path.
    """

    def __init__(
        self,
        discord_client: discord.Client,
        registry: AgentRegistry,
        *,
        cache_ttl_seconds: float = 2.0,
        cache_max_entries: int = 100,
    ) -> None:
        self._client = discord_client
        self._registry = registry
        self._cache_ttl = cache_ttl_seconds
        self._cache_max = cache_max_entries
        # Cache: (source_channel_id, before_message_id, limit) -> (ts, records)
        self._cache: OrderedDict[tuple[int, int, int],
                                  tuple[float, list[HistoryRecord]]] = OrderedDict()
        # Bounded LRU log-dedup for Forbidden errors (matches the pattern
        # in gateway.py:109 for _seen_message_ids).
        self._forbidden_log_dedup: OrderedDict[int, None] = OrderedDict()
        self._forbidden_log_max = 256

    async def fetch(
        self,
        *,
        source_channel_id: int,
        before_message_id: int,
        limit: int,
    ) -> list[HistoryRecord]:
        # 1. Normalize limit
        limit = min(max(0, limit), 100)
        if limit == 0:
            return []

        # 2. Cache check
        key = (source_channel_id, before_message_id, limit)
        now = monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._cache_ttl:
            self._cache.move_to_end(key)
            return list(cached[1])  # defensive copy

        # 3. Channel resolution
        channel = self._client.get_channel(source_channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(source_channel_id)
            except discord.NotFound:
                return self._cache_and_return(key, now, [])
            except discord.Forbidden:
                self._log_forbidden_once(source_channel_id)
                return self._cache_and_return(key, now, [])
            except discord.HTTPException:
                logger.warning(...)
                return self._cache_and_return(key, now, [])

        # 4. History iteration (newest-first from Discord)
        try:
            messages = [
                m async for m in channel.history(
                    limit=limit,
                    before=discord.Object(id=before_message_id),
                )
            ]
        except discord.Forbidden:
            self._log_forbidden_once(source_channel_id)
            return self._cache_and_return(key, now, [])
        except discord.HTTPException:
            logger.warning(...)
            return self._cache_and_return(key, now, [])

        # 5. Build records (oldest-first)
        records = [self._to_record(m) for m in reversed(messages)]

        # 6. Cache + return
        return self._cache_and_return(key, now, records)

    def _to_record(self, msg: discord.Message) -> HistoryRecord:
        author_display_name = msg.author.display_name or msg.author.name
        author_agent_id: str | None = None
        if msg.webhook_id is not None:
            spec = self._registry.by_display_name(author_display_name)
            if spec is not None:
                author_agent_id = spec.agent_id
        return HistoryRecord(
            message_id=msg.id,
            created_at=msg.created_at,
            content=msg.content,
            author_display_name=author_display_name,
            author_agent_id=author_agent_id,
        )

    def _cache_and_return(self, key, ts, records):
        self._cache[key] = (ts, records)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return list(records)

    def _log_forbidden_once(self, channel_id):
        if channel_id in self._forbidden_log_dedup:
            return
        self._forbidden_log_dedup[channel_id] = None
        while len(self._forbidden_log_dedup) > self._forbidden_log_max:
            self._forbidden_log_dedup.popitem(last=False)
        logger.warning(
            "channel=%d: Read Message History permission missing; "
            "agent invocations will run without channel history. "
            "Grant the bot 'Read Message History' permission to enable.",
            channel_id,
        )
```

### 6.2 `project_history`

Pure function. Reads records, returns `list[ModelMessage]` from the target's POV.

```python
def project_history(
    records: Sequence[HistoryRecord],
    self_agent_id: str | None,
) -> list[ModelMessage]:
    """Project records into agent-POV ModelMessage list.

    self_agent_id=None means "outside observer" (used by the router).
    In that case everything classifies as ModelRequest.

    Does NOT merge consecutive same-role messages — pydantic-ai's
    _clean_message_history handles that automatically before the
    provider mapper sees the list (verified in
    calfkit/_vendor/pydantic_ai/_agent_graph.py:1386-1432, called at
    both line 213 and line 526). Our ModelRequests carry
    instructions=None and our ModelResponses carry no provider metadata,
    so pydantic-ai's merge conditions are met every time.

    DOES drop leading ModelResponse(s) — pydantic-ai merges but never
    drops, and Anthropic still requires the first message to be user.
    """
    out: list[ModelMessage] = []
    for r in records:
        # Step 1: drop empty content (Anthropic's mapper would skip
        # empty user_content_params anyway; OpenAI would waste tokens).
        if not r.content.strip():
            continue
        # Step 2: classify per POV
        is_self = (
            self_agent_id is not None
            and r.author_agent_id == self_agent_id
        )
        if is_self:
            out.append(ModelResponse(parts=[TextPart(content=r.content)]))
        else:
            prefix = f"<{r.author_display_name}> "
            out.append(
                ModelRequest(parts=[UserPromptPart(content=prefix + r.content)])
            )
    # Step 3: drop leading ModelResponse(s). Anthropic requires user-first;
    # pydantic-ai's merge does not drop, so this stays our job.
    while out and isinstance(out[0], ModelResponse):
        out.pop(0)
    return out
```

That's the entire projector. ~25 lines.

## 7. File-by-file changes

| File | Change |
|---|---|
| `src/calfkit_organization/bridge/history.py` | **NEW** — `HistoryRecord`, `ChannelHistoryFetcher`, `project_history` |
| `src/calfkit_organization/bridge/wire.py` | Add `source_channel_id: int \| None = None` |
| `src/calfkit_organization/bridge/normalizer.py` | Both `MessageNormalizer.normalize` and `SlashNormalizer.normalize` populate `source_channel_id` from `message.channel.id` / `interaction.channel.id` |
| `src/calfkit_organization/_compat/invoke.py` | Add `history: tuple[HistoryRecord, ...] = ()` to `MetadataEnvelope`; update `model_rebuild` `_types_namespace` |
| `src/calfkit_organization/agents/definition.py` | Add `history_turns: int = Field(default=30, ge=0, le=100)` |
| `src/calfkit_organization/bridge/ingress.py` | Accept fetcher via constructor (Optional, set via `set_fetcher`); add `prefetched_history` kwarg to `handle()`; wire history into slash and ambient branches |
| `src/calfkit_organization/bridge/synthesized.py` | Read `envelope.history`, pass to `ingress.handle(prefetched_history=...)` |
| `src/calfkit_organization/bridge/gateway.py` | Construct `ChannelHistoryFetcher(self._client._client, registry)` in `_on_ready`; call `self._ingress.set_fetcher(fetcher)` to inject |
| `src/calfkit_organization/router/definition.py` | Read `CALFKIT_ROUTER_HISTORY_TURNS` env (default 10); populate `history_turns` on the router definition |
| `tests/bridge/test_history.py` | **NEW** — projector, fetcher (mocked discord), error paths, cache, thread `source_channel_id` |
| `tests/bridge/test_ingress*.py` | Update existing tests to pass a no-op fake fetcher |
| `docs/ambient-routing.md` | Add operator runbook note: requires "Read Message History" Discord permission |

No SDK changes. No new processes. No new env vars beyond `CALFKIT_ROUTER_HISTORY_TURNS`.

## 8. Call-site wiring

### 8.1 Path A — Direct slash / @-mention

In `BridgeIngress.handle`, slash branch:

```python
spec = self._registry.by_id(wire.slash_target)
if prefetched_history is not None:
    records = list(prefetched_history)
elif self._fetcher is None:
    # Pre-ready window (gateway not yet ready). Degrade gracefully.
    records = []
else:
    records = await self._fetcher.fetch(
        source_channel_id=wire.source_channel_id or wire.channel_id,
        before_message_id=wire.message_id,
        limit=spec.history_turns,
    )

if spec.history_turns < len(records):
    records = records[-spec.history_turns:]

message_history = project_history(records, self_agent_id=wire.slash_target)

self._pending_wires.put(wire.event_id, wire)
try:
    handle = await self._client.invoke_node(
        user_prompt=wire.content,
        topic=self._ingress_topic_template.format(cid=wire.channel_id),
        correlation_id=wire.event_id,
        deps={"discord": wire.model_dump(mode="json"),
              "phonebook": phonebook_to_deps(phonebook)},
        output_type=str,
        model_settings=model_settings,
        temp_instructions=temp_instructions,
        message_history=message_history,        # NEW
    )
except Exception:
    self._pending_wires.pop(wire.event_id)
    raise
handle._future.cancel()
```

### 8.2 Path B — Ambient

In `BridgeIngress._publish_ambient`:

```python
# Determine fetch limit: max across all eligible assistants + router.
# Eager fetch is intentional: even when the router decides "ignore", we
# accept the wasted ~200ms in exchange for (a) router gets channel
# context for routing decisions, (b) all fan-out targets agree on the
# same history snapshot, (c) single fetch instead of N at synth-in.
assistants = [s for s in self._registry.all() if s.role == "assistant"]
fetch_limit = max(
    [s.history_turns for s in assistants] + [self._router_history_turns],
    default=0,
)

records: list[HistoryRecord] = []
if fetch_limit > 0 and self._fetcher is not None:
    records = await self._fetcher.fetch(
        source_channel_id=wire.source_channel_id or wire.channel_id,
        before_message_id=wire.message_id,
        limit=fetch_limit,
    )

# Router projection: outside observer (self_agent_id=None).
router_projected = project_history(records, self_agent_id=None)
if self._router_history_turns < len(router_projected):
    router_projected = router_projected[-self._router_history_turns:]

envelope = MetadataEnvelope(
    wire=wire,
    phonebook=tuple(phonebook),
    history=tuple(records),                     # NEW — raw, unprojected
)

return await invoke_node_with_metadata(
    self._client,
    user_prompt=wire.content,
    topic=AMBIENT_INGRESS_TOPIC,
    reply_topic=AMBIENT_REPLY_DISCARD_TOPIC,
    metadata=envelope.model_dump(mode="json"),
    deps={"discord": wire_dict, "phonebook": phonebook_dict},
    temp_instructions=temp_instructions,
    message_history=router_projected,           # NEW
    correlation_id=uuid_utils.uuid7().hex,
)
```

### 8.3 Path C — Synthesized slash (router fan-out → assistant)

In `bridge/synthesized.py`'s consumer:

```python
envelope = MetadataEnvelope.extract(result.state.metadata)
# Pass envelope.history straight through; Path A's slash branch handles
# per-target POV projection and trimming.
await ingress.handle(envelope.wire, prefetched_history=envelope.history)
```

No fetch in the router process. The bridge's synthesized-in consumer hands the raw records to Path A's slash branch, which re-projects from each fan-out target's POV.

### 8.4 Path D — A2A (`private_chat`)

**Unchanged.** Stateless RPC, confirmed.

## 9. Configuration

| Setting | Default | Override |
|---|---|---|
| Per-agent `history_turns` | 30 | `history_turns:` in `.md` frontmatter (0–100) |
| Router `history_turns` | 10 | `CALFKIT_ROUTER_HISTORY_TURNS` env |
| Fetcher TTL cache | 2 seconds | hardcoded |
| Fetcher cache LRU bound | 100 entries | hardcoded |
| Forbidden-log dedup bound | 256 channels | hardcoded |
| Discord per-call hard cap | 100 messages | enforced in fetcher |
| Required Discord permission | Read Message History | operator grant; bridge silently degrades to empty history if missing |

## 10. Error-handling matrix

| Failure | Detection | Behavior |
|---|---|---|
| `discord.HTTPException` (transient) | exception during `channel.history()` | return `[]`; log WARN with channel id + event id |
| `discord.Forbidden` (missing Read Message History) | exception type | return `[]`; log WARN **once per channel** via bounded LRU |
| `discord.NotFound` (channel deleted) | exception type | return `[]`; log WARN |
| Channel not in `get_channel` cache | `get_channel(id) is None` | try `fetch_channel(id)`; on failure return `[]` |
| Fetcher not yet injected (pre-`on_ready`) | `self._fetcher is None` | return `[]` from slash branch; ambient skips packing history |
| Malformed `envelope.history` from synth-in | pydantic `ValidationError` in `MetadataEnvelope.extract` | already wrapped via existing `raise_envelope_error` path |
| Empty `content` records | filtered in `project_history` step 1 | dropped silently |
| History list empty after projection | natural | passed as `[]` to `invoke_node`; agent runs with system prompt + user prompt only |
| `history_turns=0` | short-circuit | fetcher returns `[]` without Discord call |

## 11. Test plan

### 11.1 Unit tests — `tests/bridge/test_history.py` (NEW)

**Projector**:
- `test_project_history_simple`: 3 records, target=scribe → scribe's reply → ModelResponse; others → ModelRequest with `<author>` prefix.
- `test_project_history_pov_switches`: same 3 records, target=conan vs target=scribe → different classifications.
- `test_project_history_drops_empty_content`: `content=""` and `content="  "` skipped.
- `test_project_history_drops_leading_responses_iteratively`: `[Response, Response, Request, ...]` → `[Request, ...]`.
- `test_project_history_router_pov`: `self_agent_id=None` → all ModelRequest.
- `test_project_history_does_not_merge`: input `[Req, Req]` → output `[Req, Req]` (relies on pydantic-ai to merge).

**Fetcher** (using `unittest.mock.AsyncMock`/`SimpleNamespace` for discord client):
- `test_fetcher_happy_path`: returns oldest-first list.
- `test_fetcher_caps_limit_at_100`: caller asks 999, internal call uses 100.
- `test_fetcher_zero_limit_short_circuits`: no Discord call, returns `[]`.
- `test_fetcher_cache_hit_within_ttl`: 2 calls in 1s → 1 Discord call.
- `test_fetcher_cache_expires`: 2 calls 3s apart → 2 Discord calls.
- `test_fetcher_cache_lru_bound`: 101st distinct key evicts oldest.
- `test_fetcher_handles_http_exception`: returns `[]`, WARN logged.
- `test_fetcher_handles_forbidden_logs_once_per_channel`: 10 calls same channel → 1 WARN; 10 calls 10 different channels → 10 WARNs.
- `test_fetcher_handles_channel_not_found`: returns `[]`.
- `test_fetcher_falls_back_to_fetch_channel`: `get_channel` returns None → `fetch_channel` is called.
- `test_fetcher_resolves_webhook_to_agent_id`: webhook display_name matching registered agent → `author_agent_id` populated.
- `test_fetcher_unknown_webhook_passes_through`: display_name unknown → `author_agent_id=None`.
- `test_fetcher_uses_source_channel_id_for_threads`: thread fixture verifies `source_channel_id != channel_id`.

### 11.2 Integration tests — `tests/bridge/test_ingress*.py`

- `test_ingress_slash_fetches_and_projects`: full slash path with mocked fetcher → `invoke_node` called with non-empty `message_history`.
- `test_ingress_slash_uses_prefetched_history`: `prefetched_history` set → fetcher NOT called.
- `test_ingress_slash_pre_ready_fallback`: fetcher is `None` → empty history, invocation still happens.
- `test_ingress_ambient_packs_history_into_envelope`: envelope's `history` field populated and round-trips.
- `test_ingress_respects_per_agent_history_turns`: agent with `history_turns=5` → fetcher called with `limit=5`.
- `test_ingress_zero_history_turns_skips_fetch`: `history_turns=0` → fetcher.fetch not called.
- `test_synthesized_in_passes_envelope_history`: synthesized-in consumer reads envelope and forwards to `handle`.
- `test_router_gets_smaller_history_window`.

### 11.3 Provider-integration sanity test (gated)

`tests/bridge/test_history_provider_integration.py`, marked `@pytest.mark.integration`, gated on `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`:

- Construct a projected history that ends in `[..., user, user, user]` (forces the boundary case).
- Stage a user_prompt on top.
- Send to a real Anthropic model and a real OpenAI model.
- Assert no 400, and that the response mentions content from the merged user messages.

Purpose: regression alarm if pydantic-ai ever changes `_clean_message_history` behavior. Not part of normal CI; runnable on demand.

### 11.4 Regression: existing tests

- `test_ingress*`: update fixtures to pass a no-op fake fetcher.
- `test_normalizer*`: add assertion for `source_channel_id` populated on normalized wires.

## 12. Performance

| Metric | Estimate | Notes |
|---|---|---|
| Added latency per direct invocation | 150–300ms | One Discord REST call |
| Added latency per ambient invocation | 150–300ms | Same; cache absorbs fan-out re-fetches |
| Added Kafka payload size (ambient) | ~5–50KB | 30 records × ~500 bytes avg; bursts up to ~150KB on chatty channels |
| Token cost per invocation | +~3K input tokens | 30 messages × ~100 tokens avg; ~$0.0005 on 4o-mini |
| Discord rate limit pressure | trivial | 50 req/sec global; chat-bot volume far below |

## 13. Backward compatibility

- All new fields have defaults; no required `.md` migrations.
- `WireMessage.source_channel_id=None` falls back to `channel_id` (correct for non-thread; thread messages lose history during the rolling-deploy window).
- `MetadataEnvelope.history=()` default — old producers ship empty; new consumers handle empty.
- `BridgeIngress.handle` new param is keyword-only with default `None`.
- Wire schema_version not bumped (existing add-only policy).

## 14. Implementation order

Steps are designed to fail closed at each stage: every commit leaves the project working.

### Phase A — Additive scaffolding (no behavior change)

1. **`bridge/history.py`** — `HistoryRecord`, `project_history`, fetcher class (pure unit-testable code; no integration with ingress yet)
2. **`bridge/wire.py` + `bridge/normalizer.py`** — add `source_channel_id` (additive; nothing reads it yet)
3. **`_compat/invoke.py`** — add `history` field to `MetadataEnvelope` (additive; depends on step 1's `HistoryRecord`)
4. **`agents/definition.py`** — add `history_turns` frontmatter (additive; nothing reads it yet)
5. **Unit tests** for steps 1–4 — verify projector, fetcher, frontmatter validation
6. **Pause for review.** No behavior change at this point.

### Phase B — Wiring (history becomes load-bearing)

7. **`bridge/ingress.py`** — wire fetcher + projection into slash and ambient branches; accept `prefetched_history` kwarg
8. **`bridge/synthesized.py`** — read envelope history, forward to `handle`
9. **`bridge/gateway.py`** — construct fetcher in `_on_ready`; inject via `set_fetcher`
10. **`router/definition.py`** — read `CALFKIT_ROUTER_HISTORY_TURNS` env
11. **Integration tests** for steps 7–10
12. **`docs/ambient-routing.md`** — operator runbook update (Read Message History permission)
13. **Manual smoke test** in dev Discord — invoke an agent, post a follow-up, verify it sees prior context

### Phase C — Final code review

14. **Run `/pr-review-toolkit:review-pr`** on the complete diff — comprehensive multi-agent pass over all changes from steps 1–13 for functional issues, bugs, anti-patterns, missed edge cases, and adherence to project conventions. Address every actionable finding before declaring the feature done.

### Parallelization (optional)

Per project convention (CLAUDE.md), sub-agents are spawned with **opus model + xhigh thinking effort**. Useful parallelization opportunities:

| Stream A (independent) | Stream B (independent) | Stream C (independent) |
|---|---|---|
| Step 1: `bridge/history.py` (largest piece) | Step 2: `bridge/wire.py` + `bridge/normalizer.py` (small, isolated) | Step 4: `agents/definition.py` (one-field addition + validator + test) |

Step 3 (`_compat/invoke.py`) joins Stream A's output (depends on `HistoryRecord` symbol). Step 5 (unit tests) joins Streams A/B/C.

Phase B is tightly coupled — `ingress.py`, `synthesized.py`, and `gateway.py` share data flow and import patterns; parallelizing here usually costs more in coordination than it saves. Default to sequential for Phase B unless a specific bottleneck warrants splitting.

Step 14 (PR review) is single-threaded by definition — it operates on the assembled diff.

When delegating: each sub-agent gets a self-contained brief including the relevant sections of this plan, the specific files to touch, the exact symbols to add, and the test expectations. Sub-agents do not see the conversation that produced this plan, so the brief must be self-sufficient (per CLAUDE.md guidance).

Steps 1–6 are safe to land independently of anything else; nothing in production behavior changes until step 7.

## 15. Open questions / risk register

1. **`_clean_message_history` stability**: This plan depends on pydantic-ai's auto-merging behavior. Risk: a future pydantic-ai release changes it. Mitigation: the provider-integration test in §11.3 will catch regressions.
2. **Display-name webhook collision**: registry uses exact `by_display_name` match. If two agents share a display_name, registry construction already rejects it. Case sensitivity edge cases are pre-existing; out of scope here.
3. **Edits/deletes between fetch and consumption**: a user could edit a message after we fetched it. The agent sees the stale content. Acceptable for v1; live edit propagation is v3+.
4. **Thread archive/delete race**: `source_channel_id` points at a thread that's been archived between normalization and fetch. `fetch_channel` returns NotFound; empty history. Acceptable degradation.
5. **`source_channel_id` rolling deploy**: in-flight wires from before the deploy lack `source_channel_id`; for thread messages, fallback to `channel_id` fetches the parent channel's history (wrong). Acceptable as a transient deploy-window artifact.

---

## Self-review checklist (verified before sign-off)

- [x] No category-based filtering — naïve fetch confirmed (only empty-content drop for data hygiene)
- [x] `peel_trailing_request` removed — verified pydantic-ai auto-merges at `_agent_graph.py:1386`
- [x] In-projection merge removed — same reason
- [x] Drop-leading-ModelResponse retained — pydantic-ai merges but never drops; Anthropic requires user-first
- [x] `source_channel_id` ownership pinned — both normalizers populate
- [x] Fetcher injection via `set_fetcher` from `_on_ready` — no constructor-order conflict
- [x] `discord.Forbidden` log dedup bounded (`OrderedDict` 256 entries; matches `_seen_message_ids` pattern at `gateway.py:109`)
- [x] Fetcher cache bounded (LRU 100 entries)
- [x] `history_turns=0` short-circuits the fetch entirely (no Discord call)
- [x] Ambient eager-fetch tradeoff documented inline in `_publish_ambient`
- [x] Slash followup-id off-by-one documented (`before=followup_id` may include intervening messages — acceptable for v1)
- [x] Empty-content drop in projection (Anthropic's mapper would skip empty user_content_params anyway)
- [x] Backward compat: all new fields defaulted; rolling deploy safe
- [x] No SDK changes; no new processes; no new env vars except `CALFKIT_ROUTER_HISTORY_TURNS`
- [x] Provider-integration test guards the pydantic-ai auto-merge assumption
- [x] Reviewer's `CRITICAL` list: none. `NOTABLE` items: all addressed or explicitly accepted.
