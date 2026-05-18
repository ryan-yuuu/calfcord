# Calfkit Agent Factory — Design and Implementation Plan

The transducer that turns a declarative `agents/<name>.md` definition into a
runnable, LLM-backed calfkit agent process. Replaces the
`NotImplementedError` stub at
`src/calfkit_organization/agents/factory.py:53-58`. Builds on the existing
Discord ↔ Kafka bridge (`discord-topic-bridge-plan.md`) and the calfkit
0.3.0 emitter-header feature (`x-calf-emitter` / `x-calf-emitter-kind`).

## 1. Goal

`AgentFactory.build(definition, state, store) -> Worker` returns a calfkit
`Worker` that:

1. Subscribes to `discord.channel.{cid}.in` for each `cid` in
   `state.channels`.
2. Gates inbound events with the project's two shared predicates
   (addressable, addressed-to-me).
3. Runs an LLM (Anthropic Claude) using `definition.system_prompt` as the
   system message and the inbound Discord message text as the user prompt.
4. Replies via `ReturnCall`, which lands on the bridge's named reply topic
   (`discord.outbox`) and triggers an inline Discord post by the bridge
   gateway under the agent's persona.

The implementation must:

- Reuse calfkit's `Agent` (LLM-loop) class without subclassing.
- Reuse the bridge's existing `DiscordPersonaSender`, `MessageNormalizer`,
  `AgentRegistry`, and `WireMessage` types unchanged.
- Be testable without hitting Discord or the Anthropic API.

## 2. Background and constraints

### 2.1 What's already in place

- **`agents/definition.py`** — parses YAML frontmatter + Markdown system
  prompt into an `AgentDefinition`. Validates Discord slash and webhook
  constraints at load time.
- **`agents/loader.py`** — scans `agents/*.md`, returns a sorted list of
  `AgentDefinition`s.
- **`agents/state.py`** — atomic per-agent JSON state file with
  `channels: list[int]`.
- **`agents/runner.py`** — CLI entry. Loads definition + state, opens
  `DiscordPersonaSender` and calfkit `Client`, constructs `AgentFactory`,
  calls `factory.build(...)`, runs the worker under SIGINT/SIGTERM.
- **`bridge/`** — full Discord ingress: gateway WebSocket, message
  normalizer, agent registry, Kafka publisher. Currently fire-and-forget on
  publishes (`KafkaPublisher.publish` schedules a cleanup task that
  discards replies).
- **`agents/echo.py`** + **`agents/echo.md`** — hand-coded reference agent
  that defines the gate pattern this plan generalizes.

### 2.2 Calfkit 0.3.0 capabilities relied on

- **Emitter headers stamped automatically on every publish**
  (`x-calf-emitter` carries `node_id`; `x-calf-emitter-kind` carries
  `NodeKind ∈ {"node","agent","tool","client"}`). Set in
  `BaseNodeDef._publish_action` for nodes and in `BaseClient._invoke` for
  the client.
- **Emitter readable on the consumer side**: `BaseNodeDef.handler` reads
  the inbound headers and writes them onto `ctx.emitter_node_id` /
  `ctx.emitter_node_kind` via `prepare_context`. Backed by `PrivateAttr` so
  they cannot be spoofed via envelope construction.
- **Emitter surfaced client-side too**: `NodeResult.emitter_node_id` /
  `emitter_node_kind` populated by `deserialize_to_node_result` from the
  reply-topic headers stashed by `_ReplyDispatcher`.
- **`Client.connect(reply_topic=...)`** accepts a named reply topic. The
  dispatcher binds its subscriber to that topic for the client's lifetime.

### 2.3 Hard constraints (locked, do not relitigate)

| # | Constraint | Source |
|---|---|---|
| 1 | Bridge holds no centralized membership state. Each agent owns its own `state.channels`. | `discord-topic-bridge-plan.md` §3 #11 |
| 2 | One agent process = one consumer group = one Worker = one calfkit node. | bridge plan §3 #6 |
| 3 | The bridge is the only Discord HTTP egress; agents do not call Discord directly via this factory. | new (this plan) |
| 4 | `WireMessage.channel_id` is always a parent text channel. Posting into a thread is out of scope for v1. | bridge plan §2; normalizer behavior |
| 5 | The factory does not manage the lifecycle of `DiscordPersonaSender` or `Client` — the runner does. | runner.py current contract |

## 3. Locked design decisions

These are settled across the prior design discussion. Captured here so they
don't get rediscussed during implementation.

| # | Decision |
|---|---|
| D1 | **Channel topic pair per Discord channel.** Ingress is `discord.channel.{cid}.in`. There is no per-channel egress topic. |
| D2 | **Single named reply topic at the bridge.** `Client.connect(reply_topic="discord.outbox")`. The calfkit dispatcher binds to this topic; all agent replies land here. |
| D3 | **Bridge uses `Client.execute_node` (awaitable)** to invoke the agent and await the reply. The bridge's gateway handler is request/response within one coroutine; the WireMessage stays in local scope across the `await`. |
| D4 | **No `AgentFactory`-side subclass of `BaseAgentNodeDef`.** Identity-stamping comes for free from calfkit 0.3.0 emitter headers. The factory composes a vanilla `Agent`. |
| D5 | **Persona resolution via `result.emitter_node_id`** at the bridge. The bridge looks up the responding agent's `AgentDefinition` in the `AgentRegistry` and renders its persona. |
| D6 | **AND-stacked gates on the agent**: `addressable_gate` (not from self / not from unregistered bot) then `addressed_to_me_gate` (slash target matches OR non-slash message). These already exist in `agents/echo.py` and need extraction. |
| D7 | **Anthropic provider for v1**, via `AnthropicModelClient`. The Anthropic API key is read from `ANTHROPIC_API_KEY` env var by the provider; the factory does not handle keys. |
| D8 | **Model name resolution**: prefer `definition.model` (full Anthropic model name, e.g. `"claude-sonnet-4-5"`). Falls back to env `CALFKIT_AGENT_DEFAULT_MODEL`. Falls back to a constant default of `"claude-sonnet-4-5"`. No shortname mapping. |
| D9 | **Inline-reply style**: `"button"`, matching the echo agent. Configurable later via env if needed. |
| D10 | **Definition.tools is ignored for v1** with a WARNING log when non-empty. Tool support is a separate phase. |
| D11 | **`store` is accepted into `build()` but unused for v1.** Runtime channel mutation (e.g. "agent auto-joins a new thread") is a future feature. |
| D12 | **Multi-agent reply degradation accepted for v1.** Only the first agent's reply lands; subsequent replies for the same `correlation_id` are dropped by the dispatcher. Acceptable because v1 traffic is dominated by slash/mention (single-target). |

## 4. Architecture

### 4.1 Topology

```
┌────────────────── Bridge process ──────────────────┐
│                                                    │
│  DiscordIngressGateway  ──MessageNormalizer──> WireMessage
│        │                                           │
│        │ execute_node(                             │
│        │   topic="discord.channel.{cid}.in",       │
│        │   correlation_id=wire.event_id,           │
│        │   deps={"discord": wire.model_dump()},    │
│        │ )                                         │
│        ▼                                           │
│  ──────── publish to discord.channel.{cid}.in ─────┼──> Kafka
│                                                    │
│  (await suspends; wire stays in local scope)       │
│                                                    │
│                                                    │     ┌──── Agent X process ──────────┐
│  Kafka ──── consume ──────────────────────────────────── │ Worker (calfkit)               │
│                                                    │     │   Agent(node_id="scheduler",   │
│                                                    │     │         subscribe=...,         │
│                                                    │     │         gates=[...],           │
│                                                    │     │         model_client=...)      │
│                                                    │     │     ├─ gates                   │
│                                                    │     │     ├─ LLM round-trip          │
│                                                    │     │     └─ ReturnCall              │
│  ──── consume discord.outbox ──── _ReplyDispatcher ─────│── publishes to "discord.outbox"│
│        │                                           │     │   (frame.callback_topic),     │
│        │ dispatcher resolves future                │     │   headers x-calf-emitter=     │
│        │ for correlation_id=wire.event_id          │     │   "scheduler",                 │
│        ▼                                           │     │   x-calf-emitter-kind="agent" │
│  result = NodeResult(                              │     └────────────────────────────────┘
│    output="…", emitter_node_id="scheduler",        │
│    emitter_node_kind="agent", …)                   │
│        │                                           │
│        ▼                                           │
│  registry.by_id("scheduler") → AgentDefinition     │
│  Persona(name=defn.display_name, avatar=…)         │
│  DiscordPersonaSender.send(persona, channel_id=    │
│    wire.channel_id, content=result.output,         │
│    reply_to=ReplyContext.from_wire(wire))          │
│        │                                           │
└────────┼───────────────────────────────────────────┘
         ▼
   Discord HTTP (webhook)
```

### 4.2 Identity carriage

Per D4 and D5: the agent's identity is carried by the Kafka `x-calf-emitter`
header, which is stamped automatically by calfkit on every node publish. The
bridge reads this off `NodeResult.emitter_node_id`. No `state.metadata`
stamping, no subclassing, no `Deps` mutation.

A bonus: `emitter_node_kind == "agent"` lets the bridge defensively reject
emissions that came from anything other than an agent (e.g. client
republishes or accidental tool emissions). This is a safety check, not a
correctness requirement.

### 4.3 Channel target carriage

Per D3: `_on_message` builds `wire`, awaits `execute_node`, then posts to
`wire.channel_id`. The `wire` local survives the `await` via Python's
coroutine semantics. No external state tracking, no dict, no Redis. Each
inbound message has its own coroutine with its own `wire` binding.

## 5. Files

### 5.1 New files

| File | Purpose | Approx. lines |
|---|---|---|
| `src/calfkit_organization/agents/gates.py` | `make_addressable_gate(agent_id)`, `make_addressed_to_me_gate(agent_id)` — extracted from `agents/echo.py`. | ~70 (incl. docstrings) |
| `tests/agents/test_gates.py` | Unit tests: addressable/not-addressable, slash-target match/mismatch, non-slash accept, missing-discord-dep reject, AND-stacking sanity. | ~120 |

### 5.2 Modified files

| File | Change |
|---|---|
| `src/calfkit_organization/agents/factory.py` | Replace `NotImplementedError` stub with real `build()`. Add `default_model` constructor argument and `_resolve_model` helper. Add optional `model_client_factory` parameter for test injection. |
| `src/calfkit_organization/agents/__init__.py` | Re-export the two gate helpers and a `make_persona(definition)` helper if extracted. |
| `src/calfkit_organization/bridge/publisher.py` | **Delete** or repurpose. Replace `KafkaPublisher.publish(wire)` with an awaitable `invoke_and_get_reply(wire) -> NodeResult` (or fold straight into `DiscordIngressGateway._on_message`). The cleanup-future scaffolding goes away because we now consume replies. |
| `src/calfkit_organization/bridge/gateway.py` | (a) Call `Client.connect(server_urls, reply_topic="discord.outbox", client_id="bridge.discord")` in `main`. (b) `_on_message` becomes `wire → execute_node → registry lookup → persona send` inline. (c) Remove the `KafkaPublisher`-specific wiring. |
| `src/calfkit_organization/discord/persona.py` | Add `ReplyContext.from_wire(wire, style="button")` classmethod. Used in 3+ call sites. |
| `agents/echo.py` | Migrate gate construction to import from `agents/gates.py`. Behavior unchanged. Echo continues to post directly via persona sender (it remains a hand-coded reference, outside the LLM factory flow). |
| `tests/agents/test_factory.py` | Rewrite: the stub-pinning tests go away. New tests verify `build()` returns a `Worker` with one node whose `node_id`, `subscribe_topics`, gates (by name), and model-client wiring match the definition + state. Uses an injected `model_client_factory` to avoid real model construction. |
| `tests/bridge/conftest.py` and `tests/bridge/test_*.py` | Update where they reference the old `KafkaPublisher` API. |
| `agents/scribe.md` (or similar) | A v1 LLM-backed test agent definition for the smoke phase. |

### 5.3 Files explicitly NOT changed

| File | Reason |
|---|---|
| `agents/definition.py` | Schema unchanged. |
| `agents/loader.py` | Logic unchanged. |
| `agents/state.py` | Schema unchanged. |
| `agents/runner.py` | Already does the right thing; no changes needed. The runner constructs the factory, passes the existing `persona_sender` and `calfkit_client`, calls `build`, runs the worker. |
| `bridge/wire.py`, `bridge/registry.py`, `bridge/normalizer.py`, `bridge/slash.py` | Unchanged. |
| `bridge/egress.py` (A2A resolver) | Unchanged. |

## 6. Implementation phases

Each phase is a single commit with a verifiable acceptance criterion. Phases
are ordered so each is unit-testable without the next.

### Phase 1 — Extract shared gates

**Files**: `src/calfkit_organization/agents/gates.py` (new),
`agents/echo.py` (update imports), `src/calfkit_organization/agents/__init__.py`
(re-exports), `tests/agents/test_gates.py` (new).

**Scope**:

- Move `_make_addressable_gate` and `_make_addressed_to_me_gate` from
  `agents/echo.py` into `agents/gates.py`. Rename to public
  `make_addressable_gate` / `make_addressed_to_me_gate`.
- Update `agents/echo.py` to import them. The hand-coded runtime in
  `agents/echo.py` keeps its current direct-persona-send behavior; only the
  gate construction moves.

**Acceptance**:

- `pytest tests/agents/test_gates.py` passes.
- `pytest tests/bridge/test_persona_reply.py tests/bridge/test_normalizer.py`
  still passes (no echo-agent regression).
- Echo agent still runs locally end-to-end.

### Phase 2 — `ReplyContext.from_wire` helper

**File**: `src/calfkit_organization/discord/persona.py` (add classmethod).

**Scope**: One ~10-line classmethod that constructs a `ReplyContext` from a
`WireMessage`. No behavior change anywhere else; this is a refactor that
deduplicates the construction across echo, the smoke agent, and the new
bridge handler.

**Acceptance**:

- `tests/bridge/test_persona_reply.py` extended with two cases:
  `from_wire` populates all fields correctly; `style` argument overrides
  the default.

### Phase 3 — `AgentFactory.build`

**Files**: `src/calfkit_organization/agents/factory.py` (real impl),
`tests/agents/test_factory.py` (rewrite).

**Scope sketch** (not the final code, but the shape):

```python
class AgentFactory:
    def __init__(
        self,
        persona_sender: DiscordPersonaSender,
        calfkit_client: Client,
        *,
        default_model: str = "claude-sonnet-4-5",
        model_client_factory: Callable[[str], PydanticModelClient] | None = None,
    ) -> None:
        self._persona_sender = persona_sender
        self._calfkit_client = calfkit_client
        self._default_model = default_model
        self._model_client_factory = model_client_factory or _default_anthropic

    def build(
        self,
        definition: AgentDefinition,
        state: AgentRuntimeState,
        store: AgentStateStore,
    ) -> Worker:
        if definition.tools:
            logger.warning(
                "agent %r declares tools=%s but tools are not yet wired; ignoring",
                definition.agent_id, list(definition.tools),
            )
        model_name = (
            definition.model
            or os.getenv("CALFKIT_AGENT_DEFAULT_MODEL")
            or self._default_model
        )
        model_client = self._model_client_factory(model_name)
        agent = Agent(
            node_id=definition.agent_id,
            system_prompt=definition.system_prompt,
            subscribe_topics=[f"discord.channel.{cid}.in" for cid in state.channels],
            model_client=model_client,
        )
        agent.gate(make_addressable_gate(definition.agent_id))
        agent.gate(make_addressed_to_me_gate(definition.agent_id))
        # store accepted but unused for v1 (see D11)
        del store
        return Worker(self._calfkit_client, [agent])
```

`_default_anthropic` is a free function that returns
`AnthropicModelClient(model_name=name)`. The factory does not touch API
keys; pydantic-ai's `AnthropicProvider` reads `ANTHROPIC_API_KEY` from env.

**Tests** (no live LLM, no Kafka):

- Construction with all required args succeeds.
- `build()` returns a `Worker` whose `_nodes` has exactly one element.
- That node has `node_id == definition.agent_id`,
  `subscribe_topics == [f"discord.channel.{cid}.in" for cid in state.channels]`,
  and exactly two registered gates whose names match
  `addressable_for_{agent_id}` and `addressed_to_me_{agent_id}` (or
  whatever the gates are named).
- Model resolution: `definition.model="x"` ⇒ factory passes `"x"` to the
  model client factory. `definition.model=None` + env unset ⇒ factory
  passes `default_model`. `definition.model=None` + env set ⇒ env wins.
- Tools warning: `definition.tools=("calendar",)` ⇒ a warning is logged
  with the agent_id and the tool list.

The `model_client_factory` parameter exists specifically so tests pass a
`Mock(spec=PydanticModelClient)` and avoid constructing a real Anthropic
client.

**Acceptance**: `pytest tests/agents/test_factory.py` passes; the existing
`test_runner.py` is unaffected.

### Phase 4 — Replace `KafkaPublisher` with the awaitable round-trip

**Files**: `src/calfkit_organization/bridge/publisher.py` (delete or
simplify), `src/calfkit_organization/bridge/__init__.py` (drop the
re-export), `src/calfkit_organization/bridge/gateway.py` (update internal
calls), `tests/bridge/test_publisher.py` (rewrite or delete).

**Decision point during impl**: do we keep a thin `AgentInvoker` class for
testability, or fold the logic into `gateway._on_message`?

- Folding inline: ~15 lines in the gateway. Simpler. Harder to unit-test
  in isolation (need a full gateway).
- Thin `AgentInvoker`: separates "build wire → invoke → post" from "Discord
  gateway plumbing". Easier to test. Adds one file.

**Recommendation**: thin `AgentInvoker` (or `BridgeRoundtrip`) with one
public method `async def handle(wire: WireMessage) -> None`. Keep the
gateway focused on Discord event handling.

```python
class AgentInvoker:
    def __init__(
        self,
        calfkit_client: Client,
        registry: AgentRegistry,
        persona_sender: DiscordPersonaSender,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        ...

    async def handle(self, wire: WireMessage) -> None:
        try:
            result = await self._client.execute_node(
                user_prompt=wire.content,
                topic=f"discord.channel.{wire.channel_id}.in",
                correlation_id=wire.event_id,
                deps={"discord": wire.model_dump(mode="json")},
                output_type=str,
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("agent reply timed out event_id=%s", wire.event_id)
            return
        if result.emitter_node_kind != "agent" or not result.emitter_node_id:
            logger.warning(
                "non-agent emitter for event_id=%s: id=%s kind=%s",
                wire.event_id, result.emitter_node_id, result.emitter_node_kind,
            )
            return
        spec = self._registry.by_id(result.emitter_node_id)
        if spec is None:
            logger.warning("unknown agent emitter=%s; dropping", result.emitter_node_id)
            return
        text = (result.output or "").strip()
        if not text:
            return
        await self._persona_sender.send(
            persona=Persona(name=spec.display_name, avatar_url=spec.avatar_url),
            channel_id=wire.channel_id,
            content=text,
            reply_to=ReplyContext.from_wire(wire),
        )
```

**Tests** (`tests/bridge/test_agent_invoker.py`, new):

- Happy path: stub `Client.execute_node` to return a `NodeResult` with
  `emitter_node_id="scheduler"` and `output="hello"`. Registry contains
  Scheduler. Assert `persona_sender.send` called with Scheduler's persona,
  the right channel_id, content `"hello"`, and a `ReplyContext`.
- Unknown emitter: assert warning logged and no send.
- Empty output: assert no send.
- Wrong emitter kind: assert warning + no send.
- Timeout: assert warning + no send.

**Acceptance**: tests pass; gateway compiles against the new API.

### Phase 5 — Bridge gateway integration

**File**: `src/calfkit_organization/bridge/gateway.py`.

**Scope**:

- In `main()`, pass `reply_topic="discord.outbox"` and
  `client_id="bridge.discord"` to `Client.connect`.
- Construct an `AgentInvoker` in `DiscordIngressGateway.__init__` and
  call `await self._invoker.handle(wire)` from `_on_message` in place of
  the current `await self._publisher.publish(wire)`.
- Construct the `DiscordPersonaSender` in the gateway's lifecycle (it's
  currently constructed in the runner for agents, but the *bridge* also
  needs one for outbound posting on agent replies). Two options:
  (a) the gateway opens its own persona sender, or (b) the runner passes
  one in. **Choose (a)** — the bridge process is independent of agent
  processes; each agent's runner has its own persona sender.

**Tests**: integration / smoke test deferred to Phase 7. Unit-testing the
gateway's `_on_message` end-to-end is heavy (requires faking `discord.Message`,
the calfkit client, the persona sender). The `AgentInvoker` is the
testable layer; gateway is a thin shim.

**Acceptance**: `uv run calfkit-bridge` boots without errors against a
local Kafka + a test Discord guild; ingress logs an `agent reply timed out`
warning when no agents are running (proving the round-trip path is wired).

### Phase 6 — Echo agent migration (deferred decision)

**Two options**:

- **Leave echo as-is**: it remains a hand-coded reference that bypasses the
  factory and the bridge's reply round-trip. Posts directly via its own
  persona sender. Good for keeping a simple smoke-test that doesn't depend
  on the new round-trip.
- **Migrate echo to use the factory**: define a system prompt like
  "respond with `echo: <user message>`", drop the hand-coded `EchoNode`.
  Tests the full factory + bridge path end-to-end.

**Recommendation**: leave echo as-is for v1. It exercises the bridge's
ingress path and the persona sender; it does not exercise the new factory
or the round-trip. We get *both* signals by also adding a separate LLM
test agent in Phase 7.

### Phase 7 — Smoke-test LLM agent

**Files**: `agents/<name>.md` (new) — e.g. `agents/scribe.md`:

```markdown
---
name: scribe
slash: /scribe
display_name: Scribe
description: Test agent that LLM-responds via the factory.
avatar_url: https://api.dicebear.com/9.x/glass/png?seed=scribe
model: claude-haiku-4-5    # use the cheap model for smoke tests
---

You are Scribe, a friendly test agent. Reply concisely (1-3 sentences) to
whatever the user says.
```

**Acceptance** (manual, against a live test guild):

1. `ANTHROPIC_API_KEY=… CALFKIT_AGENT_SCRIBE_BOOTSTRAP_CHANNELS=<test_channel_id> uv run calfkit-agent scribe`
   starts the agent without errors.
2. Bridge running concurrently: `uv run calfkit-bridge`.
3. In the test channel, post `@scribe hello`.
4. Within ~10s, Scribe replies in the channel under its persona, inline-
   reply-styled.
5. The bridge logs: ingress → publish to
   `discord.channel.<cid>.in` → reply received with
   `emitter_node_id="scribe"` → persona send.
6. Scribe logs: gate-accept → LLM round-trip → `ReturnCall` published.

**Negative checks**:

- Post `@unknown_agent hello` → bridge replies with the existing unknown-
  mention error reply; no Kafka publish.
- Stop the Scribe process. Post `@scribe hello`. Bridge logs a timeout
  after 120s and does not post (no orphaned reply).

## 7. Test strategy

### 7.1 Unit tests (CI)

All unit tests run without network, without Kafka, without Discord, without
LLM credits. They use `unittest.mock` and the existing
`tests/bridge/conftest.py` fakes for discord.py objects.

| Layer | What is mocked |
|---|---|
| Gates | None — pure functions over a fake `SessionRunContext` (dataclass) |
| Factory | `model_client_factory` injected; `persona_sender`, `calfkit_client` are `MagicMock` |
| `AgentInvoker` | `calfkit_client.execute_node` returns a constructed `NodeResult`; `persona_sender.send` is asserted on |
| `ReplyContext.from_wire` | Pure function over a `WireMessage` |

### 7.2 Integration test (manual, gated)

The smoke test in Phase 7 is the only test that requires Kafka + Discord +
Anthropic credits. Documented in the repo README but not run in CI.

### 7.3 What is NOT tested

- Real LLM output quality. Scribe's reply is asserted to exist and be
  non-empty, not to be "correct."
- Real Discord rate limits.
- Bridge resilience under partition rebalance or Kafka unavailability.
- Concurrent multi-message flow (the closure model is correct by Python
  semantics; no test needed for that).

## 8. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Multi-agent reply: only first lands; second is dropped at the dispatcher. | Medium for ambient flows; low for slash/mention. | Reduced fidelity vs design doc §5.1. | Documented (D12). Migrate to a non-dedupe egress consumer when ambient flows become important. |
| Anthropic API key absent at boot. | Low | Worker fails on first event. | `AnthropicProvider` raises immediately on missing key — manifests at agent start, not silently later. Surface via `BootstrapError` in the runner. |
| Definition.model has a typo or invalid name. | Low | First LLM call returns 4xx. | The `AnthropicModelClient` constructor accepts any string; we won't catch this until first call. Acceptable — same UX as a typo in any model name. |
| LLM returns empty text. | Low | No Discord post. | Explicitly handled in `AgentInvoker.handle` (early return on empty). |
| LLM returns a structured response (DataPart) instead of text. | Low for v1 (`output_type=str` enforced). | DeserializationError raised by `_extract_text`. | Tests assert this case logs and drops without re-raising. |
| Reply-topic name collision across multiple bridges. | Low (single bridge per server). | Two bridges sharing `discord.outbox` would steal each other's replies. | If we ever run multiple bridge instances, use `client_id=f"bridge.{instance}"` and let calfkit auto-derive a unique reply topic. v1 is single-bridge. |
| `state.channels` empty at boot. | Low (runner enforces non-empty bootstrap). | `subscribe_topics=[]` causes the agent worker to start with no consumers. | The runner's existing bootstrap path rejects zero channels (`runner.py:138-139`). |
| Agent process death between bridge publish and reply. | Medium (agent crash, restart) | Bridge times out after 120s, drops the original Discord message. | Documented behavior. Idempotency means replays from Kafka offset rewind would re-publish; bridge would re-await but Discord message ID is fixed → potential double-post on agent recovery. Out of scope for v1; flagged for future work. |

## 9. Out of scope (deferred)

These are explicit non-goals for this plan. Each is a real follow-up worth
its own design pass.

- **Tools** (`definition.tools`). Wiring named tools into the agent's
  toolset requires a tool registry and a `ToolNodeDef` factory. Separate
  plan.
- **OpenAI provider.** `definition.model` could later carry a provider
  prefix (`openai/gpt-5`, `anthropic/claude-sonnet-4-5`) and the factory
  could dispatch. Not in v1.
- **Auto-join via `store.add_channel`.** The runtime mutation of channel
  subscriptions when an agent is summoned into a new channel via slash.
  Requires either bridge-side membership tracking or a per-agent sidecar.
- **Multi-reply egress.** A proper egress consumer that does not dedupe by
  `correlation_id`. Returns multi-agent ambient flows to design-spec
  behavior. Replace `AgentInvoker.handle` with a fire-and-forget publish
  plus a separate subscriber on `discord.outbox`.
- **Thread posting.** `WireMessage` does not carry `thread_id`. Add the
  field + populate in `MessageNormalizer` + thread `thread_id=` through to
  `DiscordPersonaSender.send`.
- **Idempotency / deduplication on retry.** Discord can redeliver
  `MESSAGE_CREATE` on gateway reconnect; the bridge currently double-
  publishes. Acceptable per bridge plan §10.3.
- **Cost telemetry.** Per-agent token cost reporting to `#control-plane`.
- **Streaming responses.** LLM token streaming into Discord. The current
  flow is request/response.
- **Authority profile.** `definition` has no authority field yet. Approval
  flows belong to a separate layer.

## 10. Open questions to resolve during implementation

These are real loose ends. Each has a recommended default; surface to the
user if evidence demands otherwise.

1. **Persona sender ownership in the bridge.** The bridge needs to post on
   reply. Where does its `DiscordPersonaSender` come from — open one in the
   gateway's `_run`, or accept one externally? **Default**: open in the
   gateway. Symmetric with how agents own theirs.
2. **`AgentInvoker.timeout_seconds`.** Default 120s. Trade-off: too short
   ⇒ premature timeout on long LLM responses; too long ⇒ pending futures
   accumulate if agents die. **Default 120s**, configurable via env.
3. **`client_id="bridge.discord"`.** Makes the bridge's emitter id stable
   (`client.bridge.discord` instead of `client.<uuid7>`). Helpful for
   telemetry. **Default: set it.**
4. **Should `AgentInvoker` be a class or a free function?** Class makes
   testing slightly easier (dependency injection via constructor). Free
   function is shorter. **Default: class**, ~30 lines.
5. **Error reply to the user on timeout.** Currently the bridge silently
   drops on timeout. Should we reply with "Agent didn't respond"? **Default:
   no.** Matches the existing fire-and-forget UX; revisit if users
   complain about missing responses.

---

## Appendix A — Concrete file diff inventory

For the engineer doing the work, here's the bill of materials:

**New files (4)**:
- `src/calfkit_organization/agents/gates.py` (~70 lines)
- `tests/agents/test_gates.py` (~120 lines)
- `src/calfkit_organization/bridge/invoker.py` (`AgentInvoker`, ~80 lines)
- `tests/bridge/test_agent_invoker.py` (~150 lines)
- `agents/scribe.md` (~15 lines)

**Modified files (6)**:
- `src/calfkit_organization/agents/factory.py` (rewrite: stub → ~60 lines)
- `src/calfkit_organization/agents/__init__.py` (add 2 re-exports)
- `src/calfkit_organization/bridge/gateway.py` (~30 lines of delta:
  inject `AgentInvoker`, swap publisher call, set `reply_topic`)
- `src/calfkit_organization/bridge/__init__.py` (drop `KafkaPublisher`
  re-export, add `AgentInvoker`)
- `src/calfkit_organization/discord/persona.py` (add
  `ReplyContext.from_wire`, ~10 lines)
- `agents/echo.py` (import gates from new module; ~5 lines of delta)
- `tests/agents/test_factory.py` (rewrite: stub-pin tests → real tests)

**Deleted files (2)**:
- `src/calfkit_organization/bridge/publisher.py`
- `tests/bridge/test_publisher.py`

Net: +~430 lines (mostly tests), -~150 lines (publisher + stub pins),
~6 file modifications.
