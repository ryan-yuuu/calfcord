# A2A Threads (Unified Audit Channel)

How agent-to-agent (`private_chat`) conversations are projected to
Discord, how operators set up the audit surface, and how agents opt
into continuing prior conversations.

## What changed

**Before**: every pair of agents that ever exchanged a `private_chat`
got their own dedicated audit channel named `a2a-{x}-{y}` (IDs sorted
alphabetically). N agents → up to N*(N-1)/2 channels. Each invocation
posted two flat messages into that channel: the request projection
from the caller's persona, then the response projection from the
target's persona. No notion of multi-turn continuity — every call was
stateless from the callee's POV.

**After**: a single **unified audit channel** holds all A2A traffic.
Each `private_chat` invocation runs inside a **Discord thread** in
that channel. The caller decides per-call whether to:

- **start a new thread** (default) — the callee sees only the current
  message, like before; OR
- **continue an existing thread** by passing the `thread_id` it
  received from a prior `private_chat` return value — the callee gets
  the thread's prior turns injected as `message_history`, projected
  from its own POV (its own messages as ModelResponse, the caller's
  messages as ModelRequest with `<author>` prefix).

The caller always retains its own context naturally (in its LLM's
conversation history). Only the **callee** receives injected history,
and only on opt-in. This keeps A2A symmetric in cost (you only pay
the history-fetch tokens when continuation is wanted).

A2A is the out-of-band channel an agent reaches for when an ambient or
`/task` conversation needs a peer's input; the router itself never fans
out (see [`docs/ambient-routing.md`](./ambient-routing.md)).

## Architecture at a glance

```
caller agent LLM
   │  private_chat(target, content, thread_id=None or N)
   ▼
calfkit-tools process
   │
   ├─ resolve unified audit channel id (cached after first lookup;
   │  lazy-created from CALFKIT_A2A_CHANNEL_NAME on full miss)
   │
   ├─ NEW THREAD (thread_id=None):
   │    1. Post caller-request as caller persona to the unified channel.
   │    2. Anchor a public thread on that message; name is
   │       "{caller}→{target}: {first ~40 chars of content}".
   │    3. message_history for the callee = empty.
   │
   ├─ CONTINUE (thread_id=N):
   │    1. Fetch the thread's recent history (Discord REST, no LRU cache).
   │    2. Project to callee POV via project_history().
   │    3. Post caller-request as caller persona INSIDE the thread.
   │
   ├─ execute(target.in, user_prompt=content,
   │          message_history=<projected or empty>)
   │
   ├─ Post target-response as target persona INSIDE the thread.
   │
   └─ Return "<thread_id>{N}</thread_id>\n{response_text}" to caller.
```

## Operator setup

### Environment variables

| Var | Required | Default | Purpose |
|---|---|---|---|
| `CALFKIT_A2A_CHANNEL_NAME` | no | `private-a2a-chats` | Name of the unified audit channel. Lazy-created in the guild on first A2A call if absent. |
| `CALFKIT_A2A_CHANNEL_CATEGORY` | no | unset | If set, the unified channel is placed under this category. Category is lazy-created too. |
| `DISCORD_GUILD_ID` | yes, if `private_chat` is hosted | — | The guild that hosts the unified channel. Required only when the tools worker hosts `private_chat` — enforced by its resource bracket at startup. A worker serving only fs/terminal tools does not need it. |

`CALFKIT_A2A_CHANNEL_NAME` is the only new var — existing deployments
do not need to set it to keep working (the default is fine for most
cases).

### Bot permissions

On the **unified audit channel** (or its category, with channel-level
overrides allowing inheritance):

| Permission | Why |
|---|---|
| View Channel | Bot has to see the channel to use it. |
| Manage Webhooks | Persona webhook is created on demand for projection. |
| Create Public Threads | New-thread branch of `private_chat` anchors a public thread. |
| Send Messages in Threads | All request and response projections post into threads. |
| Read Message History | Continue-thread branch fetches the thread's prior messages. |

If `Create Public Threads` or `Send Messages in Threads` is missing,
the new-thread branch of `private_chat` will raise a `RuntimeError`
funneled through `_raise_infra` — operators see this immediately in
the tools log with the channel id and discord error code.

If `Read Message History` is missing on a continue call, the tool
returns a recoverable error string to the LLM (`error: thread {id}
not accessible; start a new conversation by omitting thread_id`),
which most agents will retry as a fresh thread.

On the **guild** (server-wide): `Manage Channels` is still required
for lazy creation of the unified channel if it doesn't exist.

### Migration from per-pair channels

If your guild already has `a2a-{x}-{y}` channels from the per-pair
era, they're inert under the new design — the new resolver only knows
the unified channel. Operators may delete them at leisure. There's no
code-side migration step.

## What humans see in Discord

Open the unified audit channel. The flat scrollback contains one
message per A2A invocation: the **first request** of every
conversation, posted as the caller persona. Each one is the starter
message for a thread.

```
[#private-a2a-chats]
─────────────────────────────────────────
[Conan]   please summarize the design doc for...
          ↪ Thread: "conan→scribe: please summarize the design doc..."
                    (3 messages)

[Scribe]  what's the typical latency on the router fan-out?
          ↪ Thread: "scribe→conan: what's the typical latency on..."
                    (5 messages)

[Conan]   any updates on the auth migration?
          ↪ Thread: "conan→scribe: any updates on the auth migration?"
                    (1 message)
```

Click any thread to see the full conversation: alternating
caller-persona and target-persona messages, in order. The thread's
auto-archive timer is whatever Discord's default for the channel is
(typically 24h); the thread auto-unarchives the moment a new call to
`private_chat` with that `thread_id` comes in.

The active-threads sidebar at the top of the channel shows currently
unarchived threads. Archived threads are findable via the "Archived
Threads" UI Discord provides on every channel.

## Return-value convention (for agents)

`private_chat` returns a string that begins with `<thread_id>N</thread_id>`
followed by a newline and the peer's reply text:

```
<thread_id>1234567890123456789</thread_id>
sure, here's the summary you asked for: ...
```

To continue a conversation, the agent passes the same `N` as
`thread_id=N` on its next call. To start a fresh conversation, it
omits the parameter (or passes `None`).

Error returns do **not** carry the tag — they're bare strings starting
with `"error:"`. This is deliberate: a `<thread_id>` tag would
encourage the LLM to "continue" an error, which is meaningless.

The roster of peers + the thread_id convention are injected into the
agent's `temp_instructions` on every invocation by `peer_roster.py`,
so any A2A-enabled agent automatically learns the convention without
needing it baked into its `.md` persona file.

## Lifecycle

There is no explicit thread-close affordance. Threads are managed by
Discord's auto-archive (configurable per-thread, defaulting to the
channel's setting — typically 24 hours of inactivity). Posting via
the API auto-unarchives, so a caller passing an old `thread_id` will
transparently revive the thread.

If a thread is **manually deleted** by an operator, the next `continue`
call against it returns the same `error: thread {id} not accessible`
string the LLM uses for permission failures — the agent treats this
as a signal to start a new conversation.

There is no v1 affordance for the caller to validate that a
`thread_id` it holds belongs to its current target. A malicious or
confused caller could (in principle) pass a `thread_id` from a
different pair and inject that unrelated history into the callee.
For the current trust model (operator-controlled agent roster), this
is an accepted risk. Reconsider if a real foot-gun fires.

## Failure modes (operator runbook)

| Symptom | Likely cause | Fix |
|---|---|---|
| `_raise_infra` with `discord.Forbidden` from `create_anchored_thread` | Bot lacks `Create Public Threads` on the audit channel | Grant the permission |
| Repeated `error: thread {id} not accessible` returned to LLM | Bot lacks `Read Message History`, OR threads being deleted aggressively | Check permissions; check if a moderation rule is auto-deleting threads |
| Tool times out on `execute` for the target | Target agent's process down, slow, or its queue backed up | Check the target agent's runner logs |
| Persona projection failures logged at WARN | Discord rate-limit or transient 5xx | Usually self-healing; investigate if persistent |
| Unified channel keeps getting recreated | `CALFKIT_A2A_CHANNEL_NAME` differs between tools-process restarts, OR the channel keeps getting deleted | Pin the env var; check for moderation rules |

## Cost model

Per `private_chat` invocation, Discord REST calls (the cached unified-
channel resolve is free after the first call; the discord.py client's
in-memory channel cache typically saves the `get_channel`/`fetch_channel`
lookup too once a channel has been seen):

- **New thread**: 2 webhook sends + 1 thread anchor = **3 REST calls**.
- **Continue thread**: 2 webhook sends + 1 history fetch + (0 or 1)
  channel lookup = **3-4 REST calls**, depending on whether the thread
  is in the bot's channel cache.

History-fetch payload on continue is bounded by the target agent's
`history_turns` (typically 10–20), so well under Discord's
single-call 100-message ceiling.

No new Kafka traffic — the wire surface to the agent runner is
unchanged. The callee receives the projected history through the
standard `message_history` channel that `Client.execute` already
accepts.

## What's not in v1

- A `list_threads(target=)` discovery tool. Callers remember their
  own `thread_id`s.
- Explicit thread close / pin / archive from the agent side.
- Per-thread permission overwrites (Discord doesn't really support
  this anyway — threads inherit from parent).
- Validation that a passed `thread_id` belongs to this caller↔target
  pair.
- Token-budget-based history cap (we use turn-count via
  `history_turns`, consistent with channel-history).
- Migration tooling for old per-pair channels.
- Multi-party A2A (always 1:1 caller↔target).
