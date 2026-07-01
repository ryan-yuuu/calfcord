# calfkit 0.12.2 migration — implementation plan

- **Status:** Draft — revised after a 3-reviewer deep review (sequencing,
  completeness, tests/API). All calfkit 0.12.2 API assumptions verified against
  source.
- **Date:** 2026-06-30
- **Derives from:** `docs/design/calfkit-012-migration.md` (decisions **C1–C11**, the
  §3 preserved-behavior contract, §7 deletions, §9 behaviors, §10 operational +
  public-surface invariant). This doc is the *how/order/tests*; the spec is the
  *what/why*.
- **Pre-reqs (shipped/verified):** calfkit **0.12.2** (mesh #307, streaming #302),
  **calfkit-tools 0.1.3** (`calfkit<0.13`). No remaining calfkit gate.

---

## 0. Conventions

- **TDD (mandatory).** Tests first (red→green→refactor); `/test-driven-development`;
  100% coverage on changed files via `/pytest-coverage`.
- **`uv` only** for deps. Conventional commits. Ruff clean on changed files.
- **Worktree.** The cutover (Phase B) runs on a dedicated branch in a fresh worktree
  off `main`; Phase A and Phase C are their own PRs.
- **Per step:** *Goal · Prereqs · Changes (add/mod/del) · Approach & pinned decisions
  · Tests-first (Given/When/Then) · Acceptance · Risk.*

---

## 1. Sequencing & dependency analysis (read first)

**The bump is a hard cutover.** calfkit 0.10→0.12 is multiple breaking changes; every
calfkit call site moves at once. Structure: **Phase A (subtractive prep on 0.10,
ships green) → Phase B (the coordinated 0.12.2 cutover on a branch) → Phase C
(docs/ADRs).**

**Phase B reality (confirmed by review):** the messaging layer is **one indivisible
landing**. From the bump (B1) until the mesh-backed roster lands (B3c), the bridge is
non-functional regardless of sub-step boundaries (the registry — which `@mention`
resolution depends on — is mid-rewrite). So **B1→B3c land together** on the branch,
sequenced internally for build-up and validated by per-step fakes; an **integration
smoke runs right after B3c**; then **B4 (control-plane + C11), B4.5 (field drop), B5
(MCP), B6 (deploy/ops) are separable-after**, each landing against a green messaging
layer. B7 is the final gate.

**Hard import-timing rules (violating any leaves the tree un-importable mid-branch —
all are module-level imports verified in source):**

- **R1.** `agents/factory.py` and `bridge/gateway.py` and `bridge/ingress.py` import
  router symbols at module top (`factory.py:85,95,101`; `gateway.py:71,91`;
  `ingress.py:90`). **A1 must edit all three** (not just `registry.py`), and reseed
  the production registry as `AgentRegistry([])` (`gateway.py:816`).
- **R2.** `pending_wires` is read by **both** the outbox consumer **and** the steps
  consumer (`steps.py:178,807,821`; `gateway.py:65,887,910`). **Delete `pending_wires`
  only when the *last* old `ConsumerNode` is gone (B3b)** — not in B3a.
- **R3.** `control_plane/` has module-level importers beyond runner/_provisioning:
  `supervisor/roster.py:49` (probe), `cli/init.py:549` (first_reply), `gateway.py:942`
  (`register_state_consumer`), `bridge/slash.py:48-49` (publish/schema). **B4 must cut
  *all* of them in the same step it deletes the package.**
- **R4.** Dropping the `AgentDefinition` fields (C8–C10, `extra="forbid"`) forces
  fixing/removing **every reader**. Readers span modules deleted across B2
  (`phonebook`, `private_chat`, `peer_roster`) and B4 (`control_plane/builders`), plus
  live readers (persona, registry, normalizer, history, slash, gateway, ingress, CLI).
  → **Do not drop the fields in B1.** Switch readers off the fields incrementally
  through B2/B3/B4, then **drop the fields + run the `.md` migration as B4.5**, once
  nothing reads them.
- **R5.** The **agents↔bridge name-addressing flip is atomic** (B2 drops channel-topic
  subs+gates exactly as B3a stops publishing to channel topics and switches to
  `client.agent(name)`); they co-land.

---

## 2. Test strategy

- **Unit (fast, no broker).** The step/mesh types are public + frozen → fakes emit
  the real types, **with these gotchas (verified):**
  - **`FakeMesh.get_agents()`** → `{name: AgentInfo(name, description, last_seen=<aware utc>)}`;
    raise `MeshUnavailableError(msg, reason=…)`. Both construct trivially. **`client.mesh`
    cannot run under `TestKafkaBroker`** (it opens its own `ControlPlaneView` reader) —
    FakeMesh is the *only* unit path; real roster I/O needs Tansu.
  - **`FakeHandle`**: `stream()` is an async-gen yielding scripted step events then
    ending; **`result()` returns a hand-built `InvocationResult(output=…,
    state=State(message_history=[…]), correlation_id=…, emitter_node_id="B")`** — do
    **not** try to fabricate a `RunCompleted` for the success terminal (it requires a
    real `Envelope`, and its field is **`agent`**, not `emitter`; persona comes from
    `result().emitter_node_id`, not the streamed terminal). For a *failure*,
    `RunFailed(report, correlation_id)` is constructible and `result()` raises
    `NodeFaultError`. Every surface step event **requires `frame_id`** (+ `correlation_id`,
    `depth`, `emitter`) — fake builders must set them.
- **Integration.** Caller surface + streaming round-trip **work under
  `TestKafkaBroker`** with a co-located agent `Worker` (pattern:
  calfkit `tests/test_step_emission_integration.py`). **Roster/mesh + topic
  provisioning = real Tansu only.** The **§5 contract is the integration acceptance
  suite**.
- **Coverage.** 100% on changed files; branch coverage on the error arms (every
  `MeshUnavailableError` reason, `RunFailed`/no-emitter, reject **and faulted** consult,
  concurrent multi-consult, retry, timeout).

---

## 3. Pinned build-time decisions

- **D-1 · A2A anchoring.** One thread per top-level **`correlation_id`** (the whole
  run-tree shares it — verified). Anchor message = the first `message_agent` request
  in that run (caller persona); reuse for the run. Pair reply↔request by
  **`tool_call_id`**. Peer identity = request `args["name"]`; happy reply confirms via
  `emitter`.
- **D-2 · A2A reply rendering (corrected).** Happy reply (`ToolResultEvent`,
  `emitter`/`name`=peer) → post as peer. **Rejected** consult (`name=="message_agent"`,
  `emitter`=caller, `is_error=True`) → system/error note in the thread. **Faulted**
  peer → **a genuine peer fault escalates and faults the *whole top-level run*** (no
  `ToolResultEvent`; the terminal is `RunFailed`, `result()` raises `NodeFaultError`).
  So a faulted consult makes the **human turn fail** (the B3a generic error reply)
  *and* the A2A projector must synthesize a failure note from the recorded
  `ToolCallEvent` on the stream-end/`RunFailed` path. (Document: a faulted consult
  fails the turn — it is not a localized note.)
- **D-3 · Author-stamping (corrected).** Stamp `ModelResponse.name` from the
  **persona-webhook identity** on the message the bridge posted. The history fetcher
  must **discover the channel's webhooks per-channel by marker name** (list the
  channel's webhooks, filter by the persona marker name + app-owner id) — **not** a
  lazily-populated process-lifetime cache (the sender only learns its webhook on first
  send, so a cold channel would mis-attribute every prior agent post). Human turns →
  `UserPromptPart.name` (calfkit's `start(author=…)` already sets this). Rewire
  **`history.py`** (load-bearing: `author_agent_id` drives POV self-detection +
  replay). **Delete** the `normalizer.py` `by_display_name` author lookup — it is dead
  after A1 (ambient filter) + B2 (self-gate) remove its only consumers.
- **D-4 · Replay hydration → KEEP.** `start()` path: Discord history → join
  `TranscriptStore` deltas → name-stamped self `ModelResponse`s carrying
  `ToolCallPart`/`ToolReturnPart` → `message_history`. Keep a byte bound (envelope <
  broker max message size).
- **D-5 · `agents/state.py` → DELETE** (with B2): channels are gone under
  name-addressing. Delete `state.py` + `AgentStateStore` + `state/agents/*.json` + the
  bootstrap env vars; fix importers `agents/__init__.py:26` (drop the re-exports) and
  `agents/factory.py:96` (drop the build-signature params). Per-node return inbox is
  native (verified) — nothing replaces it.
- **D-6 · Roster availability.** Mesh = pre-flight check only. On
  `MeshUnavailableError`: **fail-open** (skip the check, attempt the call, bound with
  `result(timeout=…)`). `establishing`/`open_failed` → retry / empty roster;
  **`reader_dead` is permanent for the `Client`** (no `Mesh.reset` — verified) →
  **degraded-roster mode** (alert + keep fail-open routing).
- **D-7 · Roster name index.** A bridge-owned **short-TTL cached snapshot** of
  `client.mesh.get_agents()` (it is **async**; the `@mention` path is sync → caching is
  *required*), refreshed on a small loop. **Persona is a pure fn** —
  `Persona(name=emitter, avatar=dicebear_avatar_url(emitter))` (no roster).
- **D-8 · C11 settings.** Re-point the **existing `/thinking-effort <agent> <effort>`**
  owner command (keep the name — `bridge/slash.py:54`) to write a **new additive
  `agent_overrides` table** in the SQLite `TranscriptStore` (define
  `NullTranscriptStore`-degraded behavior); the bridge reads it into per-call
  `model_settings` (reuse `agents/thinking.py:build_model_settings`; remove its
  agent-side tier-3 override path). Bridge-initiated calls only (consults + handoffs
  use agent defaults). Per-agent (global).
- **D-9 · Health probe → DROP only the BROKER arm.** Remove `health/check.py`'s
  `aiokafka` broker probe (`default_broker_probe`/`BrokerProbe`) — the only
  raw-`aiokafka` touchpoint — and its consumers: `supervisor/lifecycle.py:43,413`,
  `cli/main.py:40` (the `_healthcheck broker` exec path `:512/:531`),
  `supervisor/compose.py:211` (broker exec gate). **Keep** the bridge-heartbeat arm +
  `healthcheck("bridge")`. Broker readiness then = the broker's own readiness + the
  `Client` fail-fast on first dispatch.
- **D-10 · MCP CLI tool display → DROP** (delete `capability_read`). Optional later
  restore via public `client.mesh.get_tools()`; not in scope.
- **D-11 · Bridge inbox provisioning (NEW — review MUST-FIX).** `Client.connect(...)`
  defaults to a **no-op `provisioning` (`enabled=False`) and an *ephemeral* inbox**.
  On Tansu the bridge **must** pass `provisioning=ProvisioningConfig(enabled=True,
  replication_factor=<prod>)` **and** a durable `inbox_topic="<stable>"`, or terminal
  replies *and* step messages silently never arrive. The connecting principal needs
  `CreateTopics` (else broker start fails loud with `MissingTopicsError`); otherwise
  pre-create the inbox + `calf.agents` + `calf.capabilities` out-of-band.

---

## 3.12 · Review resolutions (pre-implementation, 2026-06-29) — supersede pinned decisions where noted

Confirmed with the owner in the pre-implementation review, after verifying the 0.12.2 wheel
and the current tree. Where these conflict with a pinned decision above, **these win**.

- **R-A1 (provider gap → provider-blind union; refines D-8).** Deleting the registry removes the
  bridge's only source of an agent's `provider`; the public mesh (`AgentInfo` =
  name/description/last_seen) cannot supply it, and `model_settings` is forwarded **raw** and is
  **provider-specific** (`anthropic_thinking` dict vs `openai_reasoning_effort` str) with no generic
  effort abstraction (verified). Resolution: the bridge stays **provider-blind** by emitting a
  **union** `model_settings` carrying **both** provider keys — each model client reads only its own
  via `model_settings.get(...)` and ignores the foreign key (`ModelSettings` is `total=False`). The
  C11 store is an in-memory `{agent → ThinkingEffort}` map **hydrated from the persisted SQLite
  `agent_overrides` table** (D-8 persistence kept). `agents/thinking.py` gains a provider-blind union
  builder; the bridge never needs the provider and never reads agent files.
- **R-A2 (routing fail-fast; supersedes D-6 fail-open).** Parse `@mentions` in order; invoke the
  first the mesh shows **online**; none online → user-facing reply; **mesh unavailable → fail-fast
  user-facing reply too** (NOT fail-open). `reader_dead` is permanent (no `Mesh.reset`) → the bridge
  rejects mentions until restart; emit a loud operator alert on `reader_dead`.
- **R-A3 (author-stamping simplification; supersedes D-3 / §7-S-3).** Drop the webhook-id-set /
  rotation / per-channel-cache machinery. Mechanism: a **bot-owned** persona-webhook message →
  `ModelResponse.name = message.author.display_name` (persona username == agent name under C8); a
  normal message → human `UserPromptPart.name`. The only ownership check distinguishes the bot's
  persona webhooks from a third-party webhook (the persona sender already tracks its own). calfkit's
  POV projection re-roles from `name`.
- **R-A4 (memory always-ship; refines §9 memory bullet).** The `any(spec.memory)` registry gate is
  gone; **always ship the memory-prompt template** on bridge-originated calls (non-memory agents
  ignore it). Verified: `deps` propagate to native `message_agent` peers and handoff targets, so
  memory still reaches them — no A2A regression.
- **R-A5 (no global history ceiling; refines §10 history backstop).** Drop `history_turns` and add
  **no** global token/byte cap; `message_history` is whatever the Discord fetch returns, used
  directly. Keep the existing `REPLAY_TOOL_RETURN_MAX_CHARS` replay truncation (it already bounds the
  one realistic envelope blow-up).
- **R-A6 (stale_after = ~90s).** Use the calfkit default (90s = 3×30s heartbeat) for both `agent ps`
  and the duplicate-start guard; graceful stops tombstone immediately, so only a crash-restart is
  gated.
- **R-A7 (completeness corrections to fold in).** R1–R5 miss ≥2 module-level importers
  (`bridge/gateway.py:85` `control_plane.publish`, `agents/__init__.py:23` `gates`) plus
  `agents/loader.py:43` (`role`) — re-sweep every importer before each deletion rather than trusting
  R1–R5 verbatim. Test deletes (~32) and rewrites (~45-50) run ~2× the plan's estimate (the whole
  `tests/control_plane/` dir is omitted from the count); `FakeHandle`/`FakeMesh`/`TestKafkaBroker`
  infra is net-new.

---

## 4. Phases & steps

### Phase A — Subtractive prep (calfkit 0.10; ships green; own PR)

#### A1 · Delete router + `/task`
- **Goal:** ambient unanswered; `@mention`+slash unchanged on 0.10; `/task` gone; the
  tree still imports + tests green.
- **Delete:** `router/`, `ambient_routing.py`, `agents/routing.py`,
  `bridge/synthesized.py`, `cli/router_config.py`; the ambient path in `ingress.py`;
  `AMBIENT_INGRESS_TOPIC`/`SYNTHESIZED_INGRESS_TOPIC`/`ROUTING_DECISIONS_TOPIC`; the
  router service (compose ×2) + `calfkit-router` entry point + `supervisor/compose.py`
  reserved-name/slot + `cli/deploy.py` router + `scripts/install.sh` shim;
  `docs/ambient-routing.md`; `gateway.py` `_maybe_handle_task`/`_parse_task_command`;
  `normalizer.py` `normalize_task`.
- **Modify (R1 — the completeness fix):**
  - `agents/factory.py` — drop `from …routing import …` (`:95`) + the
    `AMBIENT_INGRESS_TOPIC` import (`:101`); remove `_build_router_node` (`:444`), the
    `role=="router"` dispatch (`:363`), `ToolOutput(RoutingDecision)` (`:510`).
  - `bridge/gateway.py` — drop `build_synthesized_consumer` import+wiring (`:71,897`)
    and `build_router_definition` import (`:91`); reseed `AgentRegistry([])` (`:816`).
  - `bridge/ingress.py` — drop the `build_router_temp_instructions` import (`:90`).
  - `bridge/registry.py` — drop the router seed/`router()`/multi-router/role branches;
    `cli/main.py` + `cli/explain.py` — drop router verbs/catalog.
- **Tests-first:** ambient message → dropped (no publish); `@agent` → routes (existing
  slash path); `/task …` → treated as ordinary message. Delete the ~3,500 LOC router
  tests; port fixtures.
- **Acceptance:** `python -m compileall src/` clean **and** all five entrypoints import
  (`calfcord.bridge.gateway`, `agents.runner`, `tools.runner`, `mcp.runner`,
  `cli.main`) — *not* `import calfcord` (the package `__init__` is a docstring only);
  full suite green on 0.10; ambient silent; `@mention`+slash unaffected.
- **Risk:** low (subtractive). Rollback = revert PR.

---

### Phase B — The 0.12.2 cutover (branch). B1→B3c indivisible; smoke after B3c; B4/B4.5/B5/B6 separable.

#### B1 · Bump + forced API fixes + `description=` wiring (no field drop)
- **Bump:** `uv add 'calfkit~=0.12.2' 'calfkit-tools>=0.1.3'`; relock.
- **Forced fixes:** `MCPToolboxRef`→`MCPToolbox` (import from `calfkit.mcp`; drop
  `strict`) in `mcp/agent_select.py:25` + `agents/factory.py:85`; move
  `client.send/execute` call sites to the `AgentGateway` caller surface (these are
  runtime, fixed in B2/B3 — here just unbreak imports).
- **Wire `description=`:** `agents/factory.py` `Agent(..., description=definition.description)`
  (else every `AgentCard.description` is `None` → blank roster + blank `message_agent`
  directory).
- **Acceptance:** entrypoints import on 0.12.2; identity tests still pass (fields not
  yet dropped). Bridge/agent suites red until B3 — expected on the branch.

#### B2 · Agents: name-addressing + native A2A/handoff + native advertisement *(co-lands with B3a — R5)*
- **Modify `agents/factory.py`:** `subscribe_topics=[]`; remove gate installs; remove
  `publish_topic=AGENT_STEPS_TOPIC`; build `peers=[Messaging(...)/Handoff(...)]` (C4/C7);
  drop the `state.py` build params (D-5); switch persona construction off
  `display_name`/`avatar_url` → `persona_for(name)`.
- **Modify `agents/runner.py`:** delete control-plane lifecycle hooks + channels
  bootstrap (D-5). (Agent advertises `AgentCard` automatically.)
- **Delete:** `agents/gates.py`; `tools/private_chat.py` transport (+ from `ALL_TOOLS`);
  `agents/phonebook.py`; `agents/peer_roster.py`; **`agents/state.py` + `AgentStateStore`
  + `state/agents/*.json`** (D-5) and the `agents/__init__.py:26` re-exports.
- **Modify `tools/runner.py`:** drop `Client`/reply-topic/`a2a_client` → plain
  tool-hosting `Worker` (keeps the ~11 `calfkit-tools` nodes).
- **Tests-first:** factory builds an `Agent` with empty `subscribe_topics`,
  `description` set, `peers` populated, no gates/steps-mirror; peer-less agent has no
  `message_agent` tool; **a name-addressed agent still receives its own tool `Call`/
  `TailCall` returns** (native return inbox — guards the dropped `subscribe_topics[0]`).
- **Acceptance:** agent boots, advertises (assert via `client.mesh.get_agents()` in an
  integration test), reachable by name; A2A consult round-trips (integration).

#### B3a · Bridge reply path (caller surface) *(co-lands with B2)*
- **Add `CalfcordBridge`** (this is the **`bridge/gateway.py` rewrite** — the embedded
  `Worker` becomes a pure `Client`): one `Client.connect(server_urls=CALF_HOST_URL,
  provisioning=ProvisioningConfig(enabled=True, …), inbox_topic="<durable>",
  mesh_config=…)` (**D-11**); per-`@mention` task: `start(prompt, message_history=…,
  deps={"discord":…, **memory_deps}, author=…, model_settings=<C11 override>)` →
  iterate `stream()` (B3b) → `await handle.result(timeout=…)`; success → post
  `result.output` as `persona_for(result.emitter_node_id)`; `RunFailed` → generic
  error reply, using `ErrorReport.origin_node_id` as a best-effort persona when present
  (else generic); **retry-with-feedback** = re-`start()` with corrective
  `message_history` + `model_settings` + memory deps.
- **Delete:** the `bridge/outbox.py` reply *consumer*. **Keep/re-home:** persona
  posting, retry-with-feedback helpers, chunk-split, reply-context/thread routing.
- **Tests-first (FakeHandle):** post-handoff terminal `emitter_node_id="B"` → posts as
  **B**; `RunFailed` with `origin_node_id` → that persona; `RunFailed` without → generic;
  oversized → chunk-split; post-failure → retry re-`start()`; **memory deps ride every
  `start()` and the retry**; unresponsive agent → `timeout` → user-facing reply; (F)
  crash mid-turn → at-most-once accepted.

#### B3b · Stream drain (live progress + A2A projection)
- **Add** `A2ADispatcher` (D-1/D-2): one `stream()` drain loop (one per handle) records
  `message_agent` `ToolCallEvent.tool_call_id`s, routes matching `ToolResultEvent` +
  `HandoffEvent` to the A2A projector (re-homed `A2AChannelResolver` + persona webhooks,
  anchored per `correlation_id`); else → live-progress renderer.
- **Delete:** `bridge/steps.py` consumer + the `publish_topic` mirror;
  `bridge/steps_state.py`; **`bridge/pending_wires.py`** (R2 — last old consumer gone
  here); `bridge/__init__.py` re-exports of the deleted consumers. **Keep/re-home:** the
  step *renderers* (onto the normalized `StepEvent`), the ⤵ toggle + `TranscriptStore`
  write (now from `result().message_history`), typing indicators (off the seam).
- **Tests-first (FakeHandle):** A→B consult renders request=A, reply=B, one thread;
  **rejected** consult (`is_error`, emitter=caller) → system note; **faulted** consult
  → terminal `RunFailed` *and* a synthesized A2A failure note from the dangling
  `tool_call_id` (D-2); **A consults B and C in one hop** (two outstanding ids,
  interleaved replies → each under the right peer); **nested** B→C (`depth=2`) → same
  thread; plain tool call/return → live progress, not A2A; `HandoffEvent` → handoff note.

#### B3c · Roster + history + author-stamping
- **Add:** the mesh-backed cached name index (D-7); fail-open + per-reason handling
  (D-6); history build emitting one author-stamped canonical `message_history` (D-3,
  per-channel webhook discovery; **delete `project_history`** — native POV); D-4 replay
  hydration; keep thread-starter recovery + `/clear`.
- **Delete:** `bridge/registry.py`'s `agent.state` projection + the registry as name
  source; `gateway.py:942` `register_state_consumer` wiring; the `normalizer.py:243`
  author lookup (dead — D-3) + remaining `role` usages (`normalizer.py:217`,
  `slash.py`, `gateway.py:670`).
- **Tests-first (FakeMesh):** online mention → routes; absent → "no agent named X
  online"; `open_failed` → **fail-open** (attempts call); `establishing` → retry /
  fail-open (self-heals same client); `reader_dead` → degraded mode (alert, still
  routes); offline-authored historical turn still attributes (webhook-id, mesh-
  independent); per-thread vs per-channel scoping preserved; thread-starter + `/clear`
  survive.
- **→ Integration smoke (subset of §5: routing, history, A2A) on real Tansu** before
  B4/B5/B6.

#### B4 · control_plane deletion + C11
- **Delete `control_plane/` in full** and cut **all** module-level importers in the
  same step (R3): `agents/runner.py`, `_provisioning.py`, `bridge/gateway.py`
  (`register_state_consumer`), `supervisor/roster.py:49` (→ mesh, D-6/D-7),
  `cli/doctor.py:447` (→ mesh or drop deep probe), **`cli/init.py:549`** (drop the
  first-reply wizard step §7 + the `DISCORD_DEFAULT_CHANNEL_ID` writer `:346`, D-5),
  **`bridge/slash.py:48-49`**.
- **Add (C11/D-8):** rewrite `bridge/slash.py`'s `/thinking-effort` to write the SQLite
  `agent_overrides` table; reuse `agents/thinking.py:build_model_settings` bridge-side
  and remove its agent-side tier-3 path.
- **Tests-first:** `/thinking-effort scribe high` persists + survives bridge restart +
  applies on the next bridge-initiated call to scribe; an A2A consult to scribe uses
  its **default**; DB-unavailable → graceful.

#### B4.5 · Drop the `AgentDefinition` fields + `.md` migration (R4 — after all readers gone)
- **Modify `agents/definition.py`:** drop `display_name`/`avatar_url`/`history_turns`/
  `role` (keep `extra="forbid"`). Update the remaining CLI readers
  (`cli/_fields.py`/`_agents.py`/`agent_inspect.py`).
- **On-disk migration:** one-time migrator for `agents/*.md` + templates + bundled seeds
  (strip the four fields); ship as a `calfcord` migration command / doctor auto-fix; run
  in CI fixture setup.
- **Tests-first:** post-migration `.md` parses; pre-migration `.md` rejected → migrator
  fixes; CLI `agent create/show/edit` round-trips without the fields. Grep gate: no
  `display_name|avatar_url|history_turns|\brole\b` reader remains.

#### B5 · MCP
- **Delete:** `mcp/capability_read.py` + the CLI live tool display (D-10); fix
  `tests/cli/conftest.py` autouse import + `cli/agent_tools.py`.
- **Modify:** `mcp/runner.py` → host the native `MCPToolboxNode`; keep
  `selector.py`/`config.py`/`config_write.py`.
- **Tests-first:** `mcp.json` → `MCPToolbox` handles; an agent picks up a toolbox's
  tools at runtime (integration, namespaced `server__tool`).

#### B6 · Deploy / supervisor / CLI / provisioning / health
- **Compose:** add the **`mcp` service** (missing in both files today); remove all
  router remnants; **move `CALFKIT_A2A_CHANNEL_NAME/_CATEGORY` from the tools service to
  the bridge** (and construct `A2AChannelResolver` bridge-side); **remove
  `DISCORD_BOT_TOKEN` from the tools service** (no Discord connection after `private_chat`
  — security blast-radius shrink).
- **Health (D-9):** drop the broker arm + its consumers (`supervisor/lifecycle.py`,
  `cli/main.py` `_healthcheck broker`, `supervisor/compose.py:211`); keep the
  bridge-heartbeat arm.
- **Provisioning:** ensure `calf.agents` + `calf.capabilities` (compacted) + the bridge
  inbox exist on Tansu (worker provisioning when enabled / ops); remove deleted topics
  from `_provisioning.py`.
- **CLI:** `status`/`agent ps` off the mesh; `explain` topology; env-var retirements
  (`CALFKIT_ROUTER_*`, `CALFKIT_TOOLS_TIMEOUT_SECONDS`, bootstrap-channels).
- **Security:** document (and where config-bearing, implement) the slash-gate-removal
  broker-write broadening and the channel-allowlist removal (`docs/security.md` + the
  concrete config above).
- **Tests-first:** compose lints + contains `mcp`, no `router`; `doctor` runs without
  `control_plane`; provisioning excludes deleted topics, includes the inbox.

#### B7 · Integration + green gate (branch exit)
- Run the full **§5** suite (TestKafkaBroker for caller/stream; real Tansu for
  roster/mesh + provisioning). `/pytest-coverage` 100% on changed files.
- **Public-surface gate (grep):** no `client._connection`, no `faststream`/`aiokafka`
  import (health broker probe gone), no `control_plane`, no `MCPToolboxRef`, no
  `AGENT_STEPS_TOPIC` — only the two accepted `_vendor.pydantic_ai` exceptions remain.

---

### Phase C — Docs + ADRs (own PR)

- **ADRs:** delete-router/ambient-unanswered (C2); full-native A2A + handoff (C4/C7);
  delete control_plane + roster→mesh (D); at-most-once (F); stateless A2A (G);
  persona=name + dropped fields (C8–C10); public-surface-only invariant. Supersede
  calfcord ADR-0001/0002/0004/0005/0007.
- **Docs:** rewrite `architecture.md`, `a2a-threads.md`, `authoring-agents.md` (+ native
  `peers`/`message_agent`/handoff; remove dropped frontmatter), `mcp-tools.md`,
  `configuration.md`, `distributed-deployment.md`, `security.md`; delete
  `ambient-routing.md`; update README/CONTRIBUTING/SECURITY process counts. Cosmetic:
  import public types from top-level `calfkit`.

---

## 5. Final verification — preserved-behavior contract (integration acceptance)

All Given/When/Then; **green at B7** and through C:

1. **@mention routing** — `@scribe hi` → scribe replies as scribe's persona (name +
   derived avatar). Unknown/offline `@name` → "no agent named X online" (mesh online-only).
2. **Message history** — multi-turn multi-agent channel → each mentioned agent sees
   prior turns; self read as assistant, others as `<name>` (native POV over the
   webhook-stamped history).
3. **Per-thread / per-channel scoping** — thread vs channel history (`source_channel_id`
   vs `channel_id`); thread-starter recovery + `/clear` preserved.
4. **A2A in a private channel** — consult → request (caller persona) + reply (peer
   persona) in a per-`correlation_id` thread; rejected → system note; **faulted →
   human turn fails (error reply) + failure note**.
5. **Handoff** — A→B → B's answer posts to the channel as **B**.
6. **Settings** — `/thinking-effort` persists + applies (D-8); not applied to consults.
7. **Public-surface invariant** — the B7 grep gate.

---

## 6. Risk register

| Risk | Mitigation |
|---|---|
| Un-importable tree mid-branch | The R1–R5 import-timing rules; per-step `compileall`+entrypoint-import check. |
| B1→B3c is one big red window | Per-step fakes; **integration smoke right after B3c**; B4/B4.5/B5/B6 land green-on-green; the branch is the rollback unit. |
| Faulted consult fails the whole turn (D-2) | Explicit test: `RunFailed` reply + synthesized A2A note; documented behavior. |
| Stateful A2A classifier (M1) | Tests: reject / faulted / nested / **concurrent multi-consult**; pair by `tool_call_id`, never `kind`/`name`. |
| Author mis-attribution (D-3) | Per-channel webhook discovery test incl. cold channel + offline/renamed author; never use the mesh for authorship. |
| `reader_dead` permanence (D-6) | Degraded-mode test; fail-open. |
| Inbox/topics not provisioned on Tansu (D-11) | `ProvisioningConfig(enabled=True)` + durable `inbox_topic` + `CreateTopics` ACL (or pre-create); fail-open on `MeshUnavailableError`. |
| Replay hydration envelope size (D-4) | Byte bound + a test at broker max message size. |
| At-most-once on crash (F) | Accepted + documented; side-effecting tools need their own idempotency keys. |
