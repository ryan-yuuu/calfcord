# Ambient Routing

How ambient (non-slash, non-mention) Discord messages reach the right
agents, and why the system needs a fourth process — `calfkit-router` —
to make that work.

## Motivation

Before this feature: every assistant agent subscribed to
`discord.channel.{cid}.in` and the `addressed_to_me_gate` accepted
`kind="message"` envelopes from humans. Result: every co-tenant agent
in the channel ran its LLM on every ambient message and posted a reply.
"All agents talk at once" — wrong dynamic for a groupchat.

After this feature: a built-in routing agent sits in front of all
assistant agents. It receives every ambient message and answers a
single question — "who is the user talking to?" — picking exactly
one addressee. The chosen agent replies normally. If that agent
needs input from peers, it can pull them in out-of-band via its
`private_chat` tool; the router itself never fans out. Slash and
@-mention invocations are unaffected — they continue to route
directly to the targeted agent.

The "exactly one" policy is enforced two ways: at the schema level
(`RoutingDecision.agent_id` is a single `str | None`, so multi-agent
fan-out is impossible at the type boundary) and at the prompt level
(see `router/prompt.py`). The schema's `agent_id=None` case is
defense in depth — a misbehaving LLM that emits a tool call with
no `agent_id` falls through to the fan-out consumer's no-op path
rather than triggering pydantic-ai structured-output retry storms.

The router is **built-in infrastructure**, not a user-customizable
agent. Its definition lives in code (`router/definition.py`) and is
auto-appended to the bridge's `AgentRegistry`. Operators tune it via
env vars; user-authored `agents/*.md` files cannot accidentally
override it (the name `_router` is reserved by convention, and the
registry rejects multi-router lists at boot — see `Schema
constraints` below; note that the constraint is enforced at registry
construction in `bridge/registry.py`, not at the `AgentDefinition`
schema level).

## Architecture

Four independent processes, communicating exclusively through Kafka:

```
┌────────────────── calfkit-bridge ─────────────────┐
│ DiscordIngressGateway → MessageNormalizer →       │
│   BridgeIngress.handle(wire)                      │
│     - if wire.kind == "slash":  publish to        │
│       discord.channel.{cid}.in                    │
│     - if wire.kind == "message" (ambient):        │
│       publish to discord.ambient.in (via the      │
│       state.metadata helper, see "Implementation  │
│       notes")                                     │
│                                                   │
│ Synthesized-in @consumer (NEW)                    │
│   - subscribes: bridge.synthesized.in             │
│   - reads wire from result.state.metadata         │
│   - calls ingress.handle(wire)  ← reuses ingress  │
│     to publish to channel topic and populate      │
│     PendingWires                                  │
│                                                   │
│ Outbox @consumer (unchanged)                      │
│   - subscribes: discord.outbox                    │
│   - posts agent replies to Discord                │
└───────────────────────────────────────────────────┘
              ▲                            │
              │                            ▼
   discord.outbox          discord.channel.{cid}.in  ─→  ┌── calfkit-agent ──┐
   bridge.synthesized.in                                  │ Assistant agents  │
                                                          │  - gate: kind=    │
                                                          │    slash@self     │
                                                          │  - ReturnCall →   │
                                                          │    discord.outbox │
                                                          │  (assistants do   │
                                                          │   NOT subscribe   │
                                                          │   to ambient —    │
                                                          │   the router is   │
                                                          │   the sole        │
                                                          │   consumer of     │
                                                          │   discord.ambient │
                                                          │   .in)            │
                                                          └───────────────────┘
                                       │
                                       ▼
                          ┌──────── calfkit-router ─────────┐
                          │ Router agent                     │
                          │   - subscribes:                  │
                          │     discord.ambient.in           │
                          │   - publish_topic:               │
                          │     routing.decisions            │
                          │   - LLM emits ONE                │
                          │     dispatch(agent_id="...")     │
                          │     call (ToolOutput pattern;    │
                          │     no tool body runs)           │
                          │                                  │
                          │ Fan-out @consumer                │
                          │   - subscribes:                  │
                          │     routing.decisions            │
                          │   - reads wire from              │
                          │     result.state.metadata        │
                          │   - synthesizes one kind=slash   │
                          │     wire for the chosen agent    │
                          │   - publishes to                 │
                          │     bridge.synthesized.in        │
                          └──────────────────────────────────┘
                                       │
                                       └─→ (loops back to bridge synthesized-in)
```

Slash and @-mention paths bypass the router entirely. They continue
to publish directly from `BridgeIngress.handle` to
`discord.channel.{cid}.in` with `reply_topic=discord.outbox`.

### Per-message flow (ambient)

1. A human posts an ambient message (no `/slash`, no `@-mention`).
2. Bridge normalizes to `kind="message"` and publishes to
   `discord.ambient.in` with `reply_topic=_calf.ambient.callback-discard`.
   The original wire and the phonebook are packed into `state.metadata`
   (see "Implementation notes" for why).
3. Router agent consumes the envelope, runs its LLM with the phonebook
   roster injected as `temp_instructions`. The LLM emits one
   `dispatch(agent_id="scribe", reasoning="...")` tool call. Pydantic-ai's
   `ToolOutput` pattern terminates the agent loop on that call without
   running a tool body — one LLM turn, no second-pass narration.
4. The router's `ReturnCall` publishes to two topics (existing calfkit
   double-publish behavior):
   - `_calf.ambient.callback-discard` (the inbound `reply_topic`) —
     nobody subscribes; the envelope is retained per Kafka topic
     retention and eventually discarded.
   - `routing.decisions` (the router's `publish_topic`) — picked up
     by the fan-out consumer.
5. Fan-out consumer recovers the original wire from
   `result.state.metadata["wire"]` and processes the single
   `decision.agent_id`:
   - Skips the no-op cases: `agent_id=None` (defense-in-depth for a
     misbehaving LLM), `agent_id == router_agent_id` (defensive
     self-filter), `agent_id` not in the publisher's phonebook
     (LLM hallucination / registry drift — ERROR log).
   - Otherwise, synthesizes a wire copy with a fresh `event_id`,
     `kind="slash"`, `slash_target=<agent_id>`. Channel, message id,
     and author are preserved from the original ambient.
   - Publishes to `bridge.synthesized.in` via `invoke_node_with_metadata`,
     packing the synthesized wire into `state.metadata`. Fire-and-forget
     (the handle's future is cancelled — same pattern as
     `BridgeIngress.handle`).
6. Bridge synthesized-in consumer receives the envelope, recovers the
   synthesized wire from `state.metadata`, and calls
   `BridgeIngress.handle(wire)`. The slash branch fires: publish to
   `discord.channel.{cid}.in`, populate `PendingWires` keyed on the
   synthesized event id.
7. The targeted assistant's `addressed_to_me_gate` accepts (kind=slash
   matched to its own id), the LLM runs, `ReturnCall` publishes to
   `discord.outbox`.
8. Bridge outbox consumer reads `discord.outbox`, finds the synthesized
   wire in `PendingWires` (keyed on `correlation_id` = synthesized
   `event_id`), and posts the reply to Discord as an inline reply to
   the original human message.

### What slash and @-mention do (unchanged)

Bridge ingress branches on `wire.kind`:

- `kind == "slash"` (real slash command or @-mention normalized to slash
  by `MessageNormalizer`): publish to `discord.channel.{cid}.in`,
  `reply_topic=discord.outbox`, deps populated. `PendingWires.put` on
  the wire. Targeted assistant accepts; replies as before. The router is
  not invoked.
- `kind == "message"`: ambient path (see above).

## Configuration

The router exposes four runtime knobs (`provider`, `model`,
`thinking_effort`, `history_turns`). They can be set in either of two
ways:

* `router.yml` at the project root (file-based, recommended for
  long-lived deployments — versionable, easy to diff).
* `CALFKIT_ROUTER_*` environment variables (runtime overrides — handy
  for staging a swap without editing files).

When both are set on the same field, the file wins. Missing fields
fall through to env vars, then to the in-code defaults.

### Required env vars

Router-specific:

```
CALFKIT_ROUTER_PROVIDER=openai            # anthropic | openai | openai-codex
CALFKIT_ROUTER_MODEL=gpt-5-nano           # fast/cheap recommended
CALFKIT_ROUTER_THINKING_EFFORT=none       # none | minimal | low | medium | high | xhigh | max
CALFKIT_ROUTER_HISTORY_TURNS=10           # 0..100; channel history window for routing decisions
CALFKIT_ROUTER_CONFIG_PATH=router.yml     # override the YAML config path (default ./router.yml)
```

Defaults: `openai` / `gpt-5-nano` / `none` / `10`. The router runs once
per ambient message — keep it fast and cheap. The router's history
window is intentionally smaller than per-agent assistants (default 30)
because the router only needs enough context to recognize follow-ups vs.
fresh topics, not to carry the conversation. Invalid values (non-integer
or outside 0..100) fall back to the default with a WARN log rather than
crashing the router build.

### Router config file (`router.yml`)

Optional YAML file at the project root (or wherever
`CALFKIT_ROUTER_CONFIG_PATH` points). When present, its fields take
precedence over env vars on a per-field basis:

```yaml
# router.yml
provider: openai-codex
model: gpt-5.3-codex
thinking_effort: minimal
history_turns: 10
```

A committable template lives at `router.yml.example` at the repo
root — copy it and edit:

```bash
cp router.yml.example router.yml
```

All four fields are optional — omit any to fall through to the matching
env var, then the in-code default.

Loader behavior at boot:

| Scenario                                          | Behavior                                                     |
|---------------------------------------------------|--------------------------------------------------------------|
| Default path `./router.yml`, file missing         | Silent fallback to env + defaults (backward compat).         |
| `CALFKIT_ROUTER_CONFIG_PATH` set, file missing    | **Boot error** — operator pointed at a path explicitly.       |
| File present, empty / whitespace-only             | Boot error — almost always a half-finished edit.             |
| File present, malformed YAML                      | Boot error with file path and parse-error location.          |
| File present, unknown key (e.g. typo `provder:`)  | Boot error from pydantic `extra="forbid"`.                   |
| File present, reserved key (`role`, `name`, etc.) | Same — the router's identity is not operator-tunable.        |

**What's NOT in the schema** by design: `name`, `display_name`,
`description`, `avatar_url`, `role`, `publish_topic`, `tools`, and
`system_prompt`. These are router-singleton invariants (`agent_id =
"_router"`, `role="router"`, etc.) — the registry depends on them
being fixed. The Discord slash command is always `/<name>` (i.e.
`/_router` for the router), so the slash is implicitly reserved with
the agent_id. Operators who need to change the routing prompt should
edit `src/calfkit_organization/router/prompt.py` and re-deploy.

**Container deploys:** the docker-compose `router:` service does NOT
bind-mount `router.yml` by default — Docker would silently create a
directory at the host path if the file is absent, leaving a stray
`router.yml/` at repo root for deploys that don't use the feature.
To opt in, add this to your `docker-compose.override.yml`:

```yaml
services:
  router:
    volumes:
      - ./router.yml:/app/router.yml:ro
```

Shared with the assistant runner (also required by `calfkit-router`):

```
CALF_HOST_URL=<broker-host[:port]>        # Kafka bootstrap; defaults to "localhost"
OPENAI_API_KEY=...                        # required iff CALFKIT_ROUTER_PROVIDER=openai (default)
ANTHROPIC_API_KEY=...                     # required iff CALFKIT_ROUTER_PROVIDER=anthropic
```

The LLM key is read by the provider SDK at first invocation, not at
boot — a missing key surfaces as an exception on the first ambient
message rather than at process start. Set it in the
`calfkit-router` deployment's secret store, not just the bridge's.

The router definition is built in code; these env vars are the only
operator-tunable surface. The router does NOT honor `agents/*.md`
overrides or the `/thinking-effort` slash command. The slash UI
already excludes the router from its choice list
(`bridge/slash.py`'s `_build_thinking_effort_command` filter), so
an operator cannot select it from Discord. If something does reach
`AgentRegistry.set_thinking_effort("_router", ...)` directly (e.g.
a future admin tool that bypasses the slash UI), it **raises**
`ValueError("agent '_router' has no source_path; cannot rewrite
frontmatter")` — `source_path` is `None` for the in-code router
definition, and the rewrite path requires a backing `.md` file.

### Discord access

The router does not touch Discord. It needs:

| Resource                      | Router |
|-------------------------------|:------:|
| Kafka broker                  |   yes  |
| LLM provider API key          |   yes  |
| Discord bot token             |    —   |
| `agents/*.md` (local files)   |    —   |

The phonebook (which the router uses to build its roster prompt)
arrives via `state.metadata` from the bridge — no local file access
required.

## Operating

### Running locally

```bash
uv sync
docker compose up -d                  # Kafka broker
uv run calfkit-bridge
uv run calfkit-agent                  # all assistants
uv run calfkit-tools
uv run calfkit-router                 # NEW — required for ambient
```

All four processes are independently restartable.

### Deploy checklist (production)

Before the first ambient message reaches production, the following
**must** be true. None of these are enforced by the code — they are
operator responsibilities the bridge cannot verify on its own.

1. **Kafka topic retention on the discard topic** (privacy /
   compliance). `_calf.ambient.callback-discard` receives a copy of
   every ambient envelope, including the original Discord wire
   (author + plaintext content) and the phonebook. Cluster default
   retention (typically 7 days) would persist that on a topic
   nobody reads. Run BEFORE the first ambient publish:

   ```bash
   kafka-configs.sh --alter --entity-type topics \
       --entity-name _calf.ambient.callback-discard \
       --add-config retention.ms=60000,cleanup.policy=delete
   ```

   Verify with `kafka-configs.sh --describe --entity-type topics
   --entity-name _calf.ambient.callback-discard`. The topic name
   starts with an underscore; some `kafka-topics.sh --list`
   invocations hide it unless you pass the literal name.

2. **Process startup order.** `calfkit-router` must be running
   before `calfkit-bridge` starts accepting Discord traffic.
   Without the router, ambient messages publish to
   `discord.ambient.in` and sit there until retention expires; the
   user sees no reply. There is no in-process health check for the
   router from the bridge — coordinate via deployment ordering or
   a readiness probe upstream of the bridge.

3. **External monitoring on the silent-router signal.** Wire an
   alert on the "publish-without-arrival" log diff (see "Hard
   cutover" below) AND on the `ambient publish aborted` ERROR
   rate. Both should be zero in steady state.

4. **Secrets in `calfkit-router`'s environment.** The router needs
   `OPENAI_API_KEY` (default) or `ANTHROPIC_API_KEY` (if
   `CALFKIT_ROUTER_PROVIDER=anthropic`), plus `CALF_HOST_URL` for
   Kafka. Set them in the router deployment's secret store, NOT
   just the bridge's — the router runs as an independent process
   with no shared filesystem.

5. **calfkit version pin.** `pyproject.toml` pins
   `calfkit~=0.3.1`. `src/calfkit_organization/_compat/invoke.py`
   depends on calfkit private APIs (documented in the module's
   FIXME); a minor calfkit bump (`0.4.0`) could rename them and
   break the bridge at import time. Do NOT relax this constraint
   without verifying the upstream cleanup at calfkit-sdk#144 has
   landed.

6. **Discord "Read Message History" permission.** The bridge fetches
   recent channel history on every agent invocation and projects it
   into the agent's `message_history` (see "Conversation history"
   below). The fetch uses Discord REST `GET /channels/{id}/messages`,
   which requires the bot to have the **Read Message History**
   permission in every served channel. Missing the permission is not
   fatal: the bridge logs a WARN (once per channel) and the
   invocation proceeds with empty history. But quality degrades —
   agents lose multi-turn context. Grant Read Message History
   alongside View Channel in the bot's role configuration for every
   guild it operates in.

### Conversation history

The bridge fetches recent channel history on every agent invocation
and projects it from that agent's point of view (the agent's own
prior webhook posts become `ModelResponse` turns; everyone else
becomes `ModelRequest` turns with the speaker's display_name
prefixed into the user content as `<name>`).

| Knob | Where | Default | Range |
|---|---|---|---|
| Per-assistant window | `history_turns:` in `agents/<name>.md` frontmatter | 30 | 0..100 (0 disables) |
| Router window | `CALFKIT_ROUTER_HISTORY_TURNS` env on the router process | 10 | 0..100 (0 disables) |

The upper bound of 100 matches Discord's per-call REST cap. History
fetches are cached in-process for 2 seconds keyed on
`(channel_id, before_message_id, limit)`. Under the single-agent
routing policy the ambient path makes one Discord call per ambient
message; the cache mainly absorbs the slash path's repeated fetches
during burst typing. The cache is process-local and ephemeral; bridge
restarts cold-start it.

A2A `private_chat` is **stateless** — peer-to-peer invocations do
not carry channel history. The caller is responsible for putting any
needed context into the message content.

### Clearing context (`/clear`)

`/clear` is an owner-gated operator slash that resets conversation
context for **every** agent in the channel it is run in. It is
**non-destructive** — no Discord messages are deleted.

How it works: the bot posts a sentinel marker message
(`CLEAR_MARKER_TEXT` in `bridge/history.py`) into the channel. On every
subsequent invocation the history fetcher truncates the fetched records
at the **most recent** marker, dropping the marker and everything above
it. The boundary therefore lives in the channel itself, which means it
**survives bridge restarts** (unlike the in-process fetch cache) with no
extra state store.

- **Recognition.** A marker is only honored if it is the bot's own
  **non-webhook** message *and* its content exactly equals the sentinel
  (see `is_clear_marker`). A user typing the sentinel text, or an agent
  persona webhook posting it, is **not** a boundary — authorship cannot
  be forged.
- **Scope.** Per channel/thread. The marker exists only where it was
  posted and the fetcher keys on the source channel, so `/clear` in a
  thread clears that thread, and `/clear` in a parent channel does not
  clear its threads.
- **Authorization.** Restricted to `DiscordSettings.owner_user_id`
  (same as `/thinking-effort`). When `owner_user_id` is unset, the
  slash is open to anyone.
- **Window interaction.** Truncation composes with `history_turns`: the
  floor trims the old end, the per-agent count cap trims the new end. If
  the marker scrolls beyond the fetch window, every message in the
  window is already newer than it (post-clear), so the result is still
  correct without finding the marker.
- **Going-forward only.** A message already in flight when `/clear`
  runs may still see pre-clear context for that one turn. Deleting the
  marker message un-clears the channel (the marker *is* the boundary).

A2A `private_chat` history is a separate, stateless reader and does
**not** honor the marker.

### Task threads (`/task`)

`/task <message>` posts the message into the invoking channel, opens a
public **thread** anchored on it, and routes the message **ambiently** so the
router summons whichever agents the task needs. Agent replies — and the live
`⚙ running…` step-progress message — post **into the new thread**, realizing
the "threads are tasks" model as a first-class command.

- **Open to anyone** in the guild (no owner gate, unlike `/clear` and
  `/thinking-effort`): anyone can spin up a task.
- **Where it runs.** Only in a top-level text channel. Inside an existing
  thread it is rejected (Discord can't nest threads); forum/voice channels
  are rejected (the persona webhook needs a parent text channel).
- **Thread title** is derived from the message (whitespace-collapsed,
  truncated to Discord's 100-char cap; falls back to `Task` if empty).
- **Routing.** Always ambient (`kind="message"`) — the router decides the
  respondents. To involve a specific agent, `@mention` it inside the thread
  afterward (normal mention routing applies there).
- **Permissions.** The bot needs **Create Public Threads** (in addition to
  the **Manage Webhooks** it already needs for persona replies) in any
  channel where `/task` is used. A missing permission is surfaced to the
  invoker as an ephemeral error; the already-posted message is left in place.

This rides the same thread-aware reply path used for **any** message sent
inside a thread: when an event originates in a thread (its
`source_channel_id` differs from the flattened parent `channel_id`), the
outbox posts the agent's reply — and the steps consumer posts its live
progress message — into that thread rather than the parent. See
`WireMessage.thread_id`.

### Outbox retry behavior

When the outbox consumer fails to post an agent's reply to Discord
(common cause: an agent reply over Discord's 2000-character limit
triggers a 400-50035), the bridge **silently retries the agent with a
system-reminder prompt injection** rather than dropping the reply.
The retry is invisible to the user: it reuses the same
``correlation_id`` so the eventual successful reply anchors to the
original user message as a normal inline reply.

The retry message_history contains:

* the channel-history projection from the original invocation
  (unchanged from what the LLM saw the first time);
* the original user prompt as a ``ModelRequest``;
* the LLM's failed reply as a ``ModelResponse`` so the LLM can see
  what it tried;
* a ``<system-reminder>``-tagged ``UserPromptPart`` carrying the
  literal Discord error text (e.g. ``"HTTP 400: ... Must be 2000 or
  fewer in length."``) and the instruction to retry without
  mentioning the error to the user.

LLMs trained on the ``<system-reminder>`` convention treat the tag
as out-of-band metadata and don't leak it back to the user.

#### Retry budget + fallback

Each wire gets up to **2 retry attempts** beyond the original — after
which the outbox falls back to **chunk-splitting** the latest failed
reply into ≤1990-char chunks and posting each as a continuation from
the same persona. The first chunk uses Discord's inline-reply anchor;
subsequent chunks are bare follow-ups directly below. This guarantees
the user never loses the agent's content entirely, even if the agent
cannot comply with the constraint after retries.

Retries do not apply to **non-agent-fixable** errors:

| Status | Reason | Behavior |
|---|---|---|
| 401 | bot token invalid | log WARN, drop |
| 403 | Manage Webhooks / View Channel missing | log WARN ("operator must verify Manage Webhooks permission"), drop |
| 404 | channel or webhook deleted | log WARN ("operator must check the channel exists"), drop |
| 429 | rate limited | discord.py already retried internally; log WARN, drop |
| 5xx | Discord-side outage | one internal retry-with-delay then log WARN, drop |

Agent retrying with revised content cannot fix any of these — they
need operator action (or wait for Discord to recover). The drops
preserve the existing operator-actionable WARN log lines.

#### Log lines to watch

| Log line | Meaning |
|---|---|
| ``posted reply event_id=... agent=...`` (INFO) | Original reply posted successfully — no retry needed. |
| ``outbox post failed; triggering agent retry attempt=N`` (INFO) | A retry has been triggered after a 4xx; agent will revise. |
| ``agent retry succeeded after N attempt(s)`` (INFO) | A retry succeeded; user sees a single reply anchored to the original question. |
| ``retry budget exhausted attempt=N max=N; chunk-splitting`` (WARN) | Both retries failed; chunk-splitting fallback engaged. The literal ``max=`` value reflects ``MAX_REPLY_RETRY_ATTEMPTS`` at runtime. |
| ``chunk-split posted chunk M/N`` (INFO) | Each chunk successfully posted. |
| ``outbox post failed (not retryable)`` (WARN) | A non-agent-fixable error — log + drop, no retry. |
| ``pending entry evicted before retry could be claimed`` (WARN) | Bridge-local LRU evicted the entry under pathological load; fell back to chunk-split. |
| ``retry publish failed event_id=... falling back to chunk-split`` (ERROR) | The Kafka publish of the retry envelope itself failed; chunk-split took over. |

If you observe an agent's reply never reaching Discord, grep for
``event_id=<the affected event>`` in the bridge logs and look for one
of these signals to diagnose where the path broke.

#### Configuration

Currently hardcoded (no env vars):

| Constant | Default | Where |
|---|---|---|
| ``MAX_REPLY_RETRY_ATTEMPTS`` | 2 | ``bridge/outbox.py`` |
| ``CHUNK_SAFE_SIZE`` | 1990 chars | ``bridge/outbox.py`` |
| ``NON_AGENT_FIXABLE_STATUSES`` | ``{401, 403, 404, 429}`` | ``bridge/outbox.py`` |

If a specific Discord error code reliably defeats the generic
``<system-reminder>`` text in production (the LLM doesn't adapt
correctly), add an override entry to
``_RETRY_REMINDER_OVERRIDES``. Empty by default — populate only on
empirical evidence.

### Hard cutover

`calfkit-router` is **required** for ambient mode. Without it, ambient
messages are silently swallowed:

- Bridge publishes the ambient wire to `discord.ambient.in`.
- No router process is subscribed → no decision is made → no
  synthesized wires get published → no assistant replies.
- The Kafka envelope sits on the topic until retention expires.

There is no broadcast-fallback path. The legacy `kind=message` branch
of `addressed_to_me_gate` is gone. If you don't run `calfkit-router`,
only slash and @-mention will produce replies.

Operators wanting a visual signal that ambient is broken can correlate
two INFO log lines:

- `bridge.ingress` logs `ingress ambient publish event_id=...` on
  every ambient publish.
- `bridge.synthesized` logs `synthesized-in arrival event_id=...` on
  every arrival from the router.

A growing gap between these counts (ambient publishes without
corresponding synthesized arrivals) indicates a silent router. Per-reply
WARN tracking is deferred for v1 — see "Out of scope" below.

**False negative on this signal: empty roster.** If the registry has
no eligible assistant agents (only the built-in router is loaded),
`BridgeIngress._publish_ambient` raises `AmbientRosterEmptyError`
*before* the publish log fires — the gateway catches it and replies
to the user inline. In that case the "publish vs. arrival" diff
stays at 0 even though the user *did* send a message. If both
counts are flat AND you suspect ambient is broken, also grep for
`bridge.ingress` ERROR `ambient publish aborted: empty router
roster`; that line identifies the affected event/channel and means
the registry needs an assistant added, not that the router is
silent. The two cases require different operator action — distinguish
them before paging the router-process oncall.

**Monitoring recommendation.** Set up an alert on the
"publish-without-arrival" gap (a counter or log-volume diff over a
sliding window) AND on the `ambient publish aborted` ERROR rate.
Both should be at zero in steady state; either firing indicates a
specific class of degradation.

### Adding a new assistant agent

Existing flow is unchanged. Drop a new `agents/<name>.md`, restart
the bridge (so the registry sees it), restart the affected agent
processes. The router automatically sees the new agent on its next
ambient invocation because the bridge stamps the phonebook into
`state.metadata` on every publish.

## Topology reference

### Topics introduced

| Topic                                | Producer                              | Consumer                       |
|--------------------------------------|---------------------------------------|--------------------------------|
| `discord.ambient.in`                 | Bridge ingress (ambient branch)       | Router agent                   |
| `routing.decisions`                  | Router agent (publish_topic)          | Fan-out @consumer              |
| `bridge.synthesized.in`              | Fan-out @consumer                     | Bridge synthesized-in @consumer|
| `_calf.ambient.callback-discard`     | Router agent (frame.callback_topic)   | nobody (intentional)           |

The discard topic exists because calfkit's `_publish_action` always
publishes `ReturnCall` to `frame.callback_topic` (= the caller's
`reply_topic`) **in addition** to whatever `publish_topic` triggers via
FastStream's `@publisher` wrapping. We point the discard topic at a
no-op so the second publish lands somewhere harmless. The router's
useful output goes via `publish_topic` to `routing.decisions`.

**Operator action required — retention.** The discard topic receives
a copy of every router envelope, which includes `state.metadata` with
the original Discord wire (author, content) and phonebook. The same
envelope also goes to `routing.decisions` for the fan-out consumer,
so we cannot strip the metadata from the discard side without losing
it from the consumed side. Configure short retention on the discard
topic explicitly (the cluster default is typically 7 days, which
would persist plaintext message history on a topic nobody reads):

```bash
kafka-configs.sh --alter --entity-type topics \
    --entity-name _calf.ambient.callback-discard \
    --add-config retention.ms=60000,cleanup.policy=delete
```

### Topics unchanged

| Topic                                | Producer                              | Consumer                       |
|--------------------------------------|---------------------------------------|--------------------------------|
| `discord.channel.{cid}.in`           | Bridge ingress (slash branch + synthesized-in consumer) | Assistant agents |
| `agent.{aid}.in`                     | calfkit-tools (private_chat)          | Assistant agents (A2A)         |
| `discord.outbox`                     | Assistant agents (ReturnCall)         | Bridge outbox @consumer        |

A2A semantics (private_chat) are unchanged. Tool deployment, persona
projection, audit channels — all untouched.

## Schema constraints

`AgentDefinition` has two router-specific fields:

- `role: Literal["assistant", "router"]` — defaults to `"assistant"`.
  User-authored `agents/*.md` should not set this.
- `publish_topic: str | None` — `None` for assistants;
  `"routing.decisions"` for the built-in router.

A model-level validator enforces two invariants on routers:
- `tools` must be empty (the router uses `ToolOutput`, not function
  tools).
- `publish_topic` must be non-None.

The bridge `AgentRegistry` rejects multiple routers at boot (any
configuration that produces more than one is a wiring bug). Zero
routers fails lazily when `registry.router()` is called — in
production this can only happen via direct `AgentRegistry(...)`
construction (which test code does), because `from_agents_dir`
auto-appends the built-in router definition.

## Implementation notes

### `state.metadata` and the `_compat/invoke.py` helper

The fan-out @consumer needs to recover the original Discord wire (so
it can synthesize the wire with the right channel id, message id,
author, etc.). The natural place for that data would be
`ctx.deps.provided_deps["discord"]` — and the bridge ingress does put
it there. But calfkit's `@consumer` decorator only exposes
`NodeResult` to the consume function; the consume function never sees
`ctx.deps`.

Calfkit's `State` model has a `metadata: Any` field explicitly
documented for application data. `State` propagates through every
`_publish_action` branch in `calfkit/nodes/base.py` (parallel-fanout,
Call, ReturnCall, TailCall) — each constructs the publish envelope
with `state=output.state` or `state=call.state`. The calfkit
`Agent.run` mutates only `message_history` and `final_output_parts`
on our State instance; `state.metadata` rides through unchanged.
(Pydantic-ai's internal `GraphAgentState` is a separate object with
the same field name — its metadata is touched by pydantic-ai's run
loop, but it isn't the calfkit `State` we set.)

The catch: `Client.invoke_node()` does not expose a `metadata`
parameter. It constructs `State` internally with `metadata=None`. To
set it without subclassing, we use a small helper at
`src/calfkit_organization/_compat/invoke.py` that calls
`Client._invoke` (single-underscore, convention-private) directly with
a pre-populated State.

This is a **temporary workaround**. The helper carries a FIXME pointing
at the upstream cleanup that obviates it (tracked at
[calf-ai/calfkit-sdk#144](https://github.com/calf-ai/calfkit-sdk/issues/144)):

1. **Preferred**: expose `deps` on `NodeResult`. Consumers then read
   the original wire from `result.deps.provided_deps["discord"]` and
   the metadata channel becomes unnecessary. ~15-line SDK change.
2. **Half-step**: add `metadata=` parameter to `Client.invoke_node`.
   Removes the need to dip into `_invoke` directly but keeps the
   metadata channel.

Two callers in this project use the helper:

- `bridge/ingress.py:_publish_ambient` — packs the original wire +
  phonebook into `state.metadata` so the router → fan-out chain can
  recover them.
- `router/fanout.py` — packs the synthesized wire into
  `state.metadata` so the bridge synthesized-in consumer can recover
  it.

When the upstream cleanup lands, both callers swap to the public API
and the helper goes away. The `_compat/` package exists specifically
to make this migration easy to find.

### Why a synthesized-in consumer (instead of fan-out publishing direct)

The bridge's outbox consumer uses `PendingWires` — a process-local LRU
map keyed by `correlation_id` — to recover wire metadata (channel id,
message id, author) when posting agent replies to Discord. `PendingWires`
is only writable by `BridgeIngress.handle`. The fan-out consumer lives
in `calfkit-router` (a different process) and has no way to populate
the bridge's in-memory map.

So the fan-out cannot publish directly to `discord.channel.{cid}.in` —
it would orphan the wire from `PendingWires`, and when the assistant's
reply landed on `discord.outbox`, the outbox would look up the
synthesized correlation id, find nothing, and silently drop the reply.

Routing synthesized wires through `bridge.synthesized.in` and back into
`BridgeIngress.handle()` keeps the bridge as the single source of
truth for wire publication + `PendingWires` population. The
synthesized-in consumer is a 3-line consume function — deserialize,
delegate, log — colocated with `BridgeIngress` for in-process access.

A future SDK cleanup (exposing `deps` on `NodeResult`, as above) would
also allow the outbox consumer to read the wire from the reply
envelope's deps instead of from `PendingWires`. At that point the
synthesized-in consumer could be folded into the router process (or
deleted, with the fan-out publishing directly). For now: colocation.

### Router final-output type

The router is configured with
`final_output_type=ToolOutput(RoutingDecision, name="dispatch")`. This
nominates a pseudo-tool whose schema is exposed to the LLM but whose
body never runs. When the LLM calls `dispatch(agent_id="...",
reasoning="...")`, pydantic-ai's `_agent_graph.py` recognizes the tool
call as the agent's terminal output, captures the args as a
`RoutingDecision` instance, and ends the loop with
`end_strategy="early"`. One LLM turn, no second-pass narration to
wrap up after tool results.

This is strictly cleaner than a function-tool dispatch pattern (which
would require two LLM round-trips per ambient message — one to emit
the tool call, one to produce a final output after the tool result).

## Out of scope (deferred)

### Small follow-ups

- **SDK PR — expose `deps` on `NodeResult`** (preferred) or add
  `metadata=` to `Client.invoke_node`. Obviates the `_compat/invoke.py`
  helper. ~15-line upstream change. Tracked at
  [calf-ai/calfkit-sdk#144](https://github.com/calf-ai/calfkit-sdk/issues/144).
- **Per-reply ambient WARN tracking.** v1 only INFO-logs at publish
  and synthesized-arrival; operators correlate by grep. A small
  in-memory expectations map could turn that into a real WARN signal.
- **`_run_worker` extraction.** Three copies now exist
  (`agents/runner.py`, `tools/runner.py`, `router/runner.py`). Extract
  to a shared `calfkit_organization/runtime.py` if the duplication
  becomes annoying.

### Bigger deferrals

- **Per-channel routing policy.** Today there's one global router for
  all channels.
- **Token-budget cap on history.** v1 uses count-based truncation
  (`history_turns`). A 2K-token paste followed by 29 short messages
  blows the LLM context. v2 will add a token-budget cap on top of
  the count cap so the smaller of the two wins.
- **History edit/delete propagation.** Once fetched, an in-flight
  invocation sees the historical snapshot. A user editing a recent
  message after the fetch won't be reflected until the next
  invocation re-fetches.
- **A2A `private_chat` history.** A2A is intentionally stateless RPC
  in v1; peers receive only the caller's `content`. A future
  opt-in `continue_thread:` flag could enable multi-turn collaboration
  if real usage demands it.
- **Audit projection.** Routing decisions are logged at INFO inside
  the router process. No Discord-side audit channel mirrors the
  decisions.
- **Hot-reload of the router definition.** Env-var changes require
  `calfkit-router` restart.

## Files

```
src/calfkit_organization/
├── agents/
│   ├── definition.py          (modified — role, publish_topic, validator)
│   ├── factory.py             (modified — role="router" build path)
│   ├── gates.py               (modified — addressed_to_me_gate accepts only kind=slash)
│   └── routing.py             (new — RoutingDecision schema)
├── bridge/
│   ├── gateway.py             (modified — register synthesized-in consumer)
│   ├── ingress.py             (modified — kind-branch on handle)
│   ├── registry.py            (modified — auto-append router, router() accessor)
│   └── synthesized.py         (new — build_synthesized_consumer)
├── router/                    (new package — built-in routing agent)
│   ├── definition.py          (build_router_definition + ROUTER_AGENT_ID)
│   ├── fanout.py              (build_fanout_consumer)
│   ├── prompt.py              (SYSTEM_PROMPT)
│   ├── roster.py              (build_router_temp_instructions)
│   └── runner.py              (calfkit-router CLI entry)
└── _compat/                   (new package — temporary SDK workarounds)
    └── invoke.py              (invoke_node_with_metadata helper)
```

Calfkit SDK: zero changes.
