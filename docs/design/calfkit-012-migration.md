# calfkit 0.12 migration — design spec

- **Status:** Draft — reviewed (3 rounds; converged). Decisions resolved; **all 3
  calfkit gates shipped (0.12.2)** — the migration is **fully unblocked** (§2).
- **Date:** 2026-06-28 (updated 2026-06-30 for calfkit 0.12.2)
- **Owner:** Ryan
- **Supersedes / relates to:** `docs/design/mcp-reintroduction.md`,
  `docs/design/calfkit-worker-lifecycle-gaps.md`, `docs/ambient-routing.md`
  (the router design this migration removes), `docs/a2a-threads.md`
  (the A2A projection this migration re-homes).

> **Scope discipline.** This document records **only the design choices that
> have been explicitly confirmed.** All design decisions are now resolved; §8 is the
> **decision log**, not a list of open items. A few *implementation* choices are
> deferred to build time and flagged inline (e.g. the A2A anchor-message policy §6.2,
> replay-hydration accept-or-rehome §9, `state.py` delete-vs-shell §10).

---

## 1. Context & goal

calfcord currently hand-rolls substrates that calfkit **0.12.x** now ships natively
(agent mesh + caller surface in 0.12.0, intermediate streaming in 0.12.1, the
caller-side live-agents/​tools mesh view in 0.12.2):

- **agent addressing + discovery + agent-to-agent (A2A) messaging** — calfkit
  `#279`/`#281` (the `message_agent` tool, `peers=[Messaging(...)]`, and the
  `AgentCard` directory on the compacted `calf.agents` control-plane topic);
- a **caller surface** for non-agent processes — calfkit `#289`
  (`Client.connect()` → `client.agent(name).start()/execute()`, the
  `InvocationHandle`, and the firehose);
- **agent-POV message-history projection** — on by default in the agent runtime.

The goal of this migration is to **delete the calfcord code these primitives
replace**, shrinking the system to a smaller, more maintainable architecture
while preserving calfcord's observable behavior (§3).

This is a large, mostly-subtractive change: it removes one of the five process
types outright (the router), removes the bespoke A2A transport, and replaces the
per-channel-topic + gate routing machinery with direct name-addressed calls.

---

## 2. Confirmed scope (locked decisions)

| # | Decision |
|---|----------|
| **C1** | **Bump to calfkit `~=0.12.2`** (0.12.1 ships streaming for §5/§6; 0.12.2 ships the live-agents mesh view for the roster), replacing `calfkit~=0.10.0` (`pyproject.toml:10`). **Resolves:** `calfkit-tools` 0.1.3 pins `calfkit<0.13,>=0.9.0`. |
| **C2** | **Delete the ambient router subsystem entirely.** Non-`@mention`, non-slash ("ambient") messages **go unanswered** — there is no automatic agent selection. |
| **C3** | **Delete `/task`.** The plaintext `/task` command is removed (it existed only to summon an agent via the router). |
| **C4** | **Full-native A2A messaging.** Replace the custom `private_chat` *transport* with calfkit's built-in `message_agent(name, message)` tool + `peers=[Messaging(...)]` + the native `AgentCard` directory. The custom A2A RPC, the forwarded-wire gate trick, and the deps-propagated phonebook directory are removed. |
| **C5** | **Drop the A2A timeout knob.** No app-side policing of agent-to-agent timing (native `message_agent` has no per-call deadline in v1; this aligns). The `CALFKIT_TOOLS_TIMEOUT_SECONDS` knob and the 60 s default are removed. |
| **C6** | **Bridge runs on the caller-surface `stream()` pattern** (see §5). One lazy `Client`; per-`@mention` `start()` → iterate `handle.stream()` → `handle.result()`; lossless terminal; `emitter_node_id` → persona; intermediate events (live progress + A2A projection) consumed off the run's own stream via a normalized step-event seam. |
| **C7** | **Adopt native handoff.** Agents that should transfer control declare `peers=[Handoff(...)]` and emit `HandoffRequest`; the responding peer answers the original caller, and the bridge posts **its** persona (emitter-driven — `base.py:498` / `node_result.py:91-99`, verified). Replaces the in-channel `@`-handoff teaching in `peer_roster.py`. |
| **C8** | **Persona display name = agent name.** Drop the separate `display_name`; the bridge attributes persona-webhook posts under the agent's `name`. |
| **C9** | **Avatar derived bridge-side from the agent name.** Drop the advertised/configured `avatar_url`; the bridge generates a deterministic avatar seeded by the agent name. |
| **C10** | **Remove the history-turns limit feature entirely.** Drop the per-agent `history_turns` field and all per-agent history trimming; the bridge passes the window it fetches (bounded by the Discord fetch cap, ~100). |
| **C11** | **Runtime settings via per-call override.** Replace the agent-side control command channel with a bridge-owned `agent → settings` map applied as a per-call `model_settings` override (e.g. thinking-effort). Applies to bridge-initiated invocations (native A2A consults **and** handoffs use the agent's own defaults). |

**Sequencing (confirmed):** **all three gating dependencies have now shipped** (deps
1–3, below) — the migration is **fully unblocked on calfkit 0.12.2** and can be
implemented end-to-end. It still lands in phases (§10) for safety/reviewability, not
because anything is gated. No transitional bridge (e.g. `execute()` + the
`agent.steps` mirror) is built — the shipped `stream()` is the target transport.

**Gating dependencies — status (updated 2026-06-30: ALL SHIPPED).**
1. **A public live-agents roster API** — **✅ SHIPPED (calfkit 0.12.2, #307).**
   `Client.mesh.get_agents()` returns `{name: AgentInfo}` where
   `AgentInfo = (name, description, last_seen)` — exactly name + description +
   liveness (`client/mesh.py:44-52`, `caller.py:211`). Public (`Mesh` / `AgentInfo` /
   `MeshViewConfig` re-exported from `calfkit` **and** `calfkit.client`). Caller-side,
   lazy, no Worker; **online-only** (staleness-filtered). Also exposes `get_tools()`.
   Consumption shape + `MeshUnavailableError` handling: §10. (Resolves
   [calfkit-sdk#301](https://github.com/calf-ai/calfkit-sdk/issues/301).)
2. **Intermediate streaming** — **✅ SHIPPED (calfkit 0.12.1, #302).** Verified
   against source: `RunEvent` includes `AgentMessageEvent` / `ToolCallEvent` /
   `ToolResultEvent` / `HandoffEvent` (public); `handle.stream()` yields
   intermediates-then-terminal (`hub.py:222`); steps publish to the **root caller's
   inbox** for the whole run tree, attributed by `emitter` + `depth`
   (`base.py:1798-1815`); a consulted `message_agent` peer's reply surfaces as a
   `ToolResultEvent` (`models/step.py`).
3. **`calfkit-tools` allowing calfkit 0.12** — **✅ RESOLVED:** `calfkit-tools`
   0.1.3 pins `calfkit<0.13,>=0.9.0` →
   [calfkit-peripherals#12](https://github.com/calf-ai/calfkit-peripherals/issues/12).

> **Status note (2026-06-30).** **All gates cleared — the migration is fully
> unblocked on calfkit 0.12.2.** Issues: live-agents reader
> [calfkit-sdk#301](https://github.com/calf-ai/calfkit-sdk/issues/301) (shipped as
> #307); streaming #302; `calfkit-tools` peripherals#12. Handoff loops remain
> unbounded (C7) — tracked on [#251](https://github.com/calf-ai/calfkit-sdk/issues/251);
> not a gate.

**Preserved-behavior contract (the acceptance bar for C1–C11):** see §3.

---

## 3. Preserved-behavior contract

The migration is **behavior-preserving** iff all of the following still hold:

1. **`@mention` routing** — an `@<agent>` message reaches that agent.
2. **Message history to agents** — agents receive the conversation history.
3. **Per-scope conversation scoping** — history is scoped to the **thread** when
   the message is in a thread, and to the **channel** when it is in a channel.
4. **A2A in a private channel** — agent-to-agent exchanges are rendered into a
   separate private Discord channel.

How each is satisfied post-migration:

| Contract | Mechanism after migration | calfkit anchor |
|---|---|---|
| `@mention` routing | Bridge resolves `@<id>` → agent name (the existing normalizer) and calls `client.agent(name).start(...)`. No shared channel topic, no `slash_target` gate. | `caller.py` `agent()`; ADR-0017 name → `agent.{name}.private.input` |
| Message history | Bridge fetches Discord history (existing `ChannelHistoryFetcher`), builds **one** author-stamped canonical `message_history`, and passes it on each call. **Per-author re-roling is deleted** — calfkit's agent-POV projection re-roles per viewer automatically. | agent-POV projection on by default (`nodes/_projection.py`, `nodes/agent.py`) |
| Per-thread / per-channel scoping | Unchanged in spirit, **simpler in mechanism**: scoping is now purely *"which Discord history do I fetch & pass"*, keyed by `source_channel_id` (thread) vs `channel_id` (channel). The per-channel **Kafka** topic disappears. | n/a (app-side history fetch) |
| A2A in a private channel | The calling agent's `message_agent`/handoff activity arrives as intermediate events on **its run stream** — **both** the call and the peer reply (tool-result event; see §2 dep 2). The bridge's A2A projector renders them into the private channel via the retained `A2AChannelResolver` + personas from the bridge registry. | §5, §6 |

---

## 4. Target process model

Dropping the router (C2) reduces the process types from **five to four**:

```
before:  bridge · agents · tools · router · mcp
after:   bridge · agents · tools · mcp
```

- **bridge** — Discord gateway + the calfkit **caller surface** (§5). Gains the
  A2A private-channel projection (re-homed from the tools process).
- **agents** — calfkit `Agent` nodes, now addressed **by name** (no per-channel
  topic subscriptions, no addressing gate); each declares `peers=[Messaging(...)]`
  for A2A (C4).
- **tools** — the `calfkit-tools` nodes (a **pinned PyPI dependency**, not vendored
  — the now-resolved gating dep 3). The first-party `private_chat` tool is removed by C4.
- **mcp** — per-server MCP toolbox processes (drop `capability_read`; update to
  public `MCPToolbox`/`Tools` handles — §7).

> The `control_plane/` package is **deleted in full** (§7) — the command channel via
> per-call overrides, the roster onto `client.mesh` (0.12.2). Agents already
> auto-advertise `AgentCard`; nothing here is gated. The MCP module is trimmed (drop `capability_read`; update to
> public handles — §7).

---

## 5. Bridge design (the `stream()` pattern)

> **Status (shipped).** The intermediate streaming this design uses **shipped in
> calfkit 0.12.1** (#302). `RunEvent` carries `AgentMessageEvent` / `ToolCallEvent` /
> `ToolResultEvent` / `HandoffEvent` (public, re-exported from `calfkit.client`)
> followed by the terminal, and `handle.stream()` yields them in order
> (`client/events.py:64`, `client/hub.py:222`). This is the design to implement
> **now** — nothing is gated: all three calfkit deps shipped (the roster reader
> landed in 0.12.2, §2).

### 5.1 Principles

- **One lazy `Client` per process (pure caller — no Worker).** `Client.connect()`
  is synchronous, lazy, and does no I/O (`client/caller.py`); call it at bridge
  init. The bridge hosts no nodes, so it owns the broker lifecycle itself (explicit
  `aclose()` on shutdown). On a no-auto-create broker (Tansu) it must **provision
  its own inbox** — `Client.connect(provisioning=…)` or a pre-provisioned durable
  `inbox_topic=` — or terminal replies **and** step messages (both publish to that
  inbox) fail.
- **Per-`@mention` lossless path.** Each mention is one task: `start()` →
  iterate `stream()` → `result()`. The per-run handle channel (`_RunChannel`) is
  **lossless** (`hub.py:~85-181`); the firehose (`events()`) is best-effort
  drop-oldest and is **not** used for the reply path (ADR-0023). **At most one live
  `stream()` per handle** (`hub.py:230` raises on a second), so the A2A projector
  and the live-progress renderer share a **single** drain loop that fans out (§5.2).
- **Correct persona for free, including after handoff.** Every reply stamps
  `x-calf-emitter = self.node_id` (`nodes/base.py:498`), surfaced as
  `InvocationResult.emitter_node_id` (`models/node_result.py:91-99`). The
  emitter is the node that *actually* replied, so the bridge posts the
  responding agent's persona with no special casing.
- **Swappable step-source seam.** Renderers depend on a normalized `StepEvent`,
  never on `RunEvent`. The adapter `RunEvent → StepEvent` is the only code that
  knows the transport, so the source can change without touching the renderers.

### 5.2 Control flow (sketch)

```python
async def _handle_mention(self, msg) -> None:
    agent_name = self._roster.resolve_mention(msg.content)     # @id -> name (mesh-backed cached index; fail-open)
    if agent_name is None:
        return                                                 # ambient -> unanswered (C2)

    history = await self._history.as_model_messages(           # thread vs channel scoping
        source_channel_id=msg.source_channel_id or msg.channel_id,
    )                                                          # author-stamped; native POV projects

    handle = await self._client.agent(agent_name).start(
        msg.content, message_history=history,
        deps={"discord": msg.wire()}, author=msg.author_label,
    )

    a2a = A2ADispatcher()      # STATEFUL: records message_agent tool_call_ids; pairs reply↔call by id
    try:
        async for event in handle.stream():                    # step events, then the terminal
            if isinstance(event, (RunCompleted, RunFailed)):
                break                                          # terminal -> result() below
            # A2A claims: a ToolCallEvent(name=="message_agent"), its paired ToolResultEvent
            # (matched by tool_call_id — NOT by kind/name; a peer reply's name is the *peer*),
            # or a HandoffEvent. Everything else is live progress.
            if a2a.claims(event):
                await self._a2a.project(a2a.feed(event))       # -> private A2A channel
            else:
                await self._progress.on_step(event, channel_id=msg.channel_id,
                                             thread_id=msg.thread_id)
    finally:
        await self._progress.finish(handle.correlation_id)

    result = await handle.result()                             # cached, projected; raises on fault
    persona = persona_for(result.emitter_node_id)              # pure fn: name + dicebear avatar (handoff-correct; no roster)
    await self._post_reply(msg, persona=persona, text=result.output)
```

**The A2A/​progress split is stateful (round-3 M1).** There is no `message_agent`
step *kind* — a consult is a `ToolCallEvent` with `name == "message_agent"`, and its
reply is a `ToolResultEvent` whose `name`/`emitter` is the **peer** (or, for a
rejected consult, `name == "message_agent"`, `emitter == caller`, `is_error=True`).
A `ToolResultEvent` therefore can't be classified by its own fields; the dispatcher
records each `message_agent` `ToolCallEvent.tool_call_id` and routes the
matching-`tool_call_id` result to A2A (reliable because steps share one
`correlation_id` → single partition → request-before-reply order, and the handle
stream is lossless+ordered). The seam types are `StepEvent` (mapped from the public
`AgentMessageEvent`/`ToolCallEvent`/`ToolResultEvent`/`HandoffEvent`), `LiveProgress`,
`A2AProjector`, and the stateful `A2ADispatcher`.

### 5.3 Consequences for the agent side

Because the bridge addresses agents **by name** (C6):

- agents no longer subscribe to `discord.channel.{cid}.in` — `subscribe_topics`
  becomes empty (now optional, calfkit `#290`); each agent is reachable on its
  automatic private input topic;
- the addressing gate (`addressed_to_me` / `slash_target`) is removed;
- the agent factory's per-channel subscribe-topic list and gate install are
  removed.

---

## 6. Native A2A messaging + handoff + private-channel projection (C4, C7)

> **Public-surface note.** This whole section uses only **public** calfkit APIs:
> the agent-author handles `peers=[Messaging(...)/Handoff(...)]` (exported), and
> the auto-injected `message_agent` tool. calfcord never reads the internal
> `calf.agents` topic — calfkit resolves the directory inside the agent runtime.

### 6.1 Transport (native)

- **Messaging (consult).** Each A2A-capable agent declares
  `peers=[Messaging("<peer>", ...)]` (or `Messaging(discover=True)`). calfkit
  injects the built-in `message_agent(name, message)` tool, renders the live peer
  directory into its description from `calf.agents`, dispatches to the peer's
  private input topic, and folds the reply into the tool result
  (`nodes/agent.py:316-344,438-444`).
- **Handoff (transfer — C7).** Agents that should transfer control declare
  `peers=[Handoff("<peer>", ...)]`; the model emits `HandoffRequest(name, message)`
  as its turn output, control tail-calls to the peer, and the peer answers the
  **original** caller. The bridge posts the peer's persona because the reply is
  emitter-stamped by the responding node (`base.py:498` → `emitter_node_id`).
  - **Unbounded-loop footgun — consciously accepted (round-1).** Verified: calfkit
    has **no** handoff-loop guard. The cycle guard is messaging-only
    (`session_context.py:71-81`; `agent.py:403-409`), and calfkit's own source
    flags the handoff self-retry as "the entry to an unbounded loop (#251)"
    (`agent.py:386`). A misconfigured A→B→A handoff ring loops indefinitely (one
    LLM call per hop + broker load) with no backstop. **Accepted** as a known
    risk. Mitigation status: **both** the A→A staleness self-retry and the
    cross-agent A→B→A ring are **unbounded in v1** — calfkit's proposed
    `self_retry_budget` (issue #251) is *not shipped*. Raised the cross-agent ring
    on #251 ([comment](https://github.com/calf-ai/calfkit-sdk/issues/251#issuecomment-4825813737))
    to extend its budget carriage (a `calf.handoff_hops` cap) or split it out.
    Until a bound ships, operators keep the handoff graph acyclic by design; a
    calfcord config-load acyclicity check on the declared handoff graph is an
    optional cheap backstop. (Note: `RunFailed` carries no transport emitter, but
    `ErrorReport.origin_node_id` (0.12.1, #298) names the faulting node — use it as a
    best-effort persona for a faulted handoff, falling back to a generic error reply
    only when it is `None`.)
- **Removed:** the `private_chat` *transport* (the `Client.execute` RPC, the
  forwarded-wire `slash_target` gate trick, the `<thread_id>` return contract),
  the deps-propagated phonebook directory, and the A2A inbox topic
  (`agent.{id}.in`) + its subscription.
- **Stateless consult semantics (confirmed).** A native `message_agent` peer
  answers on a **fresh** conversation — it sees only the message, with no replay
  of prior A2A turns (`agent.py:438-444`, fresh `State()`). This is a behavior
  change from `private_chat` (which replayed the A2A thread); **accepted**.

### 6.2 Private-channel projection (preserved, re-homed)

- The Discord rendering of A2A is **preserved** and **re-homed to the bridge**.
  The retained `A2AChannelResolver` (currently `bridge/egress.py`, ~300 lines —
  already standalone) resolves/creates the unified private channel and anchors a
  thread per A2A exchange; personas come from the bridge registry.
- The projector is driven by the **bridge's run-stream** (§5). The shipped 0.12.1
  step events carry everything needed: the `message_agent` call as a `ToolCallEvent`
  (`name`, `args={name, message}`), the peer's reply as a `ToolResultEvent`
  (`parts`, `is_error`), and a handoff as a `HandoffEvent` (`target`, `reason`).
  Steps from the **whole run tree** reach the bridge (they publish to the root
  caller's inbox — `base.py:1798`), each carrying `correlation_id` + `emitter` +
  `depth`, so even nested consults are observable and attributable. The bridge's
  `normalize_run_event` maps these **public** types (`calfkit.client`
  `ToolCallEvent`/`ToolResultEvent`/`HandoffEvent`/`AgentMessageEvent`) into the
  internal `StepEvent`, rendered as persona webhooks into the private channel.
- **Anchoring + reply pairing (round-3).** Native consults are stateless (no
  conversation id). Anchor a thread per **`correlation_id`** (one per top-level human
  turn's A2A activity); pair each reply to its request by **`tool_call_id`**.
  Identity rules (M3): the **peer** identity for the thread comes from the
  *request's* `args["name"]` (the one source stable across success and rejection);
  on the **happy path** the reply's `emitter`/`name` *is* the peer (`node_id ==
  name`), so post under that persona; a **rejected** consult (offline/cycle/self) is
  a `ToolResultEvent` with `name == "message_agent"`, `emitter == caller`,
  `is_error=True` — render as a system/error note, not a peer post; a **faulted**
  peer may yield no reply step — note the failure in the thread. **Nested** consults
  (B→C) arrive too (`depth > 1`, `emitter == C`, same `correlation_id`) and render
  in the same thread. Pick the anchor-message policy before implementing.

---

## 7. Confirmed deletions / rewrites

Line counts are current `wc -l` from the investigation. All design decisions are
resolved (§8 is the decision log); inline **round-N** tags note where a fix landed.

### Delete (router subsystem — C2)
- `src/calfcord/router/` (all files, ~1,053 LOC + `router.md`)
- `src/calfcord/ambient_routing.py` (98)
- `src/calfcord/agents/routing.py` — `RoutingDecision` (122)
- `src/calfcord/bridge/synthesized.py` (162)
- `src/calfcord/cli/router_config.py` (281) + the `router` CLI verbs in `cli/main.py`
- ambient path in `bridge/ingress.py` (`_publish_ambient`, `_fetch_ambient_history`, `_router_history_turns`, the `kind=="message"` branch → log-and-return)
- topic constants `AMBIENT_INGRESS_TOPIC`, `SYNTHESIZED_INGRESS_TOPIC`, `ROUTING_DECISIONS_TOPIC` (`topics.py`)
- router service in `docker-compose.yml` / `docker-compose.dev.yml`; `calfkit-router` entry point (`pyproject.toml`); `router` slot in `supervisor/compose.py`; `router` in `cli/deploy.py`
- `docs/ambient-routing.md`; router references across docs
- router tests (~3,500 LOC: `tests/router/`, `test_ambient_routing_e2e`, `test_synthesized_consumer`, `test_ingress_router`, `test_registry_router`, `test_routing_schema`, `test_router_config`)

### Delete (`/task` — C3)
- `gateway.py` `_maybe_handle_task` / `_parse_task_command` (475-619)
- `normalizer.py` `normalize_task` (140-188)
- `/task` docs and tests

### Rewrite / shrink (A2A — C4, C5)
- `tools/private_chat.py` (1,422): **delete the transport half** (~700 LOC); the
  projection/thread/persona helpers are re-homed to the bridge A2A projector.
- `tools/__init__.py`: drop `private_chat_tool` from `ALL_TOOLS`.
- `agents/phonebook.py` (170): **delete** — directory is native (`AgentCard`);
  persona data comes from the bridge registry.
- `agents/peer_roster.py` (192): **delete** — the A2A roster is native, and the
  in-channel `@`-handoff teaching is replaced by native handoff (C7).
- A2A timeout: remove `CALFKIT_TOOLS_TIMEOUT_SECONDS` + the 60 s default
  (`private_chat.py:91-94`).
- `bridge/egress.py` `A2AChannelResolver` (300): **keep**, re-point its caller to
  the bridge A2A projector.

### Rewrite / shrink (bridge name-addressing + history — C6)
- `agents/gates.py` (147): remove the `addressed_to_me` / `slash_target` gate.
- `agents/factory.py`: remove per-channel subscribe-topic list, `agent.{id}.in`,
  gate install; `subscribe_topics` empty.
- `bridge/history.py` (895): **delete `project_history`** (app-side POV
  re-roling, ~100 LOC); keep the Discord fetcher, emitting one author-stamped
  canonical history.
- `bridge/outbox.py` / `bridge/pending_wires.py`: the **reply-correlation**
  machinery (the durable `discord.outbox` consumer group, the
  `correlation_id → reply context` LRU snapshot) is **removed** — `start()/result()`
  returns the reply in-hand. Persona posting, retry-with-feedback, and
  chunk-splitting are kept/reused (see §9).
  - **(F) Durability — at-most-once posting accepted.** A bridge crash mid-turn
    drops the in-flight reply. Honest caveat (round-1): calfcord has **no
    idempotency**, so re-mentioning **re-executes** the agent (re-billing,
    re-firing any tool side effects) — it does not replay the completed output;
    side-effecting tools need their own idempotency keys regardless. (Today's path
    is already at-most-once across a restart — `PendingWires` is in-memory, outbox
    consumes `auto_offset_reset=latest` — so F mainly removes intra-lifetime dedup,
    which the per-run lossless handle makes safe.)

### Change (MCP — C decided)
- Update handle usages to the public 0.12 API: `MCPToolboxRef` → `MCPToolbox`
  (`mcp/agent_select.py`, `agents/factory.py:85,542,551`); drop `strict`. Tool-name
  namespacing (`server__tool`) is read-time in calfkit — no calfcord change beyond
  display.
- **Delete `mcp/capability_read.py`** (it read the internal `calf.capabilities`
  topic): agents resolve MCP tools natively via the public handles. The CLI's live
  per-tool display can be **dropped, or optionally restored** via the now-public
  `client.mesh.get_tools()` (0.12.2 → `ToolboxInfo`/`ToolNodeInfo`/`ToolSpec`) — a
  sanctioned reader, no internal coupling.
- **Keep** `mcp/selector.py`, `mcp/config.py`, `mcp/config_write.py` (the
  `mcp.json` + frontmatter operator config calfkit does not provide). `mcp/runner.py`
  **stays**, rewritten to host the native `MCPToolboxNode`.

### Delete (control plane — `control_plane/` deleted in full)
With dep 1 shipped (0.12.2 mesh view), **both halves are now buildable**; the
two-wave split is kept only as a suggested *ordering*, not a gate:
- **Command channel** — `sink.py`, the `agent.{id}.control.in` topic,
  `apply_local_thinking_effort_override`, and the optimistic confirm/re-upsert dance
  — replaced by the bridge-owned per-call `model_settings` override map (C11). Also
  drop `first_reply.py` (round-1: it watched `discord.outbox`, which C6 removes, so it
  could never fire) **and** the init/onboarding wizard's first-reply step
  (`cli/init.py`, downgrades to a "try it yourself" hint), and cut the
  `control_plane.topics` import in `_provisioning.py`.
- **Roster/presence** — `state_consumer.py`, `publish.py`, `probe.py`, `builders.py`,
  `schema.py`, `topics.py`, `definition_ref.py`, the `agent.state` /
  `bridge.discovery` topics, and the runner's presence/departure lifecycle hooks.
  Agents already advertise their `AgentCard` on `calf.agents` automatically
  (calfkit-native, no opt-in); the bridge/CLI roster now reads
  `client.mesh.get_agents()` (0.12.2, §10), so this whole layer deletes.
- **C11 override map.** Must **persist** (a new additive table in the existing bridge
  SQLite `TranscriptStore`, with defined `NullTranscriptStore`-degraded behavior) —
  in-memory would silently revert on restart. It reaches only bridge-initiated calls:
  **both** native A2A consults *and* handoffs use the agent's own defaults (the
  consult `Call` and the handoff `TailCall` carry no overrides), and it cannot swap
  the model (a `model_settings` fragment only). **Issuance surface (round-3 S2):**
  keep the owner-gated Discord command, re-pointed to write the bridge map — e.g.
  `/set-thinking-effort <agent> <effort>` — with value validation, a persistence
  write, and a read into the per-call `model_settings`. Key granularity is per-agent
  (global), not per-channel.

### Change (agent identity — C8/C9/C10)
- `agents/definition.py`: drop `display_name`, `avatar_url`, `history_turns`, and
  `role` (always `assistant` once the router is gone). The `.md` frontmatter loses
  these fields; duplicate-`display_name` detection in `registry.py` is dropped
  (name is the unique key).
- **Wire `description` into the calfkit agent (round-2 MUST).** The factory must
  pass `Agent(description=definition.description)` — it currently does **not**, so
  every `AgentCard.description` would be `None` and both the roster and the
  `message_agent` peer directory would render blank. The Discord-slash `description`
  (≤100 chars) doubles as the AgentCard blurb (≤512).
- Persona is built bridge-side: `name = agent name`, `avatar_url = derive(name)`;
  `discord/persona.py` usage updated.
- Per-agent history trimming (`records[-history_turns:]`) is removed — note the
  slices live in `bridge/ingress.py` (not `history.py`); pass the fetched window
  (Discord cap, ~100) plus a global size backstop (§10).
- **Author-stamping source (round-3 M2 — corrects round-2).** Author resolution must
  **not** use the live-agents reader (an offline author's past turns would
  mis-attribute) — but the round-2 "stable on-disk `agents/*.md` set" is *also* wrong:
  in calfcord's distributed model the bridge shares no filesystem with the agents (and
  the wire source is being deleted), so it has no such set. Use the **persona-webhook
  identity on the message the bridge itself posted**: under C8 the webhook username
  *is* the agent name, and the bridge posts it through its own persona webhooks. Stamp
  `ModelResponse.name` from that webhook identity — match by `webhook_id` ∈ the
  **bridge's persona-webhook id set**, not by name-matching. **(round-4 S-3:** the
  persona sender must expose its created/marker-named webhook ids to the history
  fetcher; today `bridge/history.py` matches the webhook *display_name* via the
  now-deleted registry — rewire it to the id set.) Stamp `UserPromptPart.name` from the
  human display name. This is liveness-independent and can't mis-attribute a
  renamed/offline agent. The live-agents reader is for *roster/liveness only*.

---

## 8. Decisions resolved & the public/internal boundary

All **design** decisions are now resolved; §2–§7 reflect them. (A few *implementation*
choices are deferred to build time — flagged inline: the A2A anchor-message policy
§6.2, replay-hydration accept-or-rehome §9, `state.py` delete-vs-shell §10, the roster
fail-open/`reader_dead` handling §10.) This section retains the boundary rule and the
resolution log.

**Public-vs-internal boundary (the design rule).** calfkit 0.12.0 exports the
control-plane **substrate** as public and reusable (`ControlPlaneView`,
`advertises`, `ControlPlaneRecord`, `ControlPlaneStamp`, `ControlPlaneConfig` —
`calfkit/__init__.py:__all__`), but the **specific planes** are internal:
`AgentCard` (`calf.agents`) and `CapabilityRecord` (`calf.capabilities`) are
**unexported**, and there is **no public caller-side reader** for either topic.
Per the project rule (don't depend on calfkit internals; file an issue instead),
calfcord must not read those topics directly — but it *may* build its own plane on
the public substrate with its own record type.

- **D-ControlPlane → RESOLVED: delete `control_plane/`.** With persona/config
  dropped (C8–C10) and commands moved to per-call overrides (C11), the bridge's
  over-the-wire need collapses to **name + liveness (+ description)** — exactly the
  `AgentCard` every agent already advertises on `calf.agents` automatically. The
  roster is served by the **public live-agents reader** (calfkit dependency, §2);
  the agent-side presence plumbing and the command channel are deleted (§7). No
  calfcord control-plane record remains.

  *Glossary — what control_plane provided (now replaced).* **Roster** = which
  agents exist + are live → the public live-agents reader. **Commands** = runtime
  reconfig of a running agent (e.g. `set_thinking_effort`) → bridge-side per-call
  `model_settings` override (C11).

**Resolution log:** A-Handoff → adopt (C7); B-Sequencing → was gated on streaming + the live-agents reader (both since cleared — 0.12.1/0.12.2);
C-MCP `capability_read` → drop with the live display; D-ControlPlane → delete
(above); F-Durability → accept at-most-once; G-A2AState → stateless accepted;
C8–C11 → persona = name, avatar = seed(name), drop `history_turns`, per-call
settings overrides.

**Round-1 review decisions:** keep gating, now **3 deps** (§2), waiting for
sanctioned public APIs (deliberate cleanliness-over-speed; deps 1–2 are buildable
now but we won't couple to internals); C7 unbounded-handoff footgun **consciously
accepted** (verified no guard; track calfkit #251 / file an issue); `first_reply.py`
+ its wizard step **dropped**; `calfkit-tools` ceiling → **wait** for a
0.12-compatible release. The round-1 must-fixes are folded into §9 (behaviors) and
§10 (operational surface).

**Round-2 review decisions** *(historical — the "owner's word", "placeholder union",
and "stable on-disk set" framings below are superseded by the 0.12.1 + round-3 notes
that follow; kept for traceability)*: A2A stays **stream-based** (not
terminal-reconstruction) — the project owner confirms streaming will carry tool
results + agent messaging +
responses (§2 dep 2), resolving the round-2 concern that the placeholder event union
couldn't surface the peer reply. Folded in: wire `description` into `Agent(...)`
(else AgentCard/roster blank); **two distinct name sources** — liveness reader for
the roster, a stable on-disk set for author-stamping (§7/§9); corrected the C7
mitigation (both handoff loops are unbounded in v1; `self_retry_budget` is #251's
*proposed* feature, not shipped); A2A thread-anchoring key to specify (§6.2); bridge
can be a pure `Client`; roster reader must handle a no-auto-create broker; added a
phase plan + acceptance-criteria / `StepEvent`-schema requirement (§10). Issues
filed: [calfkit-sdk#301](https://github.com/calf-ai/calfkit-sdk/issues/301) (dep 1),
[calfkit-peripherals#12](https://github.com/calf-ai/calfkit-peripherals/issues/12)
(dep 3), [#251 comment](https://github.com/calf-ai/calfkit-sdk/issues/251#issuecomment-4825813737)
(handoff loop). **Convergence:** round 2's one critical is resolved here (owner's
streaming confirmation) and its must-fixes are folded in; a round 3 should confirm
no new criticals remain.

**Update (0.12.1 shipped, 2026-06-29)** *(superseded by the 0.12.2 update below — dep 1
has since shipped)*. Two gates cleared, verified against source:
**intermediate streaming shipped** (calfkit 0.12.1, #302) — so the stream-based A2A
projection is confirmed real, not owner's word (§2 dep 2), and live progress is
unblocked; **`calfkit-tools` 0.1.3** allows calfkit `<0.13`, unblocking C1 (now
targets 0.12.1). **Only dep 1 (the public live-agents reader) remains** — the
migration's sole gate. Also new in 0.12.1: structured exception harvest into
`ErrorReport.exception` (#298 / ADR-0024) — and `ErrorReport.origin_node_id` names
the faulting node, so a faulted handoff can post a **best-effort** persona (fall back
to a generic reply only when it's `None`; §6.1). Handoff loops remain unbounded (C7).

**Round-3 review (3 reviewers) — converged, fixes folded in.** All nine 0.12.1 claims
verified against shipped source (incl. nested-A2A observability via root-callback
routing — real, not assumption); **no CRITICAL**. Applied: rewrote the stale §5
"terminal-only / gated" callout (streaming shipped); **corrected the author-stamping
source** (M2 — persona-webhook identity on the message, not an on-disk set a
distributed bridge lacks); made the A2A/progress split a **stateful** `tool_call_id`
classifier, not a `kind` check (M1, §5.2); specified A2A reject/fault rendering +
peer-identity-from-request-`args` (M3, §6.2); split `control_plane` deletion into
**two waves** (S1, §7); specified the C11 issuance command + SQLite persistence (S2);
bridge inbox provisioning on Tansu + one-`stream()`-per-handle (S3/S5, §5); softened
the faulted-handoff persona note via `origin_node_id`. Stale text swept.

**Update (0.12.2 shipped, 2026-06-30) — ALL GATES CLEARED.** calfkit 0.12.2 ships the
**caller-side mesh view** (#307): `client.mesh.get_agents()` → `{name: AgentInfo(name,
description, last_seen)}` (public `Mesh`/`AgentInfo`/`MeshViewConfig`,
`client/mesh.py`), exactly the name + description + liveness the roster needs — dep 1
(#301) resolved. With deps 1–3 all shipped, the migration is **fully unblocked on
0.12.2**; `control_plane/` deletes in full (no Wave-B gate). Behavioral specifics
folded in: the mesh is **online-only** (so unknown/offline @mentions collapse, §9) and
opens naively → handle `MeshUnavailableError` on a not-yet-created `calf.agents` (§10);
`get_tools()` can optionally restore the CLI tool display via a public reader (§7).
**No remaining calfkit gate.**

**Round-4 review (3 reviewers).** All seven 0.12.2 mesh claims verified against shipped
source; no CRITICAL. Swept stale "still gated" text (§4/§5 callouts, §1/§7 "open
decisions in §8", §8 historical markers). Specified the newly-unblocked **roster path**
(§10): a mesh-backed cached name index replacing `AgentRegistry`; **fail-open** routing
on `MeshUnavailableError` (the mesh is only a pre-flight check); per-reason recovery
(`reader_dead` is permanent → degraded-roster mode); the idempotent-`agent start`
staleness window + the new `Probe` contract; the persona-webhook id set for
author-stamping (S-3); persona as a pure function (no roster). These are deferred
*implementation* choices, not design blockers — the spec is converged and buildable.

---

## 9. Behaviors to preserve (round-1 must-fixes)

Shipped behaviors NOT in the §3 contract that the spec under-specified; each must
be re-homed onto the `start()`/`stream()`/`result()` path before implementation.

- **Author-stamping for native POV projection.** calfkit decides self-vs-other by
  `ModelResponse.name == viewer`. The bridge rebuilds history from Discord, so it
  must stamp each historical agent turn `ModelResponse.name = <agent name>` and each
  human turn `UserPromptPart.name = <user>`. **Source (round-3 M2):** the
  persona-webhook identity on the message the bridge itself posted (match by
  `webhook_id`; under C8 the webhook username *is* the agent name) — *not* the
  live-agents reader, and *not* an on-disk set (unavailable to a distributed bridge;
  see §7). **Drop the `<author>` text prefix** in `project_history` to avoid
  double-prefixing (calfkit attributes at read time).
- **Replay hydration.** Prior tool calls are spliced back into history today
  (`ingress.py` `_build_replay_hydration` + `TranscriptStore`). Re-home (fetch → join
  store → name-stamped `ModelResponse`s carrying `ToolCallPart`/`ToolReturnPart` →
  `start()`), or explicitly accept the regression.
- **Transcript write + ⤵ expand toggle.** Re-home the per-turn delta write to the
  `result()` path (`InvocationResult.message_history` exists); keep the toggle
  reader, keyed on the new reply message id.
- **Retry-with-feedback.** The re-publish mechanism is deleted; re-express as a
  local `start()` retry with corrective `message_history` + bridge-side
  `model_settings`; re-pass memory deps.
- **Memory injection.** Keep shipping `_memory_prompt_deps()` on the new call path;
  the §5.2 sketch's `deps={"discord": …}` drops it.
- **Live progress + typing indicators.** Both ride `agent.steps` today; re-home onto
  the normalized step-event seam fed by the **shipped** 0.12.1 step events
  (`AgentMessageEvent`/`ToolCallEvent`/`ToolResultEvent`/`HandoffEvent`). No longer
  blocked — intermediate streaming shipped (dep 2).
- **Unknown / offline @mention.** Distinguish (a) no mention → unanswered (C2)
  from (b) an `@<name>` not in `client.mesh.get_agents()` → a user-facing, non-silent
  reply. **Note (0.12.2):** the mesh is **online-only**, and a distributed bridge has
  no "defined-but-offline" set, so *unknown* and *offline* collapse to one case —
  "no agent named X is online right now." Reply with that rather than the old
  `UnknownAgentMentionError` wording. The §5.2 sketch must not silently drop (b).
- **`result()` timeout.** Add a generous client-side `result(timeout=…)` to bound a
  parked coroutine when an agent never replies (the client-patience knob, distinct
  from the dropped A2A knob C5).

Minor calfkit-fact corrections (round-1): per-call `model_settings` **merges** over
ctor settings (not wholesale replace); `RunFailed` carries **no emitter** (faulted
handoff → generic error reply, no persona); stateless-consult citation is
`agent.py:438-444` (not `:331-332`).

---

## 10. Operational & implementation surface (round-1 must-fixes)

The spec is strong on runtime deletions but must also cover the operational half.

- **`extra="forbid"` + on-disk migration.** Dropping `display_name`/`avatar_url`/
  `history_turns`/`role` makes existing `agents/*.md`, seed templates, and the CLI
  field registry fail to parse. Add a one-time `.md` migration; update
  `cli/_fields.py`, `cli/_agents.py`, `cli/agent_inspect.py`.
- **Cross-package import breaks to fix** (outside the deletion list):
  `tools/runner.py` (drop `_RES_CLIENT`/`a2a_client`/reply-topic → plain tool
  Worker), `_provisioning.py` (control_plane.topics import), `cli/doctor.py`
  (`probe_live_roster` → live-agents reader, or drop the deep probe),
  `supervisor/roster.py` (`probe_live_roster` → reader; the cross-host duplicate
  `agent start` guard must work with the bridge down — note active-ping → passive
  heartbeat-staleness is a real liveness change), `tests/cli/conftest.py` (autouse
  import of `capability_read`).
- **Deploy/supervision.** Add the **`mcp` service** to both compose files (missing
  today); remove the `router` service / entry point / `install.sh` shim mapping /
  supervisor reserved-name + slot; move `CALFKIT_A2A_CHANNEL_NAME/_CATEGORY` from
  the **tools** service to the **bridge** and construct `A2AChannelResolver` there.
- **`tools` process.** After `private_chat`, `tools/runner.py` drops its `Client`/
  reply-topic/`a2a_client`; `ALL_TOOLS` keeps the ~11 `calfkit-tools` nodes.
- **`agents/state.py` / channels.** Name-addressing removes channel subscriptions;
  decide delete-vs-empty-shell for `state.py`/`AgentStateStore`/`state/agents/*.json`
  + bootstrap env vars. Confirm the per-node **private return topic**
  (`subscribe_topics[0]`) is native under name-addressing, else it stays.
- **Env vars.** Retire `CALFKIT_ROUTER_*`, `CALFKIT_TOOLS_TIMEOUT_SECONDS`; specify
  the C11 override-map **persistence** location; document the live-agents reader +
  caller surface reuse `CALF_HOST_URL` (no new secret).
- **Security (`docs/security.md`).** Tools host no longer needs `DISCORD_BOT_TOKEN`
  (blast-radius shrink); removing the `slash_target` gate lets any broker-writer
  invoke an agent via `agent.<name>.private.input` (defense-in-depth loss); the
  per-agent channel allowlist control is removed; roster-poisoning via forged
  `AgentCard`s.
- **History size backstop.** With `history_turns` gone, keep a single **global**
  cap (token/byte) so the ~100-message window + replay can't exceed the model
  context or the broker max message size.
- **Tests / docs / ADRs (CLAUDE.md mandate).** ~16 test files to delete, ~19 to
  rewrite, plus new coverage (`_handle_mention`/stream flow, A2A projector,
  name-addressing, native-handoff persona, C11 overrides). Docs: rewrite
  `architecture.md`, `a2a-threads.md`, `authoring-agents.md` (+ add native
  `peers`/`message_agent`/handoff authoring), `mcp-tools.md`, `configuration.md`,
  `distributed-deployment.md`, `security.md`; delete `ambient-routing.md`. New ADRs:
  at-most-once (F), stateless A2A (G), delete-router/ambient-unanswered (C2), delete
  control_plane (D), full-native A2A+handoff (C4/C7), persona=name + dropped fields
  (C8–C10); supersede ADR-0001/0002/0004/0005/0007.
- **Bridge process model (round-2).** With the outbox/state consumers gone and A2A
  driven off the per-run stream, the bridge can be a **pure `Client`** (no
  co-located Worker). Own the broker lifecycle explicitly on shutdown (no Worker
  owns it).
- **Roster via `client.mesh` (0.12.2).** The bridge + CLI read the roster from
  `await client.mesh.get_agents()` → `{name: AgentInfo(name, description, last_seen)}`
  (`client/mesh.py`); tune via `Client.connect(mesh_config=MeshViewConfig(stale_after=…))`.
  Lazy, caller-side, cached per-kind view (no Worker), torn down at `client.aclose()`.
  `get_tools()` is also available (§7 MCP note). The roster wiring needs these
  decisions (round-4):
  - **Name index replacing `AgentRegistry` (round-4 M-3).** The deleted
    `bridge/registry.py` was a *sync* index fed by `agent.state`; `get_agents()` is
    *async*. Replace it with a **short-TTL cached snapshot** of `get_agents()`,
    refreshed on a small loop (or on each turn), that the (sync) `@mention` path
    reads. **Persona needs no roster:** it's a pure function —
    `Persona(name=emitter_node_id, avatar=dicebear_avatar_url(name))` (C8/C9).
  - **Fail-open when the mesh is *unavailable* (round-4 M-1).** The mesh is only a
    pre-flight validity check; `start()` derives the topic from the name and does not
    consult it. On `MeshUnavailableError`, **skip the validity check and attempt the
    call** (bounded by the §9 `result(timeout=…)`), rather than answering "no agent
    online" for a routable mention. Reserve the §9 collapse reply for the
    mesh-*available*-but-absent case.
  - **Per-reason recovery (round-4 M-2).** `establishing` / `open_failed` self-heal on
    the next read (retry / empty roster). **`reader_dead` is permanent for the
    `Client`'s lifetime** (no `Mesh.reset`; recovery needs a fresh `Client`, which
    would tear down in-flight runs) → enter a **degraded-roster mode**: alert + keep
    fail-open routing; do not silently treat it as "empty roster."
  - **Idempotent `agent start` + `ps` under staleness (round-4 S-1/S-2).** The mesh is
    online-only with a ~`3×heartbeat` (~90 s default) stale window; a *crashed* agent
    stays "online" until it lapses (graceful stops tombstone immediately), so the
    cross-host duplicate guard can briefly refuse a restart. Pick a `stale_after` for
    the guard and document the changed semantics. Redefine the old `Probe` seam
    (`Callable[[str], Awaitable[list[AgentDefinition]]]`) as
    `Callable[[str], Awaitable[set[str]]]` (names) over `get_agents()`; update
    `supervisor/roster.py`, `cli/doctor.py` (or drop its deep probe).
- **Phase plan (updated 2026-06-30 — all gates shipped).** *Prep:* delete router +
  `/task` (C2/C3), the on-disk `.md` migration + CLI field-registry updates (C8–C10),
  re-home the standalone `A2AChannelResolver`. *Build:* the `calfkit~=0.12.2` bump,
  the caller-surface bridge, native A2A + handoff, live progress + A2A projection,
  C11 command removal, and the `control_plane`/roster deletion onto `client.mesh` —
  **all buildable now.** *Internal ordering (correctness, not gating):* don't delete
  the roster/`state_consumer` until the bridge reads `client.mesh`; don't delete
  `outbox`/`pending_wires` until the `start()/result()` path lands.
- **Acceptance criteria + `StepEvent` schema (TDD).** Give each §3 contract item and
  each §9 behavior a testable Given/When/Then. The upstream step events are now
  **concrete public types** (`calfkit.client` `AgentMessageEvent`/`ToolCallEvent`/
  `ToolResultEvent`/`HandoffEvent`, each with `correlation_id`/`depth`/`emitter`), so
  the bridge's normalized `StepEvent` maps from them and a fake source can emit the
  real types — no hypothetical schema needed; the stream path is TDD-able now.
- **Public-surface-only invariant (audit, 2026-06-30).** Target: calfcord uses
  **only calfkit public surfaces** — no hand-rolled control plane, no raw
  `client._connection` / FastStream pub-sub, no direct `aiokafka`. The migration
  achieves this for the **control-plane + messaging layers** (`control_plane/`
  deleted; the bridge's `client._connection` publishers/subscribers and all
  `ConsumerNode`s gone; roster via `client.mesh`; streaming via `handle.stream()`;
  topic provisioning via the public `TopicProvisioner`/`ProvisioningConfig`).
  Residual couplings, resolved:
  - **`health/check.py` → DROP (decision).** Remove the `aiokafka.admin.AIOKafkaAdminClient`
    broker-readiness probe entirely in this migration — it is the only raw-`aiokafka`
    touchpoint. Broker readiness then relies on the broker's own readiness + the
    `Client`'s fail-fast on first dispatch, not an app-side admin probe; adjust the
    Process Compose broker-readiness gate / `doctor` broker check that consumed it.
  - **`calfkit._vendor.pydantic_ai.messages` imports → ACCEPTED (decision).** The
    message vocabulary (`ModelMessage`, `ModelResponse`, `UserPromptPart`,
    `ToolCallPart`, `ToolReturnPart`, `ModelMessagesTypeAdapter`) is pervasive in the
    bridge (history build, author-stamping, transcript delta, replay) and **forced**:
    the public `InvocationResult.message_history` is `list[ModelMessage]`, yet calfkit
    re-exports only `calfkit.models.payload`'s own parts. Accepted as a known,
    unavoidable calfkit packaging gap — no issue filed.
  - **`providers/codex/model_client.py` → ACCEPTED (decision).** Its
    `calfkit._vendor.pydantic_ai.models.openai` coupling (a custom OpenAI-compatible
    model client) is a deliberate provider-layer exception — no issue filed.
  - **Cosmetic:** import public types from the top-level package, not internal
    submodules — `LifecycleContext`/`ResourceSetupContext`/`ServingContext`,
    `ToolNodeDef`, `consumer` are all re-exported from `calfkit`.

  **Net:** after the migration, the app uses **only calfkit public surfaces** for the
  control-plane, messaging, pub-sub, and provisioning layers, with exactly **two
  accepted `_vendor.pydantic_ai` exceptions** (the forced message vocabulary; the
  Codex model client). No hand-rolled control plane, no `client._connection`, no raw
  `aiokafka`.

---

## 11. References

**calfkit 0.12.2** — agent mesh: `agent-peers.md`, `designs/agent-mesh-spec.md`,
ADR-0015/0016/0017/0019; caller surface: `designs/client-caller-surface-spec.md`,
`client/caller.py`, `client/gateway.py`, `client/hub.py`, `client/events.py`,
`models/node_result.py`, ADR-0009/0022/0023; **step streaming (0.12.1, #302):**
`models/step.py`, `client/events.py` (`RunEvent` union), `nodes/base.py:1786-1815`
(step emission → root caller), ADR-0026; **caller-side mesh view (0.12.2, #307):**
`client/mesh.py` (`Mesh`, `AgentInfo`, `ToolInfo`, `MeshViewConfig`), `Client.mesh`
(`client/caller.py:211`), `MeshUnavailableError`; projection:
`designs/agent-pov-projection.md`, `nodes/_projection.py`; MCP/control-plane:
ADR-0010/0011/0012/0013/0014/0018, `models/capability.py`, `controlplane/`.

**calfcord (current)** — `bridge/` (gateway, ingress, normalizer, history,
outbox, pending_wires, egress, synthesized, steps*), `agents/` (factory, runner,
gates, phonebook, peer_roster, routing), `tools/private_chat.py`, `router/`,
`control_plane/`, `mcp/`, `topics.py`.

## Follow-ups

- **`doctor` config-drift / deep-probe removed.** The `calfcord doctor` runtime
  section's deep control-plane probe and local↔org drift check were removed in the
  0.12 migration (they were fed by the now-deleted `control_plane`; production never
  wired a probe, so both were unreachable). The runtime section now reports daemon
  liveness plus a pointer to `calfcord status`. A follow-up could rewire drift onto
  the native calfkit mesh view (`client.mesh.get_agents`) to compare the online mesh
  roster against the locally-running processes.

- **D-11 revisited — bridge no longer pre-starts the broker.** D-11 added an eager
  `async with client.events(terminal_only=True): pass` at bridge startup to force the
  broker up (provisioning the durable inbox) before Discord traffic could trigger a
  reply. On review this is redundant: calfkit's `AgentGateway.start()` awaits
  `_ensure_started()` (→ `broker.start()`, which provisions the inbox and starts its
  reply subscriber consuming under `provisioning=PROVISIONING`) **before**
  `_publish_call`, so the first `client.agent(name).start(...)` self-provisions ahead
  of the first request — a reply can't land on an unprovisioned/unconsumed inbox.
  Nothing else publishes before the first mention, and the mesh roster read opens its
  own independent reader. The bridge pre-start was removed.

  The same reasoning retired the eager-start in the two CLI probes
  (`_wait_for_agent_online`, `_probe_live_roster`). There is nothing to pre-flight: the
  real operation — `client.mesh.get_agents()` — already **raises at call time** if the
  broker/mesh can't be reached. So the probes just do the read and let it raise:
  `_probe_live_roster` lets a `MeshUnavailableError` (or timeout) propagate to the
  caller's `except Exception` degradation ("broker unreachable; …") — a *readable* but
  empty roster still returns `[]`; `_wait_for_agent_online`'s poll loop swallows the
  transient `MeshUnavailableError` and retries until the agent appears or the window
  elapses. No pre-flight probe (neither the `events(terminal_only=True)` side-effect
  trick nor a direct `broker.start()`) is used anywhere — all three eager-start hacks
  are gone. Trade-off accepted: on a brand-new org the very first `agent start` / `ps`
  (before any agent has created the `calf.agents` topic) degrades with "broker
  unreachable" rather than showing an empty roster — a one-time cosmetic wart on a path
  that proceeds correctly. Verified by source analysis against calfkit 0.12.3; a live
  no-auto-create-broker (Tansu) cold-start smoke test is the recommended final
  confirmation.
