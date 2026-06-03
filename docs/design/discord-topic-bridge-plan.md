# Discord ↔ Topic Bridge — Implementation Plan

The transport-and-routing layer between the Discord server and the Calfkit
topic substrate. See [`discord-org-design.md`](./discord-org-design.md) for
the surrounding multi-agent organization design; this document covers only the
bridge.

## 1. Goal

A thin, deterministic transducer that:

- Translates every relevant Discord event into a Kafka publish on a
  channel-scoped topic, with a typed `WireMessage` payload wrapped in a
  Calfkit `Envelope` so that any Calfkit node can consume it directly.
- Owns Discord slash command registration and projects native slash
  interactions onto the same channel topics as a `kind="slash"` event.
- Provides a small egress helper that lets an agent resolve (and lazily
  create) the `a2a-*` relationship channel between itself and a peer.

The bridge has **no opinion** about which agent should receive any given
event. Routing is the consumer's decision, made through Calfkit's gate
mechanism (see the gate-support design in the Calfkit repo).

## 2. Prerequisites

Calfkit gate support must be released before Phase 3 below can be
end-to-end tested. The bridge code itself does not depend on gate
support — gates live in the agents — but no agent can sit on a channel
firehose without it.

## 3. Locked design decisions

These are settled. Capture them here so future work doesn't re-litigate.

| # | Decision |
|---|---|
| 1 | Topic taxonomy is **channel-as-topic only**: `discord.channel.{channel_id}` for top-level messages, `discord.thread.{thread_id}` for thread messages. No agent-inbox topics, no slash topics, no per-fact-scope topics. |
| 2 | Topic identifiers are **numeric Discord IDs**. Channel renames do not migrate topics. |
| 3 | Slash UX is **per-agent slash, no in-call stacking**. Each registered agent gets `/{agent_id}`. Multi-agent invocation is achieved by users running additional slashes inside the resulting thread, not by stacking in one message. |
| 4 | Slash invocations are **projected onto channel topics** as a `WireMessage` with `kind="slash"` and `slash_target=<agent_id>`. The bridge handles the Discord interaction (defer, thread create, ephemeral ack); the channel-topic event is what agents consume. |
| 5 | The bridge **produces Calfkit `Envelope`s** on these topics. The wire payload lives in `Envelope.context.deps.provided_deps["discord"]` as a serialized `WireMessage`. The message text becomes a `user_text_prompt` in `state.message_history`. |
| 6 | Each agent runs in a **distinct Kafka consumer group** (`group_id = "agent.{agent_id}"` or the node name, which Calfkit defaults to). Every agent independently receives every message on its subscribed topics. |
| 7 | Each agent uses a **gate function** (Calfkit SDK feature) to decide whether to act on a received event. Slash addressing, thread membership, fact scoping, and self-recognition all live in the gate. |
| 8 | **Multiple concurrent responses are acceptable**. When two agents both gate-accept the same event, both will respond. Coordination is an agent-side concern (Chief of Staff, lead-and-delegate from the design doc). |
| 9 | Agent-to-agent messaging **goes through Discord**. An agent posts via `DiscordPersonaSender` to the appropriate `a2a-*` channel; the message re-enters the bridge and is published like any other. There is no internal short-circuit. |
| 10 | Kafka partition key is `thread_id if thread_id else channel_id` so messages within a thread preserve order. |
| 11 | The bridge holds **no centralized membership or catalog state**. The only state is the static `AgentRegistry` (loaded from config) and an in-memory channel-name → ID index refreshed from Discord lifecycle events. |

## 4. Topology

### 4.1 Topics

| Topic | Source events | Partition key |
|---|---|---|
| `discord.channel.{channel_id}` | `MESSAGE_CREATE` in a top-level channel; slash interactions targeting that channel | `channel_id` |
| `discord.thread.{thread_id}` | `MESSAGE_CREATE` inside a thread; slash interactions inside or onto a thread | `thread_id` |

Reactions and lifecycle events are out of scope for v1. Add `discord.reaction.{channel_id}` and `discord.lifecycle` topics when approval flows and thread-archive propagation are needed; the schema below is forward-compatible.

### 4.2 Wire format

The bridge owns `WireMessage` and `WireAuthor`. They carry a `schema_version` for forward evolution and never leak `discord.py` types.

```python
class WireAuthor(BaseModel):
    discord_user_id: int
    display_name: str
    is_bot: bool
    is_webhook: bool
    webhook_id: int | None = None
    agent_id: str | None = None        # resolved via AgentRegistry.by_display_name
    is_human_owner: bool = False       # the CEO user

class WireMessage(BaseModel):
    schema_version: int = 1
    event_id: str                       # uuid7, generated at the bridge
    message_id: int                     # Discord message ID (or interaction ID for slash)
    channel_id: int
    thread_id: int | None
    parent_channel_id: int | None       # the thread's parent, when thread_id is set
    guild_id: int
    kind: Literal["message", "slash"]
    slash_target: str | None = None     # agent_id when kind == "slash"
    content: str
    author: WireAuthor
    created_at: datetime
```

### 4.3 Envelope layout on the wire

What the bridge publishes is a Calfkit `Envelope`:

```python
state = State(message_history=[ModelRequest.user_text_prompt(wire.content)])

envelope = Envelope(
    internal_workflow_state=WorkflowState(call_stack=<frame targeting topic>),
    context=SessionRunContext(
        state=state,
        deps=Deps(
            correlation_id=wire.event_id,
            provided_deps={"discord": wire.model_dump(mode="json")},
        ),
    ),
)
```

The agent's gate reads `ctx.deps.provided_deps["discord"]` to make its decision. The agent's `run()` sees the message text as a standard user prompt.

## 5. Package layout

```
src/calfcord/bridge/
├── __init__.py          # public API surface
├── registry.py          # AgentSpec, AgentRegistry; YAML loader
├── wire.py              # WireMessage, WireAuthor
├── normalizer.py        # discord.py types → WireMessage + identity resolution
├── slash.py             # SlashCommandManager (registration + interaction handling)
├── publisher.py         # KafkaPublisher (WireMessage → Envelope → Kafka)
├── gateway.py           # DiscordIngressGateway daemon
└── egress.py            # A2AChannelResolver
```

The existing `src/calfcord/discord/` package (sender, persona sender, receiver, settings, messages) stays as-is. The bridge consumes `DiscordSettings` and uses `discord.py` directly for the gateway; it does not depend on `DiscordReceiver`.

### 5.1 Public surface

Exported from `calfcord.bridge`:

```python
class AgentSpec(BaseModel):
    agent_id: str
    slash: str                         # "/scheduler" (with leading slash, stored canonically)
    display_name: str
    avatar_url: str | None = None
    description: str                   # for Discord slash autocomplete (max 100 chars)

class AgentRegistry:
    def __init__(self, specs: Sequence[AgentSpec]) -> None
    @classmethod
    def from_yaml(cls, path: Path) -> Self
    def by_id(self, agent_id: str) -> AgentSpec | None
    def by_slash(self, slash: str) -> AgentSpec | None
    def by_display_name(self, name: str) -> AgentSpec | None
    def all(self) -> Sequence[AgentSpec]

class DiscordIngressGateway:
    def __init__(
        self,
        settings: DiscordSettings,
        publisher: KafkaPublisher,
        registry: AgentRegistry,
    ) -> None
    async def start(self) -> None    # blocks; runs gateway WebSocket + slash sync
    async def close(self) -> None

class KafkaPublisher:
    def __init__(self, broker: KafkaBroker) -> None
    async def publish(self, wire: WireMessage) -> None

class A2AChannelResolver:
    def __init__(
        self,
        sender: DiscordSender,
        registry: AgentRegistry,
        guild_id: int,
    ) -> None
    async def resolve_or_create(
        self,
        sender_agent_id: str,
        recipient_agent_id: str,
    ) -> int    # the channel ID
```

## 6. Slash command flow

End-to-end for `/scheduler book me a haircut` typed in `#chat`:

1. Discord delivers an `Interaction` to `SlashCommandManager`.
2. Manager calls `interaction.response.defer(ephemeral=True)` to claim the 3-second window.
3. Manager creates a new thread under the slash response (or, if invoked inside an existing thread, reuses it).
4. Manager calls `interaction.followup.send("Summoning Scheduler...", ephemeral=True)`.
5. Manager constructs a `WireMessage`:
   - `kind="slash"`
   - `slash_target="scheduler"`
   - `content="book me a haircut"`
   - `thread_id` = the newly created or existing thread ID
   - `author` = the invoking human user
   - `message_id` = the interaction ID (synthetic but stable)
6. Manager calls `publisher.publish(wire)`, which publishes the Envelope to `discord.thread.{thread_id}`.
7. Scheduler, subscribed to that thread topic with its gate set, accepts the event because `wire.kind == "slash"` and `wire.slash_target == "scheduler"`.
8. Scheduler does its work, posts the reply via `DiscordPersonaSender` to `channel_id=thread_id`.
9. That post returns through the gateway as a normal `MESSAGE_CREATE` event, normalized to `kind="message"` with `author.agent_id="scheduler"`. Scheduler's gate rejects it (self-recognition). Any other gated-in agent in the thread sees it.

In-thread plain messages (no slash) take the same path minus steps 1–4 and with `kind="message"`, `slash_target=None`.

## 7. Agent-to-agent flow

For Scheduler to consult Finance:

1. Scheduler calls `resolver.resolve_or_create("scheduler", "finance")`.
   - Resolver computes the deterministic channel name `a2a-finance-scheduler` (sorted alphabetically).
   - If the channel exists in the cached name → ID index, returns its ID.
   - Otherwise creates it via REST with permissions scoped to the user + the bot, then caches.
2. Scheduler posts via `DiscordPersonaSender.send(scheduler_persona, channel_id, content)`.
3. Discord broadcasts `MESSAGE_CREATE`. The bridge normalizes it: `author.is_webhook=True`, `author.display_name="<Scheduler's display name>"`, resolved to `author.agent_id="scheduler"`.
4. Publisher publishes the Envelope to `discord.channel.{a2a_channel_id}`.
5. Finance, subscribed to that channel, gate-accepts (it's in the channel, message is not from itself). Finance does its work, replies via its own persona.

The bridge runs the same path in both directions. There is no special A2A code path.

## 8. Configuration

### 8.1 Environment

The existing `.env` mechanism continues to provide `DISCORD_BOT_TOKEN`, `DISCORD_APPLICATION_ID`, `DISCORD_GUILD_ID`. Add `CALF_HOST_URL` for the Kafka broker (already supported by Calfkit's `Client.connect`).

### 8.2 Agent roster

A YAML file at `config/agents.yaml` (path configurable). Schema mirrors `AgentSpec`:

```yaml
agents:
  - agent_id: scheduler
    slash: /scheduler
    display_name: "Aksel (Scheduler)"
    avatar_url: null
    description: "Calendar mechanics; book and prep meetings"
  - agent_id: finance
    slash: /finance
    display_name: "Finn (Finance)"
    description: "Bookkeeping, expenses, recurring bills"
```

Loaded once at boot via `AgentRegistry.from_yaml(path)`. Pydantic validates the shape; the bridge fails fast on bad config.

### 8.3 Entry point

Add a script entry point in `pyproject.toml`:

```toml
[project.scripts]
calfkit-bridge = "calfcord.bridge.gateway:main"
```

`main()` is a small async wrapper: loads settings, registry, broker, constructs the gateway, calls `await gateway.start()`. Run via `uv run calfkit-bridge`.

## 9. Implementation phases

Each phase has a single committable scope and a verifiable acceptance criterion. Phase 0 lives in the Calfkit repo, not here.

### Phase 0 — Calfkit gate support (prerequisite, external)

See the gate-support design in the Calfkit repo. Must release a version of Calfkit with gate support before Phase 6 of this plan can be smoke-tested end-to-end.

### Phase 1 — Wire schemas and agent registry

**Files**: `bridge/wire.py`, `bridge/registry.py`, `bridge/__init__.py`.

- Define `WireMessage` and `WireAuthor` with `schema_version` and a `model_dump(mode="json")` round-trip.
- Define `AgentSpec` and `AgentRegistry`. Implement `from_yaml`, `by_id`, `by_slash`, `by_display_name`, `all`.
- Reject duplicate `agent_id`, duplicate `slash`, or duplicate `display_name` at load time.

**Done when**: unit tests pass for serialization round-trip and all four registry lookups, including the duplicate-detection error cases.

### Phase 2 — Normalizer

**Files**: `bridge/normalizer.py`.

- `MessageNormalizer.normalize(message: discord.Message) -> WireMessage`.
- `SlashNormalizer.normalize(interaction: discord.Interaction, thread_id: int) -> WireMessage`.
- Identity resolution: bot's own user → `author.is_bot=True, agent_id=None`; webhook with a name matching a registry `display_name` → `agent_id=<resolved>`; bot owner's user ID (configured) → `is_human_owner=True`.
- `parent_channel_id` is populated when `message.channel` is a thread.

**Done when**: unit tests cover top-level messages, thread messages, persona-webhook messages, and the slash interaction path. Tests use hand-built fake `discord.py` objects (no live Discord).

### Phase 3 — Publisher

**Files**: `bridge/publisher.py`.

- `KafkaPublisher.publish(wire)` constructs an `Envelope` per §4.3 and publishes to the correct topic (`discord.thread.{thread_id}` if set, else `discord.channel.{channel_id}`) with the partition key from §4.1.
- Uses the `KafkaBroker` from Calfkit's `Client` so we share the broker config.

**Done when**: unit tests with an in-memory mock broker verify topic name, partition key, and envelope shape (including `deps.provided_deps["discord"]` and `state.message_history`).

### Phase 4 — Gateway daemon

**Files**: `bridge/gateway.py`.

- `DiscordIngressGateway` constructs a `discord.Client` with the required intents (message_content, members, guilds), registers `on_message`, lifecycle hooks (`on_thread_create`, `on_channel_create`, etc.) for the in-memory channel index, and the slash command manager.
- `on_message` → `MessageNormalizer.normalize` → `KafkaPublisher.publish`.
- `start()` calls `await client.start(token)`; idempotent `close()`.
- Structured logs include `event_id` and `correlation_id`.

**Done when**: integration test with a real bot in a private test guild — post a message, assert the corresponding Kafka topic receives an Envelope whose decoded `WireMessage` matches.

### Phase 5 — Slash command manager

**Files**: `bridge/slash.py`.

- `SlashCommandManager.sync(guild_id)` registers one `/agent_id` slash per registered agent at boot (guild-scoped for fast iteration; global later). Each slash has one required string argument named `message`.
- On invocation: `defer(ephemeral=True)` → ensure thread (create or reuse) → normalize via `SlashNormalizer` → `publisher.publish(wire)` → `followup.send(...)` with a brief ack.
- Unknown slash (no agent registered): respond with an ephemeral error; do not publish.

**Done when**: integration test — register two agents, run `/scheduler hello` in the test guild, assert (a) a new thread was created, (b) `discord.thread.{thread_id}` received a `WireMessage` with `kind="slash"` and `slash_target="scheduler"`, (c) the human user got an ephemeral ack.

### Phase 6 — A2A egress helper

**Files**: `bridge/egress.py`.

- `A2AChannelResolver.resolve_or_create(a, b)`:
  - Computes the sorted name `a2a-{min}-{max}` from the two agent IDs.
  - Checks an in-memory cache.
  - On miss, lists guild text channels via REST, populates the cache, retries lookup.
  - On still-miss, creates the channel with permission overwrites scoped to the bot user + the human owner.
- Uses the existing `DiscordSender` for the REST client (no gateway needed).

**Done when**: integration test — first call creates the channel; second call returns the cached ID with no REST round-trip.

### Phase 7 — End-to-end smoke

No new files; this is the validation phase.

- Stand up a stub Calfkit agent that registers a gate accepting only `kind=="slash" and slash_target==self.agent_id`, plus self-recognition; in `run()` it just persona-posts "echo: {content}" back to the thread.
- Run the bridge and the stub agent concurrently.
- In the test guild:
  - Run `/echo hello` — assert the stub posts "echo: hello" into the resulting thread.
  - Post a plain message in the same thread — assert the stub does **not** respond (its slash-only gate rejects).
  - Modify the stub gate to also accept thread messages, restart, repeat — assert it now responds.
  - Verify the stub's own posts do not trigger another response (self-recognition gate).
- Add a second stub agent and confirm both gate-accept correctly when their slashes are used independently.

**Done when**: all four checks pass without manual intervention and with no exceptions in the bridge or agent logs.

## 10. Open decisions to make during implementation

These are real loose ends that will surface during the build. Each can be punted to a default until evidence demands otherwise.

1. **Per-agent thread membership persistence.** Agents need to know which threads they are "in" so their gates can accept non-slash messages there. The bridge does not track this. Default: each agent keeps an in-memory set, populated by its own `kind="slash"` accepts and `/leave` rejects. On restart the set rebuilds as new slashes come in. If that proves insufficient, the agent can subscribe to a separate state stream — but defer until needed.

2. **Thread archive propagation.** When Discord auto-archives a thread (7 days idle), the bridge should publish a wire event so agents can drop the thread from their local set. v1 punts this; threads quietly stop receiving events anyway. Add `kind="thread_archived"` as a forward-compatible field of `WireMessage` later.

3. **Bridge idempotency on reconnect.** Discord can redeliver `MESSAGE_CREATE` events when the gateway reconnects. v1 publishes both copies. Acceptable because agents are required to be idempotent (per design doc §8.4). Add Redis-backed dedup if it becomes a real problem.

4. **Schema evolution.** Bumping `WireMessage.schema_version` requires every deployed agent to tolerate the new field. Maintain a CHANGELOG entry per bump and only add fields, never remove or rename in place.

5. **Observability.** v1: structured logs with `event_id` and `correlation_id`. Add Prometheus counters per topic and per agent (publishes, drops) once we have more than one agent in steady state. Defer dashboards.

6. **Guild bootstrap.** First-run setup of `#chat`, `#facts`, `#standup`, `#coordination`, `#control-plane`, `#agent-pings`, `#announcements` is a one-time problem. Write a separate `scripts/provision_guild.py` that idempotently ensures these exist with the correct permissions; not part of the bridge daemon.

7. **Slash sync timing.** Guild-scoped slash sync is fast (seconds). Global sync takes ~1 hour. For dev iteration, set `DISCORD_GUILD_ID` and use guild sync. Production decision deferred.

8. **Reaction and lifecycle topics.** v1 omits these. The wire format and topic naming convention are designed to accommodate `discord.reaction.{channel_id}` and `discord.lifecycle` when the approval flow and thread-archive propagation features land.

## 11. Out of scope (non-goals)

Explicit non-goals — to prevent drift:

- The agents themselves. The bridge is pure transport.
- Approval / authority logic. Lives in agents and possibly a separate library above the bridge.
- `#facts` schema, scope tagging, fact subscriptions. Agent-side concerns; the bridge sees facts as ordinary channel messages.
- `#control-plane` aggregation, reputation, cost telemetry. Separate consumers.
- Internal agent-to-agent routing of any kind. A2A is Discord-mediated; the bridge does not short-circuit.
- A composable gate-predicate library. Agents write their own gate logic.
- Multi-guild support. v1 is one guild, configured via `DISCORD_GUILD_ID`.
