# Discord Reply Retry With Feedback — Implementation Plan

**Status**: Awaiting approval (drafted 2026-05-23)
**Scope**: When the bridge's outbox consumer fails to post an agent's reply to Discord, surface the raw Discord error to the agent via a tagged system-reminder prompt injection so the agent can revise and retry. Chunk-split as the final fallback when retries are exhausted.
**Touches**: bridge process only.

## 1. Goals

- When an agent's reply triggers a Discord `HTTPException` (e.g. 400-50035 "Must be 2000 or fewer in length"), the bridge feeds the raw error back to the agent as a system-reminder-tagged user message and re-invokes the agent on its private inbox. The agent revises and retries.
- The retry path is **generic** across Discord error codes — the LLM sees the actual Discord error text in a `<system-reminder>...</system-reminder>` block and is trusted to adapt. No per-error-code hardcoded messages.
- Failed reply attempts are **ephemeral**: the failed text never reaches Discord, never enters channel history, and never survives the agent's run. It exists only in the retry envelope's `message_history` for the duration of the retry's LLM call.
- The retry **reuses the original `correlation_id`** so the eventual successful reply posts as an inline reply to the same Discord message — the user sees a single reply anchored to their question and has no idea retries happened.
- A **bounded retry budget** (2 retries → 3 total LLM attempts) caps cost + latency.
- **Chunk-splitting** is the unconditional final fallback so no message is ever lost entirely (the user always gets *something*, even if the agent can't comply with the constraint after retries).

## 2. Non-goals (deferred)

- Per-error-code reminder overrides (we may add a small override map later for empirical edge cases; v1 ships generic).
- Surfacing the retry attempt count to the user (UX: failures should look like normal slow replies).
- Cross-bridge retry state (bridge-local; multi-bridge deployments are out of scope today).
- Custom retry handling for A2A `private_chat` invocations — A2A is stateless RPC; failures bubble up to the calling LLM, which can adapt without our involvement.
- Per-agent retry budget tuning (one global budget; per-agent knobs are a v2+ feature if needed).

## 3. Architecture overview

```
Agent emits ReturnCall("<3000-char story>") → discord.outbox
       ↓
┌──────────── outbox @consumer (in bridge process) ──────────┐
│ tries to post via persona_sender.send(...)                 │
│   ↓                                                        │
│ discord.HTTPException raised (e.g. 400-50035)              │
│   ↓                                                        │
│ classify error:                                            │
│   - status in NON_AGENT_FIXABLE_STATUSES (403/404/...)?    │
│       → existing behavior: log WARN, drop reply, done      │
│   - retry_attempt >= MAX_RETRIES?                          │
│       → chunk-split the failed text, post N messages,      │
│         done (user gets the full content in pieces)        │
│   - else:                                                  │
│       → build_retry_reminder(error, failed_text)           │
│       → publish retry envelope to agent.{aid}.in:          │
│           user_prompt = "<system-reminder>...</...>"       │
│           message_history = original_history               │
│                             + ModelRequest(orig prompt)    │
│                             + ModelResponse(failed_text)   │
│           correlation_id = wire.event_id (SAME as orig)    │
│           deps = same wire + retry_attempt+1               │
└────────────────────────────────────────────────────────────┘
       ↓
Agent processes retry on agent.{aid}.in (existing inbox topic)
       ↓
Agent's LLM sees: (channel history) → (original question) → (failed reply)
                  → <system-reminder>your reply was rejected ... retry</...>
       ↓
Agent emits ReturnCall("<revised, shorter reply>") → discord.outbox
       ↓
Outbox tries to post — same correlation_id, same wire, same anchor
       ↓
   ✅ posted as inline reply to original user message
       OR
   ❌ fails again → check retry budget → either retry again or chunk-split
```

Three structural facts about this design:

1. **Same `correlation_id` round-trip.** The retry envelope uses `correlation_id == wire.event_id`. The agent's reply lands on `discord.outbox` with the same id. The outbox's `pending_wires.get(correlation_id)` returns the same wire. The eventual successful post is anchored to the same Discord `message_id` (the user's original question). From the user's perspective: one reply, no double-post.
2. **`agent.{aid}.in` as the retry transport.** Already-existing topic that `private_chat` uses for peer→peer invocations. Bypasses the channel-fan-out gates (we don't want every co-tenant agent to see the retry envelope).
3. **Bridge-local state for retry counter + history.** Both live in an extended `PendingWires` entry (`PendingEntry` dataclass). Bounded LRU; no new persistence.

## 4. Design decisions (locked in from earlier discussion)

| Decision | Why |
|---|---|
| Generic retry reminder, not per-error-code map | Survives new Discord error codes; modern LLMs adapt to raw Discord error text; less code to maintain |
| `<system-reminder>` tagged user message | Provider-neutral (no Anthropic SystemPromptPart hoisting issues); LLMs trained on this convention treat it as out-of-band signal |
| Retry's `message_history` keeps the failed `ModelResponse` | Truthfully reflects what happened; LLM sees its own attempt and the rejection; can revise without re-generating from scratch |
| Same `correlation_id` as original | User sees one reply, anchored to original question; no UX artifact of retries |
| Bridge-local state (PendingEntry, not Kafka deps) | Simpler; bounded by existing LRU; outbox-local concern |
| Hardcoded retry budget (2 retries) | Simple; bounded; per-agent tuning is v2 if needed |
| Status-based non-retryable filter (403/404/401/429) | These are infrastructure errors no agent revision can fix; retrying wastes LLM tokens |
| Chunk-split as final fallback | User never loses the agent's content entirely; even if the agent can't comply after retries, the message gets through (segmented) |
| No A2A retry support | A2A is stateless RPC by design (separate plan decision); LLMs can adapt at the call site |

## 5. Data model changes

### 5.1 New: `PendingEntry` dataclass

Extends what `PendingWires` stores. Carries the original wire AND the data the outbox needs to construct a retry envelope.

```python
# src/calfkit_organization/bridge/pending_wires.py

@dataclass
class PendingEntry:
    """One wire's bridge-local context, kept between publish and reply.

    Stored in :class:`PendingWires` keyed on ``correlation_id``. The
    extra fields beyond ``wire`` exist for the outbox's retry-on-
    Discord-error path: the outbox needs to rebuild an invocation
    envelope (history + temp_instructions) to send the agent back
    to its inbox for a revised reply.
    """
    wire: WireMessage
    message_history: tuple[ModelMessage, ...]
    """Snapshot of what was passed as ``message_history`` to the
    original ``invoke_node`` call. Tuple (not list) because the
    PendingWires entry is shared across multiple outbox lookups and
    we don't want one consumer mutating another's view."""
    temp_instructions: str | None
    """Snapshot of ``temp_instructions`` from the original invocation
    (the peer roster for A2A-enabled agents). Forwarded verbatim on
    each retry so the agent's tool affordances are unchanged."""
    retry_attempt: int = 0
    """How many retries have been triggered for this wire. Outbox
    reads + increments. Capped at ``MAX_REPLY_RETRY_ATTEMPTS``;
    further failures fall back to chunk-splitting."""
```

### 5.2 Modified: `PendingWires` API

```python
class PendingWires:
    def put(self, correlation_id: str, entry: PendingEntry) -> None: ...
    def get(self, correlation_id: str) -> PendingEntry | None: ...
    def pop(self, correlation_id: str) -> PendingEntry | None: ...
    def increment_retry(self, correlation_id: str) -> int | None:
        """Increment the retry counter for an existing entry and return
        the new value. Returns None if the entry has been evicted. Used
        by the outbox to atomically claim a retry attempt."""
    def __len__(self) -> int: ...
```

`put` now accepts a `PendingEntry` instead of a bare `WireMessage`. Callers (only `BridgeIngress.handle`) wrap the wire + history + temp_instructions into an entry. The outbox reads the entry; never constructs one.

### 5.3 Migration concerns

- **Callers of `pending_wires.get`** currently expect `WireMessage`. They now get `PendingEntry`; the outbox needs to access `.wire` to keep its existing behavior. Two call sites: `bridge/outbox.py` (the consumer's closure) and a `_resolve_temp_instructions` test mock. Update both.
- **Existing tests** instantiate `PendingWires()` and call `put(correlation_id, wire)`. Update fixtures to construct a `PendingEntry`. ~6 test files affected.
- **Backward compat at the Kafka layer**: PendingEntry lives only in-process. No on-wire schema change.

## 6. New module-level code (in `bridge/outbox.py`)

### 6.1 Retry-policy constants

```python
NON_AGENT_FIXABLE_STATUSES: frozenset[int] = frozenset({
    401,   # unauthorized — bot token invalid
    403,   # forbidden — missing Manage Webhooks / View Channel
    404,   # not found — channel/webhook deleted
    429,   # rate limited — discord.py already retried; agent can't fix
})
"""Discord HTTP statuses where retrying the agent with a revised reply
cannot possibly succeed. These are infrastructure / permission errors
that require operator action, not agent content adjustment. Skip the
LLM retry; fall back to the existing log-and-drop behavior."""

MAX_REPLY_RETRY_ATTEMPTS: int = 2
"""Number of LLM retries after the original failure. Total LLM attempts
= 1 (original) + MAX_REPLY_RETRY_ATTEMPTS. Picked at 2 because:

- Discord 400 errors are usually self-correcting on attempt 2 (the LLM
  shortens / fixes formatting once told).
- Each retry is a full LLM round-trip (~5-15s); 2 retries adds at most
  ~30s before chunked fallback. 3+ retries makes the user wait too long
  with no signal.
- LLM cost: 3 attempts is bounded; pathological retry loops cost ~3x
  a normal invocation, not unbounded."""

CHUNK_SAFE_SIZE: int = 1990
"""Max chars per chunk in the chunk-split fallback. Discord's hard limit
is 2000; 1990 leaves a 10-char safety buffer for emoji/encoding
overhead that occasionally tips a 1999-char string over the limit."""
```

### 6.2 `build_retry_reminder` — pure function

```python
def build_retry_reminder(
    error: discord.HTTPException,
    failed_text: str,
) -> str:
    """Build the system-reminder-tagged user message for an agent retry.

    Generic by design: the LLM sees the literal Discord error text in
    a ``<system-reminder>`` block and adapts. Modern frontier LLMs
    reliably parse Discord's own error strings (e.g. "Must be 2000 or
    fewer in length", "Cannot send an empty message", "Invalid embed
    URL") and adjust their next reply accordingly. No per-error-code
    customization is needed; the override map slot at
    :data:`_RETRY_REMINDER_OVERRIDES` exists only for future empirical
    cases where the generic message proves insufficient.

    The ``<system-reminder>`` tag pattern is a convention many frontier
    models are trained on (notably Claude — see how Claude Code surfaces
    out-of-band reminders): it marks the message as system-level
    metadata even though it occupies a ``user``-role slot on the wire.
    LLMs respecting the convention do not echo the content back to the
    user.
    """
    # Per-code override lookup (empty by default; populate only on
    # empirical evidence the generic message isn't sufficient).
    override = _RETRY_REMINDER_OVERRIDES.get((error.status, error.code))
    if override is not None:
        body = override
    else:
        # Surface the raw Discord error to the LLM. ``error.text`` is
        # Discord's JSON error body when available; ``str(error)``
        # falls back to the formatted message.
        raw = error.text or str(error)
        body = (
            f"Your previous reply (length: {len(failed_text)} chars) was "
            f"rejected by Discord. The exact error:\n\n"
            f"  HTTP {error.status}: {raw}\n\n"
            f"Please respond again to the user's original question, "
            f"addressing the specific issue above. For example, if the "
            f"content was too long, be more concise; if it contained "
            f"banned formatting, rephrase without it."
        )
    return (
        "<system-reminder>\n"
        f"{body}\n\n"
        "This reminder is system-level — the user does NOT see it. "
        "Do NOT mention this error or that you are retrying.\n"
        "</system-reminder>"
    )


_RETRY_REMINDER_OVERRIDES: dict[tuple[int, int | None], str] = {}
"""Per-(status, code) message overrides. Populated only on empirical
evidence that the generic reminder fails to elicit the right fix from
the LLM for a specific Discord error. Empty by default. Format:

    (400, 50035): "Custom reminder for content-too-long that the LLM
                   responds better to than the generic version."

Keys with ``code=None`` match any error of that status."""
```

### 6.3 `_chunk_split` — pure function

```python
def _chunk_split(text: str, *, max_chars: int = CHUNK_SAFE_SIZE) -> list[str]:
    """Split ``text`` into pieces each ≤ ``max_chars`` for posting as
    consecutive Discord messages.

    Splits at the nicest available boundary: paragraph (``\\n\\n``) →
    line (``\\n``) → sentence (``. ``) → word (``  ``) → hard cut.
    Each chunk is rstripped of trailing whitespace. The boundary
    search refuses to split earlier than half the max so we don't
    produce tiny first chunks followed by huge tails.

    Returns a list of chunks in original order. If ``text`` already
    fits, returns ``[text]``.
    """
```

Pure / no I/O. Easily unit-tested.

### 6.4 `_publish_retry` — async helper

```python
async def _publish_retry(
    client: Client,
    pending: PendingEntry,
    agent_id: str,
    failed_text: str,
    error: discord.HTTPException,
) -> None:
    """Publish a retry envelope to ``agent.{aid}.in`` with the system-
    reminder prompt injection.

    The retry's ``message_history`` is the original projected history,
    plus the original user prompt (now as a ``ModelRequest``), plus
    the failed reply (as a ``ModelResponse`` so the LLM sees its own
    attempt and the rejection context). The retry's ``user_prompt``
    is the system-reminder-tagged feedback.

    Reuses ``correlation_id = wire.event_id`` so the eventual
    successful reply lands on the same outbox lookup and posts as an
    inline reply to the original Discord message.
    """
    reminder = build_retry_reminder(error, failed_text)
    retry_history: list[ModelMessage] = [
        *pending.message_history,
        ModelRequest(parts=[UserPromptPart(content=pending.wire.content)]),
        ModelResponse(parts=[TextPart(content=failed_text)]),
    ]

    handle = await client.invoke_node(
        user_prompt=reminder,
        topic=_AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id=agent_id),
        correlation_id=pending.wire.event_id,
        deps={"discord": pending.wire.model_dump(mode="json")},
        output_type=str,
        temp_instructions=pending.temp_instructions,
        message_history=retry_history,
    )
    handle._future.cancel()
```

Note `deps` deliberately *does not* include `phonebook` — the agent doesn't need to re-discover peers for a retry; the original `temp_instructions` already encodes the peer roster from the original invocation.

Wait — actually we should include phonebook since `private_chat` reads from `deps["phonebook"]`. Let me reconsider. **Decision**: include `phonebook` in retry deps. The outbox doesn't have a phonebook though — we'd need to rebuild it from the registry. The outbox already has the registry. Tiny overhead.

```python
deps={
    "discord": pending.wire.model_dump(mode="json"),
    "phonebook": phonebook_to_deps(phonebook_from_registry(self._registry)),
},
```

### 6.5 `_post_chunked_fallback` — async helper

```python
async def _post_chunked_fallback(
    persona_sender: DiscordPersonaSender,
    persona: Persona,
    wire: WireMessage,
    text: str,
) -> None:
    """Final fallback when retries are exhausted: split the latest
    failed text into ≤2000-char chunks and post each as the same
    persona. The first chunk is anchored as an inline reply to the
    original user message; subsequent chunks are bare continuations.
    Logs WARN on entry (operator signal that retries were exhausted).
    """
```

Each chunk goes through `persona_sender.send(persona, channel_id, content, reply_to=...)`. Only chunk 1 uses `reply_to=ReplyContext.from_wire(wire)`. Chunks 2+ have `reply_to=None`.

## 7. Modified outbox flow

The current outbox `_send_with_one_retry_on_outage` catches `HTTPException` generically. New flow inside the outbox consumer's closure:

```python
async def _post_reply(result: NodeResult[str]) -> None:
    pending = pending_wires.get(result.correlation_id)
    if pending is None:
        # ... existing DEBUG skip
        return

    wire = pending.wire

    if result.emitter_node_kind != "agent" or not result.emitter_node_id:
        # ... existing WARNING
        return

    spec = registry.by_id(result.emitter_node_id)
    if spec is None:
        # ... existing WARNING
        return

    text = (result.output or "").strip()
    if not text:
        # ... existing INFO + skip
        return

    persona = Persona(name=spec.display_name, avatar_url=spec.avatar_url)
    try:
        sent = await persona_sender.send(
            persona=persona,
            channel_id=wire.channel_id,
            content=text,
            reply_to=ReplyContext.from_wire(wire),
        )
        if pending.retry_attempt > 0:
            logger.info(
                "agent retry succeeded after %d attempt(s) event_id=%s agent=%s",
                pending.retry_attempt, wire.event_id, spec.agent_id,
            )
        else:
            logger.info(
                "posted reply event_id=%s agent=%s reply_id=%s channel=%s",
                wire.event_id, spec.agent_id, sent.id, wire.channel_id,
            )
        return
    except discord.NotFound as e:
        # ... existing handling
        return
    except discord.Forbidden as e:
        # ... existing handling
        return
    except discord.HTTPException as e:
        # New retry-with-feedback path.
        await _handle_post_failure(
            error=e,
            pending=pending,
            agent_id=spec.agent_id,
            persona=persona,
            failed_text=text,
            client=client,
            persona_sender=persona_sender,
            pending_wires=pending_wires,
        )
```

Where `_handle_post_failure`:

```python
async def _handle_post_failure(
    *,
    error: discord.HTTPException,
    pending: PendingEntry,
    agent_id: str,
    persona: Persona,
    failed_text: str,
    client: Client,
    persona_sender: DiscordPersonaSender,
    pending_wires: PendingWires,
) -> None:
    wire = pending.wire

    if error.status in NON_AGENT_FIXABLE_STATUSES:
        logger.warning(
            "outbox post failed (not retryable) channel_id=%s "
            "event_id=%s agent=%s status=%s: %s",
            wire.channel_id, wire.event_id, agent_id, error.status, error,
        )
        return

    if pending.retry_attempt >= MAX_REPLY_RETRY_ATTEMPTS:
        logger.warning(
            "retry budget exhausted (attempt=%d max=%d); chunk-splitting "
            "reply event_id=%s agent=%s status=%s",
            pending.retry_attempt, MAX_REPLY_RETRY_ATTEMPTS,
            wire.event_id, agent_id, error.status,
        )
        await _post_chunked_fallback(persona_sender, persona, wire, failed_text)
        return

    # Claim a retry attempt (atomic counter increment).
    new_attempt = pending_wires.increment_retry(wire.event_id)
    if new_attempt is None:
        # Entry was evicted between get and increment. Fall back to
        # chunk-split rather than dropping silently.
        logger.warning(
            "pending entry evicted before retry could be claimed; "
            "chunk-splitting event_id=%s agent=%s",
            wire.event_id, agent_id,
        )
        await _post_chunked_fallback(persona_sender, persona, wire, failed_text)
        return

    logger.info(
        "outbox post failed; triggering agent retry attempt=%d "
        "channel_id=%s event_id=%s agent=%s status=%s: %s",
        new_attempt, wire.channel_id, wire.event_id, agent_id,
        error.status, error,
    )
    await _publish_retry(client, pending, agent_id, failed_text, error)
```

The current `_send_with_one_retry_on_outage` is retained for its specific purpose (one extra retry for `DiscordServerError` 5xx — discord.py's internal budget exhausted). It remains the inner function; `_handle_post_failure` is invoked when even that final retry fails.

Actually, reconsidering: the existing `_send_with_one_retry_on_outage` swallows `discord.DiscordServerError` (5xx) for one extra attempt internally, then on persistent failure logs WARN and returns None. In the new flow, persistent 5xx should also trigger the retry-with-feedback path (the LLM might produce something that doesn't trip whatever's making Discord 5xx). So restructure: drop `_send_with_one_retry_on_outage`, do the persona_sender.send() call directly in `_post_reply`, and route ALL HTTPException through `_handle_post_failure`. The existing one-shot 5xx retry was a different concern; we can keep its short delay for 5xx by adding `429` and `5xx` to non-retryable (since they're transient infrastructure issues — agent retrying doesn't help on a Discord-side problem) OR by adding a transient-error category to the new policy.

**Cleaner**: 5xx is in `NON_AGENT_FIXABLE_STATUSES`. The agent's retry doesn't help when Discord itself is down. The existing one-retry-on-5xx is a separate concern (transient outage smoothing); restore it as a `_with_5xx_smoothing` wrapper around `persona_sender.send` if we want to preserve that behavior. Actually, let me leave the existing one-retry-on-5xx behavior in place — it's a separate concern from agent retries.

**Final decision**: keep `_send_with_one_retry_on_outage` intact for its 5xx-smoothing purpose. Add `_handle_post_failure` after it returns None on `HTTPException`. The status-code check distinguishes which path.

## 8. File-by-file changes

| File | Change |
|---|---|
| `src/calfkit_organization/bridge/pending_wires.py` | Introduce `PendingEntry` dataclass; `put`/`get`/`pop` take/return `PendingEntry`; new `increment_retry` method |
| `src/calfkit_organization/bridge/outbox.py` | Add `NON_AGENT_FIXABLE_STATUSES`, `MAX_REPLY_RETRY_ATTEMPTS`, `CHUNK_SAFE_SIZE`, `_RETRY_REMINDER_OVERRIDES`; new functions `build_retry_reminder`, `_chunk_split`, `_publish_retry`, `_post_chunked_fallback`, `_handle_post_failure`; modify `build_outbox_consumer` to accept `calfkit_client`; modify `_post_reply` closure to route HTTPException to `_handle_post_failure` |
| `src/calfkit_organization/bridge/ingress.py` | Slash branch: construct `PendingEntry(wire, message_history=tuple(message_history), temp_instructions=temp_instructions, retry_attempt=0)` and pass to `pending_wires.put`; same for the synthesized-slash path's underlying call |
| `src/calfkit_organization/bridge/gateway.py` | Pass `calfkit_client` into `build_outbox_consumer(...)` |
| `tests/bridge/test_pending_wires.py` | Update fixtures to construct `PendingEntry`; new tests for `increment_retry` semantics + entry-evicted-during-retry edge case |
| `tests/bridge/test_outbox.py` | New tests: 400-50035 triggers retry; 403 does not; retry envelope shape (history + ModelRequest + ModelResponse + reminder); retry counter increments; budget exhausted → chunk-split; chunk-split posts N messages with right content + only first uses reply_to |
| `tests/bridge/test_ingress.py`, `test_ingress_history.py`, `test_ingress_router.py` | Update existing assertions on `pending_wires.get(...)` to expect `PendingEntry` and access `.wire` |
| `tests/bridge/test_outbox_retry.py` (NEW) | Focused test file for the retry-with-feedback feature |
| `docs/ambient-routing.md` | Add an "Outbox retry behavior" subsection under "Operating" |

No SDK changes. No new processes. No new env vars (constants are hardcoded; per-deployment tuning can come later if needed).

## 9. Configuration

| Setting | Default | Override |
|---|---|---|
| Max LLM retries per failed post | 2 | Hardcoded `MAX_REPLY_RETRY_ATTEMPTS` (v2 could expose as env) |
| Chunk safe size (chars) | 1990 | Hardcoded `CHUNK_SAFE_SIZE` |
| Non-retryable statuses | `{401, 403, 404, 429}` | Hardcoded `NON_AGENT_FIXABLE_STATUSES` |
| Per-error-code reminder override | empty | `_RETRY_REMINDER_OVERRIDES` map (populated only on empirical need) |

## 10. Error handling matrix

| Failure | Detection | Behavior |
|---|---|---|
| 400-50035 (content too long) | `e.status == 400 and code == 50035` | Build reminder, retry agent; chunk-split if budget exhausted |
| 400-50006 (empty content) | `e.status == 400 and code == 50006` | Same — generic reminder shows the error text |
| Other 400-XXXXX | `e.status == 400` | Same — generic reminder |
| 401 (unauthorized) | `e.status == 401` | NON_AGENT_FIXABLE; WARN log + drop (existing behavior) |
| 403 (forbidden) | `e.status == 403` | NON_AGENT_FIXABLE; WARN log + drop (existing behavior) |
| 404 (channel/webhook gone) | `e.status == 404` | NON_AGENT_FIXABLE; WARN log + drop (existing behavior) |
| 429 (rate limited) | `e.status == 429` | NON_AGENT_FIXABLE; discord.py already retried internally; agent retry can't help with rate limits |
| 5xx (Discord server error) | `e.status >= 500` | Existing one-shot retry-with-delay (`_send_with_one_retry_on_outage`) handles this; on persistent failure NON_AGENT_FIXABLE branch runs (5xx not in the set today — discuss in §13) |
| Retry envelope publish fails (Kafka error) | `await client.invoke_node` raises | Fall back to chunk-split immediately; log WARN |
| `PendingEntry` evicted between original failure and retry | `pending_wires.increment_retry` returns None | Fall back to chunk-split; log WARN |
| Chunk-split itself fails (Discord 5xx mid-chunk) | inner `persona_sender.send` raises | Existing `_send_with_one_retry_on_outage` already smooths transient 5xx; persistent 5xx → log WARN, give up |

## 11. Test plan

### 11.1 Unit tests — `tests/bridge/test_outbox_retry.py` (NEW)

**`build_retry_reminder`**:
- Includes the raw Discord error text (status + body)
- Wraps in `<system-reminder>` tags
- Contains "Do NOT mention this error" instruction
- Length-error case shows the length in chars
- Empty-content case includes "empty"
- Unknown error code uses generic template (override map miss)
- Override entry returns its custom text

**`_chunk_split`**:
- Short text → `[text]`
- Long text splits at `\n\n` preferentially
- Falls back to `\n`, `. `, ` `, hard cut as boundaries get sparser
- Refuses to split earlier than `max_chars / 2`
- Each chunk ≤ `CHUNK_SAFE_SIZE`
- Preserves all content (no characters lost across chunks)
- Whitespace-trimmed chunk boundaries

**`_handle_post_failure` (with mocked client + persona_sender)**:
- 403 status → no retry, no chunk-split, only WARN log
- 400-50035 with retry_attempt=0 → retry published to `agent.{aid}.in`
- 400-50035 with retry_attempt=MAX → chunk-split fired instead
- `increment_retry` returns None → chunk-split fired
- After retry published, deps + correlation_id + history are correctly constructed
- History contains: original_history + ModelRequest(orig prompt) + ModelResponse(failed_text)
- The retry's user_prompt is the system-reminder string

**`_post_chunked_fallback`**:
- Long text → N persona_sender.send calls
- First chunk has `reply_to` set to original wire
- Subsequent chunks have `reply_to=None`
- All chunks under 2000 chars
- Logs WARN on entry

### 11.2 Integration tests — `tests/bridge/test_outbox.py` (extend existing)

- Full end-to-end: outbox receives a NodeResult, persona_sender raises 400-50035, retry envelope is published via client.invoke_node, PendingEntry's retry_attempt is incremented
- Successful retry: after retry publishes, simulate the new NodeResult arriving; persona_sender succeeds; log says "agent retry succeeded after 1 attempt(s)"
- Two-retry path: original fails → retry 1 fails → retry 2 fails → chunk-split fallback
- Same correlation_id throughout the retry chain (verify by capturing client.invoke_node call kwargs)

### 11.3 PendingWires tests — `tests/bridge/test_pending_wires.py` (extend)

- `PendingEntry` round-trip through `put/get/pop`
- `increment_retry` returns new value
- `increment_retry` on missing key returns None
- `increment_retry` after `pop` returns None
- LRU eviction still works with `PendingEntry`

### 11.4 Regression — existing tests

- Every test that calls `pending_wires.put(id, wire)` needs to be updated to construct a `PendingEntry`. Helper fixture in conftest:
  ```python
  def make_pending(wire, history=None, temp_instructions=None) -> PendingEntry:
      return PendingEntry(
          wire=wire,
          message_history=tuple(history or ()),
          temp_instructions=temp_instructions,
          retry_attempt=0,
      )
  ```

## 12. Performance / cost considerations

| Metric | Estimate |
|---|---|
| Latency on a length-error retry path | +5–15s per retry × up to 2 retries = +10–30s before chunked fallback |
| LLM cost on retry | ~1x original invocation per retry (message_history is similar length; reminder is small) |
| Token overhead from `<system-reminder>` wrapper | ~30 tokens per retry |
| Kafka publish overhead | One extra envelope per retry on `agent.{aid}.in` |
| Bridge memory | One PendingEntry per in-flight wire; ~1KB extra per entry (history is the bulk); bounded by LRU (1024 entries default) → ~1MB worst case |

The user-visible behavior is: a slow reply (10–30s instead of 5–15s) when the first attempt fails. Successful retries appear identical to a normal slow reply.

## 13. Open questions for confirmation

1. **5xx category**: Should 5xx errors be in `NON_AGENT_FIXABLE_STATUSES`? The existing `_send_with_one_retry_on_outage` already smooths a single 5xx retry with a short delay. If 5xx persists, treating it as non-agent-fixable (log + drop) matches the existing behavior. But arguably the LLM could try a different shape and Discord's CDN might serve a different backend on retry. **Recommendation**: include 5xx in NON_AGENT_FIXABLE. The agent can't influence whether Discord's cluster is healthy.

2. **Chunk-split for 5xx final failure**: When `_send_with_one_retry_on_outage` exhausts its budget and we're in the NON_AGENT_FIXABLE branch, do we still chunk-split? Today we'd just drop. **Recommendation**: for 5xx, chunk-split won't help (Discord is down); for 403/404, chunk-split also won't help (the channel is gone). Keep existing log+drop behavior for non-retryable. Chunk-split only applies after exhausting LLM retries on potentially-fixable errors.

3. **Phonebook in retry deps**: Include it (rebuild via `phonebook_from_registry`) so peer-roster-dependent tools like `private_chat` still work on retry. **Recommendation**: include it.

4. **Outbox needs registry?**: It already does (for `spec = registry.by_id(emitter)`). Reusing that for `phonebook_from_registry(self._registry)` is free.

5. **Should the retry have a timeout?**: Currently fire-and-forget (`handle._future.cancel()`). The retry's eventual reply arrives whenever the agent finishes. No timeout. **Recommendation**: no timeout in v1 — match existing fire-and-forget pattern.

## 14. Implementation order

### Phase A — data model + pure functions (no behavior change)

1. **`bridge/pending_wires.py`** — introduce `PendingEntry` dataclass; rewrite `PendingWires` to store entries; add `increment_retry`
2. **`bridge/outbox.py`** — add `build_retry_reminder`, `_chunk_split`, constants (`NON_AGENT_FIXABLE_STATUSES`, `MAX_REPLY_RETRY_ATTEMPTS`, `CHUNK_SAFE_SIZE`, `_RETRY_REMINDER_OVERRIDES`) as module-level pure additions
3. **Update callers of `pending_wires.put`** in `bridge/ingress.py` to construct `PendingEntry` with `message_history` and `temp_instructions`
4. **Update test fixtures** for PendingEntry shape
5. **Unit tests** for `PendingEntry`, `build_retry_reminder`, `_chunk_split`, `increment_retry`

### Phase B — wiring (retry path goes live)

6. **`bridge/outbox.py`** — implement `_handle_post_failure`, `_publish_retry`, `_post_chunked_fallback`; modify `_post_reply` closure to invoke them
7. **`bridge/outbox.py:build_outbox_consumer`** — accept `calfkit_client: Client`
8. **`bridge/gateway.py`** — pass `calfkit_client` to `build_outbox_consumer`
9. **Integration tests** for the full retry → success path, retry → exhausted → chunk-split path, 403 → no retry path

### Phase C — operator runbook + review

10. **`docs/ambient-routing.md`** — add "Outbox retry behavior" subsection explaining the silent-retry-then-chunk-fallback pattern, the log lines operators should watch, and how to investigate when an agent's reply genuinely never appears
11. **Run `/pr-review-toolkit:review-pr`** on the full diff
12. **Address findings**

## 15. Self-review checklist (verified before sign-off)

- [ ] `PendingEntry` is a frozen-ish dataclass (mutate `retry_attempt` in place; never mutate `wire`/`history`/`temp_instructions`)
- [ ] `build_retry_reminder` does NOT include the failed text inside the reminder (we already place it as a `ModelResponse` in history; including it twice doubles tokens and risks the LLM refining the wrong copy)
- [ ] System-reminder content is exactly one stable block — no per-call dynamic substitutions beyond `error.text` and `len(failed_text)`
- [ ] Retry's `correlation_id` is the original `wire.event_id` (preserves Discord inline-reply anchor)
- [ ] `_handle_post_failure` is async + does NOT raise (any internal failure → fall through to chunk-split or just log)
- [ ] `_post_chunked_fallback` posts at least once even if every chunk send fails (each chunk failure is logged independently — partial delivery is better than silent drop)
- [ ] Chunk-split preserves total content (no characters dropped between chunks)
- [ ] Logging at the right level: INFO for "retry triggered" + "retry succeeded"; WARN for "budget exhausted" + "non-retryable error" + "pending evicted"; ERROR for nothing in the retry path (no path is operator-error-grade)
- [ ] Existing tests still pass after `PendingWires` migration to `PendingEntry`
- [ ] No SDK changes; no new env vars; no new processes; no new Kafka topics
- [ ] Operator runbook updated with the new log lines to watch + interpretation

---

## What stays out of v1

- **Per-error-code message overrides**: empty `_RETRY_REMINDER_OVERRIDES` map ships; populate only when empirical evidence demands it.
- **Per-agent retry budget**: one global `MAX_REPLY_RETRY_ATTEMPTS=2`. Add per-agent override when a real use case shows up.
- **Configurable chunk size**: `CHUNK_SAFE_SIZE=1990` hardcoded. Discord's limit is unlikely to change.
- **Cross-process retry state**: bridge-local only.
- **Retry observability beyond logs**: no metrics emit; v2 if needed.
