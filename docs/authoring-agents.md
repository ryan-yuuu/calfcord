# Authoring a calfcord Agent

How to add a new LLM-backed agent to a calfcord deployment. This is the
contributor reference for the file-drop workflow under `agents/`; for the
companion reference on the tools an agent can wield, see
`docs/authoring-tools.md`. For the calfkit node model that backs each
agent, see `src/calfcord/agents/factory.py`.

## 1. Overview

An agent is a Markdown file with YAML frontmatter and a system-prompt
body, dropped at `agents/<name>.md`. The frontmatter declares identity
and intrinsic runtime hints (model, tools, thinking effort); the body
is the system prompt fed verbatim to the LLM on every run. The format
parallels Claude Code's `.claude/agents/*.md` convention so the same
mental model carries over.

Besides hand-writing the file, the `calfcord agent` command group manages an
agent's whole lifecycle — `create`, `list`, `show`, `edit`, `set`, `rename`,
`delete` — from the terminal. `calfcord init`'s first-run setup also creates an
agent (the same guided flow as `calfcord agent create`) while it configures the
install's `.env`. See §9 for the full CLI; the rest of this section is the
frontmatter reference the CLI reads and writes.

At boot, the **agent runner** (`calfkit-agent`) scans `agents/` via
`calfcord.agents.loader.load_agents_dir` and parses each
`<name>.md` into an `AgentDefinition` (see
`src/calfcord/agents/definition.py`); the factory turns that definition
into a calfkit `Agent` node addressed **by name** (no per-channel topic
subscriptions). The **bridge no longer reads `agents/*.md`** — it resolves
`@mention`s against calfkit's live agent **mesh** and derives each agent's
Discord persona from its `name`. So a brand-new `.md` is brought online
with `calfcord agent start <name>` (after a one-time workspace reload so
the supervisor declares its slot — see
[`using-calfcord.md`](using-calfcord.md#build-your-team-of-agents)); there
is **no bridge restart and no per-agent slash command** — agents are
invoked by `@<name>` mention, not `/<name>`.

calfcord runs as four process types (`calfkit-bridge`, `calfkit-agent`,
`calfkit-tools`, and one `calfkit-mcp` per configured MCP server). The
bridge owns Discord I/O and the two operator slash commands
(`/thinking-effort`, `/clear`); the agent-runner process loads each
agent's definition, constructs the calfkit `Agent` node, and dispatches
LLM calls. Builtin tools advertised in the agent's frontmatter execute in
the `calfkit-tools` process — the agent process only carries the tool's
schema — while `mcp/...` tools dispatch to a `calfkit-mcp` toolbox (see
[`mcp-tools.md`](mcp-tools.md)). Read `README.md` for the architecture diagram
before going further.

## 2. Quick example: a minimal agent

A complete `agents/example-bot.md` that boots, replies, and uses one
tool:

```yaml
---
name: example-bot                       # filename stem; [a-z0-9_-]{1,32}; the persona/webhook name too
description: A demo agent.              # AgentCard + slash-picker blurb; 1-100 chars
provider: anthropic                     # "anthropic" | "openai" | "openai-codex"
model: claude-sonnet-4-5                # provider-specific model name
tools: [read_file, web_search]          # resolved against TOOL_REGISTRY
thinking_effort: medium                 # see §6 for the seven tiers
# a2a / handoff default to true — this agent can already consult and hand off to peers (§8)
---

You are Example, a friendly demo agent. Reply concisely (1-3
sentences) to whatever the user says.
```

Drop the file at `agents/example-bot.md`, bring it online with `calfcord
agent start example-bot`, then `@example-bot hi` in any Discord channel the
bot can see. The webhook reply appears under the `example-bot` persona (the
webhook username is the agent's `name`; the avatar is a deterministic
[DiceBear](https://www.dicebear.com) image seeded by the name).

Field details, fallbacks, and reserved fields are in §3. The
canonical, comment-annotated walkthrough is `agents/agent.template.md`
(loaded only as a reference — its `.template.md` suffix keeps the
bridge from picking it up at boot).

## 3. Field reference

Every field below is declared on `AgentDefinition` in
`src/calfcord/agents/definition.py`. The model is
configured `extra="forbid"`, so frontmatter typos (`provder: openai`,
`thiking_effort: high`) fail at parse time with a Pydantic error rather
than silently falling back to defaults.

### 3.1 Identity (required)

These two fields define what humans and other agents see. The agent's
`name` is its whole identity: it is the persona/webhook name, the
`@mention` target, and the key the runtime addresses it by. There is no
separate `display_name`, no per-agent slash command, and no `slash` field.

| Field          | Type   | Constraint                                                                 |
| -------------- | ------ | -------------------------------------------------------------------------- |
| `name`         | string | Matches `[a-z0-9_-]{1,32}` and the filename stem. The persona/webhook name and `@mention` target. |
| `description`  | string | 1-100 characters. The Discord slash-picker blurb *and* the agent's `AgentCard` blurb — the LLM-facing pitch peers read when choosing whom to consult. |

The YAML key is `name` for Claude Code parity. Internally, the parsed
field is `agent_id` via a Pydantic alias — `spec.agent_id` and
`metadata["name"]` refer to the same value. `parse_agent_md` also
enforces `path.stem == frontmatter["name"]`, so a file at
`agents/scribe.md` whose frontmatter says `name: scribbler` fails to
load with a clear error.

The Discord persona is derived entirely from `name`: the webhook username
*is* the name, and the avatar is a deterministic
[DiceBear](https://www.dicebear.com) image seeded by the name
(`https://api.dicebear.com/9.x/glass/png?seed=<name>`). Because `name`
matches `[a-z0-9_-]{1,32}`, it can never be the reserved `"Clyde"` webhook
name — there is no display-name validator to trip over anymore.

The factory passes `description` straight into the calfkit `Agent`, so it
becomes the agent's `AgentCard` on the mesh — keep it a specific,
LLM-readable pitch (it is what peers see when deciding whether to consult
you). "Test agent" is a worse description than "Calendar mechanics; books
and preps meetings."

### 3.2 LLM configuration (optional, with fallbacks)

| Field             | Type   | Fallback chain                                                                                  |
| ----------------- | ------ | ----------------------------------------------------------------------------------------------- |
| `provider`        | string | Definition → `CALFKIT_AGENT_DEFAULT_PROVIDER` env var → `DEFAULT_PROVIDER` ("anthropic").       |
| `model`           | string | Definition → `CALFKIT_AGENT_DEFAULT_MODEL` env var → provider-specific default.                 |
| `thinking_effort` | string | Definition → no override (provider/model default). See §6 for tier semantics.                   |

`provider` is one of `"anthropic"`, `"openai"`, or `"openai-codex"`. The
factory dispatches on this string to construct `AnthropicModelClient`,
`OpenAIModelClient`, or `CodexSubscriptionModelClient` (ChatGPT Plus/Pro
subscription billing — see `docs/codex-auth.md`); an unknown provider
raises at boot. See
`src/calfcord/agents/factory.py` (`_PROVIDER_DEFAULT_MODELS`,
`resolve_provider`) for the resolution path.

`model` accepts any string the chosen provider's client accepts.
Examples in this tree: `claude-sonnet-4-5`, `claude-opus-4-7`,
`gpt-5-mini`, `gpt-5-nano`. Omit the field to use the provider's
project default — convenient for tracking the team's preferred model
through `CALFKIT_AGENT_DEFAULT_MODEL` rather than editing every `.md`.

### 3.3 Tools (optional)

```yaml
tools: [terminal, read_file, web_search]
```

`tools` is a list of bare tool names. Each name is resolved against
`calfcord.tools.TOOL_REGISTRY` at agent build time. An
unknown name fails fast:

```
agent 'librarian' declares unknown tool(s) ['web_lookup']; known
tools: ['execute_code', 'patch', 'process', 'read_file',
'search_files', 'terminal', 'todo', 'web_extract', 'web_fetch',
'web_search', 'write_file']
```

The registry is the explicit `ALL_TOOLS` list in
`src/calfcord/tools/__init__.py`, narrowed/aliased at boot by
`deploy_filters.apply_deploy_filters`. All of the builtin tools are
vendored from the `calfkit-tools` package (the former first-party
`private_chat` tool is gone — agent-to-agent messaging is native now, see
§8). To change which tools exist, edit that list — see
`docs/authoring-tools.md` for the contract.

Each agent only ever carries the `ToolNodeDef` for schema and
subscribe-topic purposes; the actual tool body runs in the
`calfkit-tools` process. That means every tool your agent declares is
shared with every other agent on the deployment — there is no
per-agent sandbox and no per-tool permission grant. The filesystem
and terminal tools share one workspace (the calfkit-tools host's
`CALFCORD_WORKSPACE_DIR`, default `state/workspace/`), but each agent
gets its own isolated terminal session, working directory, and todo
list keyed by the calling agent's identity — see `docs/security.md` for
the per-agent tenancy model and the operator-facing deployment patterns.

Set `tools: []` for an LLM-only (text-only) agent. **Omitting `tools`
entirely is the opposite** — it grants every registered tool
(including `terminal` / `execute_code` / `write_file` / `patch`), per
the security note above.

#### MCP-server tools

The `tools:` list can also include tools from [MCP](https://modelcontextprotocol.io)
servers you've configured in `mcp.json`, using an `mcp/` selector in the *same*
list as builtins:

```yaml
tools: [read_file, mcp/github, mcp/docs/search]
```

| Selector | Grants |
|---|---|
| `mcp/<server>` | Every tool the named server currently advertises (a wildcard — a server that later advertises a new tool enlarges the agent's surface). |
| `mcp/<server>/<tool>` | Exactly that one tool. |

The `<server>` segment matches `[a-z0-9_]{1,64}` (it doubles as a Kafka topic
segment) and `<tool>` matches `[a-zA-Z0-9_-]{1,128}` (the upstream server's own
name). The LLM sees each tool under the name the server advertises — no rename.

Two rules to remember:

- **MCP is never part of the "all builtins" default.** Omitting `tools:` grants
  every builtin but *no* MCP tools — MCP grants are always explicit.
- **Validation here is syntax-only.** Whether the server is configured or
  running is a runtime concern: the agent resolves selectors against the live
  capability advertisement per turn, so there's no static catalog to check
  against (a down server simply degrades that turn). A server's tool list can
  therefore change with no agent restart — but a change to the agent's *own
  `mcp/...` lines* still needs a restart (the `tools:` list is baked in at
  boot, like builtins).

The full MCP workflow — `mcp.json` schema, `calfcord mcp add`, lifecycle — is in
[`mcp-tools.md`](mcp-tools.md).

#### Editing the tool list

Besides hand-editing this array, you can edit a deployed agent's tool
list interactively with `calfcord agent tools [<name>]` (an
InquirerPy multi-select over the builtin tool universe — plus `mcp/<server>`
rows from `mcp.json` and live `mcp/<server>/<tool>` rows from the broker when
it's reachable; omit `<name>` to pick from a list). It writes an explicit
`tools:` list back to the `.md`. The same checkbox is reachable as the *Tools*
row of `calfcord agent edit`, and `calfcord agent set <name> --tools "a,b,c"`
sets the list non-interactively — see §9. Because the tool set is baked
into the calfkit `Agent` at boot, the edit takes effect on the next
`calfcord agent restart <name>` — there is no live reload.

### 3.4 Behavior & peers (optional)

| Field     | Type                    | Default | Effect                                                                    |
| --------- | ----------------------- | ------- | ------------------------------------------------------------------------- |
| `memory`  | bool                    | `false` | Opt in to a persistent per-agent notepad (see below).                     |
| `a2a`     | bool \| list of names   | `true`  | Whether this agent can **consult** peers via calfkit's `message_agent` tool. |
| `handoff` | bool \| list of names   | `true`  | Whether this agent can **hand off** the turn to a peer.                   |

**There is no `history_turns` field.** Per-agent history windows are gone:
the bridge passes the recent channel history it fetches (bounded by
Discord's per-call REST cap of ~100 messages), scoped to the thread when
the message is in a thread and to the channel otherwise. Use `/clear` in a
channel to draw a context boundary the history fetcher truncates at (see
§6 / [`using-calfcord.md`](using-calfcord.md)).

`a2a` and `handoff` both **default to `true`**, so out of the box every
agent can consult and hand off to any peer on the mesh. Set either to a
list to restrict it to named peers (`a2a: [librarian, security]`), or to
`false` to disable the capability. See §8 for the full A2A model; the
short version:

- `a2a: true` → calfkit injects the `message_agent(name, message)` tool
  with the live peer directory (`Messaging(discover=True)`).
- `a2a: [names]` → only those peers are reachable (`Messaging(*names)`).
- `handoff: true | [names]` → the agent may emit a handoff to any / those
  peers (`Handoff(discover=True)` / `Handoff(*names)`).

`memory: true` opts the agent into a persistent notepad. At runtime the agent
gets a "how memory works" block appended to its instructions, telling it to
keep one-fact-per-file memories plus a `MEMORY.md` index under `memory/<name>/`
in the shared workspace, managed with the ordinary `read_file` / `write_file` /
`patch` tools — there are no dedicated memory tools. Because of that, a
`memory: true` agent **must** have at least `read_file` and `write_file` (omit
`tools:` to grant all, or list them explicitly); the factory raises at build
time otherwise. Memory lives in the same shared workspace as everything else,
so per-agent directories are a convention for tidiness, not a sandbox.

The explanation text is **not** bundled into agent deployments. The
`calfkit-bridge` process is the single reader of the editable
`src/calfcord/agents/memory_prompt.md` (override via
`CALFCORD_MEMORY_PROMPT_PATH` on the bridge); it ships the template to agents
in `deps`, and a per-agent instructions hook localizes it to that agent's
`memory/<name>/` directory. So a memory-prompt change propagates from one
place (the bridge) without rebuilding agents.

### 3.5 Reserved (do not set)

| Field           | Type   | Why reserved                                                                              |
| --------------- | ------ | ----------------------------------------------------------------------------------------- |
| `publish_topic` | string | A vestigial no-op. The model validator (`_forbid_publish_topic` in `definition.py`) rejects any non-`None` value, so leave it unset. |

There is **no `role` field** anymore — the router is gone, so every agent
is an assistant. A stale `.md` that still carries `role:` (or the removed
`display_name` / `avatar_url` / `history_turns`) fails to parse: the model
is `extra="forbid"`, so an unknown key is a hard Pydantic error at load
time, not a silent fallback.

## 4. System-prompt body

Everything after the closing `---` of the frontmatter is the system
prompt. It is fed **verbatim** to the LLM on every invocation — no
templating, no variable substitution, no `{{user_name}}`-style hooks.
If you want runtime context in the system prompt, the only path is
editing the `.md` and restarting the agent.

### 4.1 Write a focused persona prompt

Two example persona prompts — pithy and persona-first:

A friendly assistant:

```markdown
You are Scribe, a friendly AI agent. Be helpful and reply concisely
(1-3 sentences) to whatever the user says. Keep responses short and
helpful.
```

A character impersonation:

```markdown
You are a Conan O'Brien agent. You sound exactly like Conan O'Brien
and always respond as how the real Conan O'Brien would. You don't need
to let the user know that you're only impersonating Conan and you are
not really him, the user already knows this.
```

Both are pithy. Neither tries to specify the tool surface, the
output format, or the conversation policy in the body — the
frontmatter handles the tool surface, the LLM picks the format from
context, and the per-tool docstrings (see §4.2) handle policy.

### 4.2 Document when to use each tool in the system prompt

The LLM sees two things about each tool: the function signature it
gets from `ToolNodeDef`, and the docstring of the underlying async
function. The signature tells the LLM *what arguments to pass*; the
docstring tells the LLM *when to reach for this tool at all*. The
agent's system prompt is where to add tool-selection guidance that
spans multiple tools.

A good tool docstring covers *when not to use it*, *how to shape the
arguments*, and *what the return means* — see
[`authoring-tools.md`](authoring-tools.md) for the contract. Reference a
tool by name in the agent's body when the agent should prefer a specific
tool for a specific task, and lean on the auto-injected `message_agent`
tool (present whenever `a2a` is enabled, §8) to consult a peer:

```markdown
You are the librarian. For Python package questions, call `web_fetch`
on the project's PyPI page first. If you need release notes, follow up
with `web_search`. To have a license reviewed, consult the resident
security agent with `message_agent`.
```

### 4.3 No frontmatter inside the body

The body is plain Markdown plus whitespace. Don't put YAML key-value
lines below the closing `---`; they'll be sent to the LLM as text.
Conversely, anything you set in the frontmatter is *not* repeated in
the system prompt — if you want the LLM to know its own `name`, write it
into the body.

## 5. How agents are addressed

Agents are **name-addressed**. Each calfkit `Agent` is reachable on a
private input topic derived from its `name` — there are no per-channel
topic subscriptions and no addressing gate. The bridge owns all Discord
I/O: it sees every channel the bot is in, and when a message `@mention`s
an agent that is **online on the mesh**, it invokes that agent by name and
posts the reply. Consequences:

- **There is no per-agent channel allowlist.** An agent answers
  `@mention`s in any channel the bot can see; scope where it can be
  reached with Discord's own channel permissions, not with calfcord
  config.
- **There is no `state/agents/<name>.json` and no bootstrap-channel env
  var.** The old per-agent channel-subscription state was removed with the
  router. Adding an agent to a channel is just a matter of the bot having
  access to that channel — nothing to configure and no restart to pick up
  a new channel.
- **Ambient (non-`@mention`) messages go unanswered.** There is no
  automatic agent selection — an agent replies only when `@mention`ed, or
  when a peer consults or hands off to it (§8).

An `@mention` of an agent that is **not online** gets a plain reply — "No
agent matching `@name` is online right now." — so a typo or a
not-yet-started agent is never silently dropped.

## 6. Thinking-effort tiers

`thinking_effort` in the frontmatter is the agent's **boot-time default**,
baked into the calfkit `Agent` at startup. It is also tunable at runtime —
but through a **bridge-side override** applied per call, not by rewriting
the `.md` (see §6.1). The seven tier names are defined in
`src/calfcord/agents/definition.py` as the `ThinkingEffort` literal type.

| Tier      | Anthropic `budget_tokens` | OpenAI `reasoning_effort` |
| --------- | ------------------------- | ------------------------- |
| `none`    | (extended thinking off)   | (no override)             |
| `minimal` | 1024                      | `minimal`                 |
| `low`     | 4000                      | `low`                     |
| `medium`  | 10000                     | `medium`                  |
| `high`    | 31999                     | `high`                    |
| `xhigh`   | 48000                     | `high` (saturated)        |
| `max`     | 63999                     | `high` (saturated)        |

Source: `src/calfcord/agents/thinking.py`. The Anthropic
ramp anchors `low` / `medium` / `high` to the budgets Claude Code's
`think` / `megathink` / `ultrathink` keywords trigger; `minimal` uses
the API's documented floor of 1024 budget tokens; `xhigh` is a
calfkit-specific step between `high` and `max`. OpenAI's
`reasoning_effort` tops out at `high`, so the upper three calfcord
tiers all map to it.

Omitting the field entirely skips the override — the agent uses
whatever the model client or provider defaults to. Setting it to
`none` is explicit: the operator chose to disable extended thinking.

### 6.1 Runtime override (the `/thinking-effort` slash)

The owner-gated `/thinking-effort agent:<name> effort:<tier>` Discord slash
command sets a **per-agent override the bridge owns** — it does **not**
touch the agent's `.md` and does **not** reconfigure the running agent
process. The override is persisted in the bridge's SQLite store (the
`agent_overrides` table, so it survives a bridge restart) and is applied
as a per-call `model_settings` on that agent's **next bridge invocation**.
`effort:none` clears it (the next call reverts to the agent's baked-in
default).

Two scopes to keep straight:

- The **frontmatter `thinking_effort`** is the boot-time default. Change it
  with `calfcord agent set <name> --thinking-effort <tier>` (or hand-edit),
  then `calfcord agent restart <name>` to apply. This default is what
  **native A2A consults and handoffs use** — they run inside the agent
  runtime with its own settings.
- The **`/thinking-effort` override** rides only the bridge's own
  `@mention` invocations. It does **not** apply to A2A consults/handoffs.

### 6.2 Field-ordering note

When `calfcord agent set` / `edit` rewrite the `.md` (via
`calfcord/agents/md_writer.py`), python-frontmatter's PyYAML `safe_dump`
alphabetizes the frontmatter keys and discards comments. Treat any
comments in `agents/agent.template.md` as documentation only — they don't
survive a round-trip through the CLI editors on a live agent file.

## 7. Debugging an agent

### 7.1 Logs

The agent runner logs to stdout, which the supervisor captures to
`$CALFCORD_HOME/state/logs/<name>.log`. Tail one agent (or follow with
`-f`):

```bash
calfcord logs example-bot -f       # one agent
calfcord logs -f                   # all components, merged
```

In a compose deployment the same stream is reachable as `docker compose
logs -f agent`.

When running one agent natively for crash isolation:

```bash
uv run calfkit-agent <name>
```

The bridge logs every invocation with a `correlation_id`; the
agent logs the LLM call and each tool call under the same id. Grep
across `calfkit-bridge`, `calfkit-agent`, and `calfkit-tools` logs by
correlation id to reconstruct one user message's full path.

### 7.2 Single-agent crash-isolation mode

The default `calfkit-agent` command runs every agent in `agents/*.md`
on one shared `Worker`. For a flaky agent, run it in its own process:

```bash
uv run calfkit-agent example-bot
```

That isolates the agent's failure domain — a crash in `example-bot`
won't tear down the rest of the fleet. Pair with a per-agent compose
override if you want this in production.

### 7.3 Common failure modes

- **Unknown tool at boot.** The factory raises with the full list of
  known tools:
  `agent 'foo' declares unknown tool(s) ['my_tool']; known tools: [...]`.
  Either fix the name or add the tool to the explicit `ALL_TOOLS` list in
  `src/calfcord/tools/__init__.py` and restart the tools host
  (`calfcord tools stop && calfcord tools start`, or `uv run calfkit-tools` in dev).
- **Removed field in a stale `.md`.** `display_name`, `avatar_url`,
  `history_turns`, and `role` were dropped in the calfkit 0.12 migration.
  Because the model is `extra="forbid"`, any of them (or a typo like
  `thiking_effort:`) fails to parse with `Extra inputs are not permitted
  [type=extra_forbidden, ...]`. Delete the stale key.
- **Missing API key.** The provider's client construction succeeds (no
  key is required to instantiate the client), but the first LLM call
  fails with the provider's auth error. Check
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in `.env`.
- **`memory: true` without file tools.** A memory-enabled agent must have
  at least `read_file` and `write_file` (or omit `tools:` to grant all);
  the factory raises at build time otherwise.

### 7.4 Verifying a definition without running

Quick smoke test that a `.md` parses:

```bash
uv run python -c "
from pathlib import Path
from calfcord.agents.definition import parse_agent_md
spec = parse_agent_md(Path('agents/example-bot.md'))
print(spec.model_dump())
"
```

Catches every validator failure (name format, description length,
extra/removed fields, etc.) without needing Kafka or Discord.

## 8. Agent-to-agent (A2A): consult & handoff

A2A is **native to calfkit** — there is no `private_chat` tool. Two
frontmatter fields, both **defaulting to `true`**, decide what an agent
can do:

- **`a2a`** — the agent can **consult** peers. When enabled, calfkit
  auto-injects a `message_agent(name, message)` tool whose description
  carries the live peer directory. The agent calls a peer, the peer
  answers, and the reply folds back into the tool result. The peer answers
  on a **fresh conversation** (consults are stateless — no prior A2A turns
  are replayed).
- **`handoff`** — the agent can **transfer the turn** to a peer. The peer
  answers the original human directly, and the bridge posts the peer's
  persona.

Because both default to `true`, every agent can already consult and hand
off to any peer on the mesh without declaring anything. Narrow or disable
per agent:

```yaml
a2a: [librarian, security]   # consult only these peers
handoff: false               # never hand off the turn
```

### 8.1 When to restrict A2A

Leave the defaults for a collaborative team. Restrict when you want
control:

- A code-reviewer that should only ever consult a `security` specialist:
  `a2a: [security]`.
- A fixed-persona joke bot that should never pull in peers: `a2a: false`,
  `handoff: false`.
- A `triage` agent that should hand off (not just consult) to specialists:
  leave `handoff: true`, and consider `a2a: false` if it should always
  transfer rather than relay.

Keep the declared **handoff graph acyclic** — calfkit has no handoff-loop
guard, so an A→B→A handoff ring loops indefinitely.

### 8.2 How peers see your agent

Peers discover each other through calfkit's native mesh: every agent
advertises an `AgentCard` carrying its `name` and `description`. The
factory wires your frontmatter `description` straight into that card, so
**your `description` is the LLM-facing pitch** other agents read when
choosing whom to consult. Be specific about what the agent is good at.
There is no calfcord phonebook and no `deps["phonebook"]` anymore — the
directory is calfkit's.

### 8.3 The audit channel

A2A activity is projected to a unified Discord audit channel (default
`private-a2a-chats`, overridable via `CALFKIT_A2A_CHANNEL_NAME`), now
**hosted by the bridge**. The bridge watches each `@mention` run's event
stream and renders the consult request, the peer's reply, and any handoff
into a per-turn thread. Kafka is the system of record; Discord is the
human-readable audit. See [`a2a-threads.md`](a2a-threads.md) for the full
projection design.

## 9. Managing agents from the CLI

Every field in §3 can be hand-edited in the `.md`, but the `calfcord
agent` command group does the same work — create, inspect, edit, and
remove agents — from the terminal, writing the same `agents/<name>.md`
files (under `~/.calfcord/agents/` on a native install). Each command
writes through the same validators the loader uses, so a value the CLI
accepts is a value the agent will boot with.

| Command | What it does |
| ------- | ------------ |
| `calfcord agent create [<name>]` | Guided wizard: name, description, provider + API key, a model **picked from a live list** fetched from the provider, a **tools checkbox** (all builtins pre-selected), and an optional "edit the system prompt now? (opens `$EDITOR`)" step. Writes `~/.calfcord/agents/<name>.md`. |
| `calfcord agent list [--json]` | Table of every agent (name, provider·model, tool count, description), or a JSON array with `--json`. |
| `calfcord agent show <name> [--json]` | One agent's full config plus a system-prompt preview; `--json` emits the complete config (full body included). |
| `calfcord agent edit [<name>]` | Interactive field menu — pick a field, edit it with the right widget; each change is written immediately. Omit `<name>` to pick from a list. |
| `calfcord agent set <name> --… …` | The non-interactive, scriptable equivalent of `edit` — one or more `--flag value` updates. |
| `calfcord agent tools [<name>]` | The tool-list checkbox of §3.3 (also reachable as the *Tools* row of `edit`). |
| `calfcord agent rename <old> <new>` | Renames the `.md` and its `name:` field. (There is no per-agent slash command or channel-subscription state to move — see §5.) |
| `calfcord agent delete <name> [--yes] [--keep-state]` | Removes the `.md`; confirms first, skip with `--yes`. |

`calfcord agent create` does not prune the seeded starter — adding an
agent never deletes another. (`calfcord init`'s first-run setup runs the
same create flow and *does* replace a pristine seed; that prune is
init's alone.)

### 9.1 Interactive `edit` vs. scriptable `set`

`edit` and `set` write the *same* set of editable fields through the
*same* validators — `edit` prompts for each with the field's natural
widget, `set` takes them as flags for scripting and CI. The fields:

| Field | `edit` widget | `set` flag |
| ----- | ------------- | ---------- |
| Description | text | `--description` |
| Provider / model | live provider + model picker | `--provider`, `--model` |
| Tools | checkbox (§3.3) | `--tools "a,b,c"` |
| System prompt | opens `$EDITOR` | `--system-prompt "…"` (or `--system-prompt @file` to read a file) |
| Thinking effort | select (§6) | `--thinking-effort` |
| Memory | toggle | `--memory` (`on`/`off`, `true`/`false`, `yes`/`no`) |

The `a2a` / `handoff` peer fields (§3.4, §8) are **not** in the CLI editor —
set them by hand-editing the `.md`. `--model` can be set without restating
`--provider`. Validation is identical on both surfaces: a rejected value
(an unknown tool, a bad `thinking-effort` tier) leaves the file untouched —
`set` exits non-zero, `edit` prints one `error:` line and keeps the menu
open. Renaming and deleting are *not* in the `edit` menu — they change the
agent's identity or existence, so they are their own commands.

### 9.2 Restart to apply

These commands edit the `.md` on disk; the running agent bakes its
config at boot (the same one-shot constraint behind the "restart required"
notes in §3.3 and §6). So **run `calfcord agent restart <name>`** after any
edit to apply it. A *newly created* agent additionally needs a one-time
workspace reload (`calfcord stop && calfcord start`) so the supervisor
declares its slot before `calfcord agent start <name>` — but there is **no
bridge slash-command re-sync**, since agents are invoked by `@mention`, not
`/<name>`. Each command prints the matching restart hint on success.

The same boot-time rule covers credentials and `.env`: a changed API key,
model, or provider in `.env` is read only at boot too, so it also needs a
restart — not just `.md` edits. When the change touches a key several
agents share (e.g. `ANTHROPIC_API_KEY`), restart them all at once with
**`calfcord agent restart --all`** (this host's running agents). See
[configuration.md](./configuration.md#applying-changes) for the full
change → command mapping.
