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

At boot, the bridge scans `agents/` via
`calfcord.agents.loader.load_agents_dir` and parses each
`<name>.md` into an `AgentDefinition` (see
`src/calfcord/agents/definition.py`). The definitions feed
the agent registry that powers ingress routing, the phonebook
propagated to A2A peers, and the slash-command tree the bridge
registers with Discord. **A brand-new `.md` file is picked up by
`calfcord agent start <name>` (it brings the new teammate online); the
bridge owns the `/<name>` slash command, so a newly created or renamed
agent also needs a bridge restart — `calfcord stop && calfcord start`**
— the registry scan and the calfkit `Worker.register_handlers` are both
one-shot at startup.

calfcord runs as four processes (`calfkit-bridge`, `calfkit-agent`,
`calfkit-router`, `calfkit-tools`). The bridge owns Discord I/O and the
slash-command tree; the agent-runner process loads each agent's
definition, constructs the calfkit `Agent` node, and dispatches LLM
calls. Tools advertised in the agent's frontmatter execute in the
`calfkit-tools` process — the agent process only carries the tool's
schema. Read `README.md` for the architecture diagram before going
further.

## 2. Quick example: a minimal agent

A complete `agents/example-bot.md` that boots, replies, and uses one
tool:

```yaml
---
name: example-bot                       # filename stem; [a-z0-9_-]{1,32}; also the slash command (/example-bot)
display_name: Example                   # webhook username; 1-80 chars; not "Clyde"
description: A demo agent.              # slash-picker blurb; 1-100 chars
# avatar_url: ...                       # optional; omit for DiceBear default seeded by name
provider: anthropic                     # "anthropic" | "openai" | "openai-codex"
model: claude-sonnet-4-5                # provider-specific model name
tools: [private_chat]                   # resolved against TOOL_REGISTRY
thinking_effort: medium                 # see §6 for the seven tiers
history_turns: 30                       # channel-history depth, 0-100
---

You are Example, a friendly demo agent. Reply concisely (1-3
sentences) to whatever the user says.
```

Drop the file at `agents/example-bot.md`, bring it online with `calfcord
agent start example-bot`, then `@example-bot hi` in a Discord channel the
agent is subscribed to. (A brand-new agent also needs the bridge to learn
its `/<name>` slash command — `calfcord stop && calfcord start` once after
the first create.) The webhook reply will appear under the `Example`
persona.

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

These three fields define what Discord and other agents see. They are
immutable in practice — the bridge's command tree, the persona
webhook, and the phonebook all index by them. The Discord slash
command is always `/<name>`; there is no separate `slash` field.

| Field          | Type   | Constraint                                                                 |
| -------------- | ------ | -------------------------------------------------------------------------- |
| `name`         | string | Matches `[a-z0-9_-]{1,32}` and the filename stem. Also the slash command.  |
| `display_name` | string | 1-80 characters. The literal `"Clyde"` is rejected by Discord's webhooks. |
| `description`  | string | 1-100 characters (Discord's slash-command description cap).                |

The YAML key is `name` for Claude Code parity. Internally, the parsed
field is `agent_id` via a Pydantic alias — `spec.agent_id` and
`metadata["name"]` refer to the same value. `parse_agent_md` also
enforces `path.stem == frontmatter["name"]`, so a file at
`agents/scribe.md` whose frontmatter says `name: scribbler` fails to
load with a clear error.

`display_name == "Clyde"` is the one display name Discord's webhook
API rejects unconditionally (it's reserved for Discord's own AI). The
validator catches this at load time so you don't have to debug a
late-stage webhook 400.

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
tools: [private_chat, shell, read_file]
```

`tools` is a list of bare tool names. Each name is resolved against
`calfcord.tools.TOOL_REGISTRY` at agent build time. An
unknown name fails fast:

```
agent 'librarian' declares unknown tool(s) ['web_lookup']; known
tools: ['edit_file', 'glob', 'grep', 'private_chat', 'read_file',
'shell', 'todo_view', 'todo_write', 'web_fetch', 'web_search',
'write_file']
```

The registry is populated by `tools/discovery.py` at process import
time. To add a new tool to the registry, drop a `.py` file under
`src/calfcord/tools/builtin/` — see
`docs/authoring-tools.md` for the contract.

Each agent only ever carries the `ToolNodeDef` for schema and
subscribe-topic purposes; the actual tool body runs in the
`calfkit-tools` process. That means every tool your agent declares is
shared with every other agent on the deployment — there is no
per-agent sandbox and no per-tool permission grant. The Filesystem
and shell tools share one workspace (the calfkit-tools host's
`CALFCORD_WORKSPACE_DIR`, default `state/workspace/`). See
`docs/security.md` for the operator-facing deployment patterns.

Set `tools: []` for an LLM-only (text-only) agent. **Omitting `tools`
entirely is the opposite** — it grants every registered builtin
(including `shell` / `write_file` / `edit_file`), per the security note
above.

Besides hand-editing this array, you can edit a deployed agent's tool
list interactively with `calfcord agent tools [<name>]` (an
InquirerPy multi-select over the builtin + MCP tool universe; omit
`<name>` to pick from a list). It writes an explicit `tools:` list back
to the `.md`. The same checkbox is reachable as the *Tools* row of
`calfcord agent edit`, and `calfcord agent set <name> --tools "a,b,c"`
sets the list non-interactively — see §9. Because the tool set is baked
into the calfkit `Agent` at boot, the edit takes effect on the next
`calfcord agent restart <name>` — there is no live reload.

### 3.4 Behavior (optional)

| Field           | Type | Default | Range  | Effect                                                                      |
| --------------- | ---- | ------- | ------ | --------------------------------------------------------------------------- |
| `history_turns` | int  | `30`    | 0-100  | Number of recent channel messages the bridge projects into `message_history`. |
| `memory`        | bool | `false` | —      | Opt in to a persistent per-agent notepad (see below).                          |

`history_turns: 0` disables history entirely — no Discord REST call,
agent runs with only the system prompt and the user prompt. Useful for
single-turn personas or for cost-sensitive deployments where the
channel history is too noisy to be useful.

The upper bound of 100 is Discord's per-call REST cap for
`channel.history(limit=...)`. The default of 30 (~3K input tokens at
~100 tokens per message) is the v1 balance between context quality and
cost.

`memory: true` opts the agent into a persistent notepad. At runtime the agent
gets a "how memory works" block appended to its instructions, telling it to
keep one-fact-per-file memories plus a `MEMORY.md` index under `memory/<name>/`
in the shared workspace, managed with the ordinary `read_file` / `write_file` /
`edit_file` tools — there are no dedicated memory tools. Because of that, a
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
| `role`          | string | `"assistant"` is the default and the only value user-authored agents should use.          |
| `publish_topic` | string | Router-only Kafka publish topic. The model validator rejects it for non-router agents.    |

`role: router` is reserved for the singleton built-in routing agent
constructed by `build_router_definition` in
`calfcord/router/definition.py`. A user agent with
`role: router` will boot but collide with the real router in the
registry. Don't set this field.

`publish_topic` is the router's structured-output destination. The
validator (`_validate_router_constraints` in `definition.py`) rejects
assistants that declare it — assistants emit `ReturnCall` to the
inbound frame's `callback_topic` (the bridge's `discord.outbox`), not
to a fixed published topic.

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

The canonical "good docstring" reference is `private_chat` at
`src/calfcord/tools/builtin/private_chat.py` — it
explicitly covers *when not to use*, *how to write the `content`
argument*, and *what the return shape means*. Lean on that pattern for
tools you write, and reference the tool by name in the agent's body
when the agent should prefer a specific tool for a specific task:

```markdown
You are the librarian. For Python package questions, call `pypi_info`
first. If the result is thin or you need release notes, follow up with
`web_fetch` on the project's home page. Reserve `private_chat` for
asking the resident security agent to review a license.
```

### 4.3 No frontmatter inside the body

The body is plain Markdown plus whitespace. Don't put YAML key-value
lines below the closing `---`; they'll be sent to the LLM as text.
Conversely, anything you set in the frontmatter is *not* repeated in
the system prompt — if you want the LLM to know its own
`display_name`, write it into the body.

## 5. Channel subscriptions

An agent's identity (and the tool surface) lives in
`agents/<name>.md`. The set of Discord channels the agent listens on
lives in `state/agents/<name>.json` — see
`src/calfcord/agents/state.py`. Channel subscriptions are
**runtime state**, not identity, and so they're tracked separately and
written atomically by the agent process.

### 5.1 First-boot seeding

On first boot, an agent has no state file. The runner
(`calfcord/agents/runner.py`) seeds the channel list from
two environment variables, in order:

1. `CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` — comma-separated
   Discord channel IDs for this specific agent. Underscores replace
   hyphens (`example-bot` reads `CALFKIT_AGENT_EXAMPLE_BOT_BOOTSTRAP_CHANNELS`).
2. `DISCORD_DEFAULT_CHANNEL_ID` — the shared dev fallback used when
   no per-agent var is set. Convenient when every agent in a small
   deployment should listen on the same channel.

Once `state/agents/<name>.json` exists the bootstrap env var is ignored
with a `WARNING` log — the persisted state is canonical. **Clear the
bootstrap env var from `.env` after first successful boot** to prevent
accidental re-seeding if the state file is later deleted.

### 5.2 Restart required for new channels

Calfkit's `Worker.register_handlers` is one-shot at startup, so adding
a channel ID to the runtime state file (or via a future `store.add_channel`
call) does **not** change the running agent's Kafka subscriptions. The
agent factory's docstring spells this out:

> Worker subscription is fixed at boot. [...] Adding a channel to an
> existing agent requires a process restart.

The `store` parameter on `AgentFactory.build` exists for forward
compatibility with a not-yet-implemented dynamic-subscribe path. In v1,
the workflow for adding an agent to a new channel is:

1. Edit `state/agents/<name>.json` to add the channel ID, or set the
   bootstrap env var and delete the state file.
2. Restart the agent with `calfcord agent restart <name>` so it
   re-reads its subscriptions.

## 6. Thinking-effort tiers

`thinking_effort` is the one frontmatter field that is operator-tunable
at runtime via the `/thinking-effort` Discord slash command (the
command rewrites the file in place — see
`calfcord/agents/md_writer.py`). The seven tier names are
defined in `src/calfcord/agents/definition.py` as the
`ThinkingEffort` literal type.

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

### 6.1 Runtime tunability

The `/thinking-effort agent:<name> effort:<tier>` Discord slash command
rewrites the `.md` file's frontmatter via `md_writer.update_thinking_effort`.
Slash-invocation and `@<agent_id>` mentions pick up the new value on
the next message — the bridge resolves the override per-call via
`BridgeIngress` (see
`src/calfcord/bridge/ingress.py`).

Ambient-channel messages do **not** pick up runtime changes without an
agent restart. The tier is also baked into the calfkit `Agent`
constructor at agent boot, and ambient routing has no per-call override
path. If you change effort and want the agent's ambient behavior to
shift, run `calfcord agent restart <name>`.

### 6.2 Field-ordering note

When `/thinking-effort` rewrites the `.md`, python-frontmatter's
PyYAML `safe_dump` alphabetizes the frontmatter keys and discards
comments. Treat any comments in `agents/agent.template.md` as
documentation only — they don't survive a round-trip on a live agent
file.

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
  Either fix the name or add the tool under
  `src/calfcord/tools/builtin/` and restart the tools host
  (`calfcord tools stop && calfcord tools start`, or `uv run calfkit-tools` in dev).
- **`display_name == "Clyde"`.** Pydantic rejects this at parse time
  with a clear validator error — the agent never boots.
- **Missing API key.** The provider's client construction succeeds (no
  key is required to instantiate the client), but the first LLM call
  fails with the provider's auth error. Check
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in `.env`.
- **Frontmatter typo.** `extra="forbid"` means
  `thiking_effort: medium` fails to parse with
  `Extra inputs are not permitted [type=extra_forbidden, ...]`. Fix
  the key name.
- **Bootstrap channels not picked up.** Check that the agent has no
  existing `state/agents/<name>.json`. The bootstrap env var is a
  one-shot seed and is ignored once the file exists. Delete the file
  to re-seed (be careful — this also clears any channels added at
  runtime).

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

Catches every validator failure (display_name, name format, etc.)
without needing Kafka or Discord.

## 8. A2A patterns

calfcord supports agent-to-agent collaboration through the
`private_chat` builtin tool. Two agents wire up A2A by both declaring
the tool in their frontmatter; the bridge populates the phonebook each
agent sees, and either side can route a focused sub-task to the other.

### 8.1 When to declare `private_chat`

Declare `private_chat` on agents that have a reason to consult another
agent's expertise, persona, or access. Examples:

- A general-purpose agent that should delegate Python-packaging
  questions to a `librarian` agent.
- A `triage` agent that picks a specialist and forwards the user's
  request.
- A code-reviewer agent that asks a security specialist for a
  second opinion before approving a change.

Agents that have nothing to delegate (a fixed-persona joke bot, an
agent that only ever needs `read_file`) don't need it.

### 8.2 How peers see your agent

The bridge constructs the phonebook at boot from the agent registry and
propagates it to A2A peers via `deps["phonebook"]`. Each
`PhonebookEntry` (`calfcord/agents/phonebook.py`) carries
the peer's `agent_id`, `display_name`, `description`, and tool list.
That's what every other agent sees when picking a peer to call.

Implication: your `description` is the LLM-facing pitch for your
agent's services in any A2A interaction. Treat it as
LLM-readable: be specific about what the agent is good at and what it
isn't. "Test agent" is a worse description than "Calendar mechanics;
books and preps meetings."

### 8.3 The audit channel

Every A2A exchange is projected to a unified Discord audit channel
(default `private-a2a-chats`, overridable via `CALFKIT_A2A_CHANNEL_NAME`). The
caller's request and the target's reply each appear as the appropriate
persona's webhook message, anchored in a per-conversation thread. See
`docs/a2a-threads.md` and
`src/calfcord/bridge/egress.py` for the projection design.

Kafka is the system of record; Discord is the projection. The audit
view is for humans observing the organization, not for state recovery.

### 8.4 Continuation threads

`private_chat` returns the target's reply prefixed with
`<thread_id>{id}</thread_id>\n`. Passing that id back as the
`thread_id` argument on a subsequent call continues the conversation —
the target sees the prior turns as `message_history`. The id tag is
internal and the calling agent should not echo it to the user; the
`private_chat` docstring spells this out.

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
| `calfcord agent rename <old> <new>` | Renames the `.md`, the `name:`/`/<name>` slash command, **and** moves the agent's channel-subscription state (§5) so it isn't orphaned. |
| `calfcord agent delete <name> [--yes] [--keep-state]` | Removes the `.md` (and the state file unless `--keep-state`); confirms first, skip with `--yes`. |

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
| Display name | text | `--display-name` |
| Provider / model | live provider + model picker | `--provider`, `--model` |
| Tools | checkbox (§3.3) | `--tools "a,b,c"` |
| System prompt | opens `$EDITOR` | `--system-prompt "…"` (or `--system-prompt @file` to read a file) |
| Thinking effort | select (§6) | `--thinking-effort` |
| History turns | number (0-100) | `--history-turns` |
| Memory | toggle | `--memory` (`on`/`off`, `true`/`false`, `yes`/`no`) |
| Avatar URL | text | `--avatar-url` |

`--model` can be set without restating `--provider`. Validation is
identical on both surfaces: a rejected value (an out-of-range
`history-turns`, an unknown tool, a `display_name` of `"Clyde"`) leaves
the file untouched — `set` exits non-zero, `edit` prints one `error:`
line and keeps the menu open. Renaming and deleting are *not* in the
`edit` menu — they change the agent's identity or existence, so they are
their own commands.

### 9.2 Restart to apply

These commands edit the `.md` on disk; the running agent bakes its
config at boot (the same one-shot constraint behind every "restart
required" note in §3.3, §5.2, and §6.1). So **run `calfcord agent
restart <name>`** after any edit to apply it — and for a newly created
or renamed agent **also bounce the bridge** (`calfcord stop && calfcord
start`), since the bridge owns the `/<name>` slash command. Each command
prints the matching restart hint on success.
