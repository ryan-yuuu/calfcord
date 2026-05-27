# Calfcord

[![CI](https://github.com/ryan-yuuu/calfcord/actions/workflows/ci.yml/badge.svg)](https://github.com/ryan-yuuu/calfcord/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

A multi-agent organization that lives on Discord. Each agent is an LLM-backed [calfkit](https://github.com/calf-ai/calfkit-sdk) node with its own Discord persona; agents collaborate with one another over Kafka, and humans interact with them through normal Discord channels and slash commands.

## Architecture

Four independent processes, each safe to deploy on its own host:

- **`calfkit-bridge`** — the single Discord gateway. Loads the agent registry from `agents/*.md`, normalizes inbound Discord events to a wire format, publishes them to per-channel Kafka topics, and posts agent replies back to Discord as persona webhooks.
- **`calfkit-agent`** — runs one or all agents as calfkit `Agent` nodes. Each agent subscribes to its configured channel topics plus a private `agent.{agent_id}.in` inbox used for direct agent-to-agent (A2A) calls.
- **`calfkit-router`** — the ambient-channel router. Decides which agent (if any) should handle a non-@-mentioned message in a watched channel. See `docs/ambient-routing.md`.
- **`calfkit-tools`** — runs the A2A `private_chat` tool plus the builtin filesystem / shell / search / web / todo tools. Intentionally decoupled from the bridge (see below).

All four communicate exclusively through Kafka. The only Discord-touching processes are the bridge (gateway + outbox) and the tools runner (projection of A2A exchanges to a per-conversation thread under the unified `a2a-audit` channel).

## Decoupled deployment

The four processes have intentionally different access requirements:

| Resource                              | Bridge | Agent           | Router | Tools |
|---------------------------------------|:------:|:---------------:|:------:|:-----:|
| `agents/*.md` (local files)           |   yes  | yes (own only)  |  yes   |  no   |
| Discord bot token (env var)           |   yes  | yes             |  yes   |  yes  |
| Kafka broker                          |   yes  | yes             |  yes   |  yes  |
| LLM provider API key                  |   —    | yes             |  yes   |  —    |

The tools deployment is registry-free by design. It has no read access to `agents/*.md`. Agent identities (display name, avatar, description, tools) arrive over Kafka in a `phonebook` field that the bridge places in every invocation's `deps`. Calfkit propagates `deps` through agent → tool, so the phonebook reaches `private_chat` with no local file dependency. Practical consequences:

- The tools process can run on a host with no shared filesystem with the bridge.
- The bridge is the single source of truth for "what agents exist."
- Future hot-add support on the bridge's registry takes effect without any agent or tool restart.

## Agents

Each agent is one Markdown file under `agents/`:

```markdown
---
name: scribe
slash: /scribe
display_name: Scribe
description: Friendly assistant that answers concisely.
avatar_url: https://api.dicebear.com/9.x/glass/png?seed=scribe
provider: openai
model: gpt-5-nano
tools: [private_chat]
thinking_effort: medium
---

You are Scribe, a friendly AI agent. Be helpful and reply concisely (1–3 sentences).
```

The YAML frontmatter declares identity and runtime hints; the body is the LLM system prompt. The filename stem must match `name`.

Field summary:

- `name` — unique agent id; lowercase, `[a-z0-9_-]{1,32}`.
- `slash` — slash-command name (must start with `/`).
- `display_name` — webhook persona name (1–80 chars, Discord rejects literal `Clyde`).
- `description` — short summary (1–100 chars). Shown to peers in the A2A roster.
- `avatar_url` — optional persona avatar.
- `provider` — `anthropic` or `openai`. Falls back to `CALFKIT_AGENT_DEFAULT_PROVIDER` env, then `anthropic`.
- `model` — provider-specific model id. The provider-default fallback chain lives in `agents/factory.py` (`_PROVIDER_DEFAULT_MODELS`).
- `tools` — optional list of tool names from the [Tools](#tools) section below; resolved against `TOOL_REGISTRY` at agent build time.
- `thinking_effort` — `none` | `minimal` | `low` | `medium` | `high` | `xhigh` | `max`. Maps to provider-specific reasoning parameters. Runtime-tunable via the `/thinking-effort` slash command. See [`docs/authoring-agents.md`](./docs/authoring-agents.md) for the per-provider mapping.

For a deeper walkthrough of the agent file format (every field, channel-subscription mechanics, debugging tips), see [`docs/authoring-agents.md`](./docs/authoring-agents.md).

## Tools

Calfcord ships 11 builtin tools out of the box. Declare them in an agent's `tools:` frontmatter list to enable. The tool's body runs in the `calfkit-tools` process — see the [Security model](#security-model) for what that implies.

| Name | What it does |
|---|---|
| `private_chat` | One-on-one A2A conversation with another agent over Kafka. Audit log in Discord. |
| `shell` | Run a shell command on the `calfkit-tools` host. Persistent session via tmux when available. |
| `read_file` | View a file's contents with `cat -n` style line numbers; optional `view_range`. |
| `write_file` | Create or overwrite a file at a workspace-relative or absolute path. |
| `edit_file` | Exact-string replace with optional `replace_all`. Single-match required by default. |
| `grep` | Search file contents by regex; ripgrep-backed when present, Python fallback otherwise. |
| `glob` | Find files by name pattern (`**/*.py`, `src/**/*.ts`, etc.). |
| `web_fetch` | Fetch a URL and return Markdown-converted page content. |
| `web_search` | DuckDuckGo search (no API key required). |
| `todo_view` | Show the calling agent's task list. |
| `todo_write` | Replace the calling agent's task list. |

Tools are auto-discovered from `src/calfkit_organization/tools/builtin/` at boot — drop a `.py` file there with a `*_tool: ToolNodeDef = agent_tool(...)` at module bottom and the next `calfkit-tools` restart picks it up. Full authoring guide: [`docs/authoring-tools.md`](./docs/authoring-tools.md).

Tools are location-transparent over Kafka. By default every tool runs in the same `calfkit-tools` container, but `calfcord-package-tools` builds a slim image hosting only a subset:

```bash
uv run calfcord-package-tools shell grep --tag calfcord-shell:1.0
```

Deploy that image on a separate host (with `CALF_HOST_URL` pointing at the shared broker), and agents elsewhere call the tool the same way — the broker routes the RPC. To deploy the SAME tool on two different hosts (e.g. `edit_file` on both a workstation and an EU VM), use `--rename src=dst` so the second host's tool subscribes to a different Kafka topic and agents can call either by name. The parallel `calfcord-package-agents` builds an image that ships only the named agent definitions, useful for crash isolation per agent. Full walkthrough — including the multi-host rename pattern, broker auth/TLS for cross-network deployments, and `--dry-run` to inspect the generated Dockerfile — in [`docs/distributed-deployment.md`](./docs/distributed-deployment.md).

## Agent-to-agent communication

The `private_chat` tool lets one agent's LLM send a message to another agent and receive their reply. Kafka is the system of record; Discord is a human-readable audit log.

When agent A calls `private_chat(target_agent_id="bob", content="…")`:

1. The tool resolves the unified `a2a-audit` Discord channel via `A2AChannelResolver` (override its name with `CALFKIT_A2A_CHANNEL_NAME`).
2. For a fresh conversation: the tool posts A's request as A's persona, then anchors a new Discord **thread** on that message (the thread name encodes the caller→target pair and a topic snippet). For a follow-up: A passes the prior `thread_id` and the tool posts into the existing thread plus fetches recent thread history for context.
3. The tool invokes `agent.bob.in` via calfkit RPC, with a 60-second default timeout.
4. On reply, the tool posts B's response as B's persona into the same thread.
5. The tool returns the response text to A's LLM, prefixed with a `<thread_id>…</thread_id>` tag so A can continue the same thread on a later call.

See [`docs/a2a-threads.md`](./docs/a2a-threads.md) for the full thread-projection design.

Correlation is handled natively by calfkit. Timeouts return as LLM-readable error strings so the calling LLM can adapt; infrastructure failures raise `RuntimeError` with caller/target/correlation context.

The bridge injects a `temp_instructions` block listing available peers whenever it invokes an agent that has `private_chat` in its tools — so the LLM knows who it can call without trial-and-error.

## Configuration

Copy `.env.example` to `.env` and fill in:

```
DISCORD_BOT_TOKEN=...            # required, all deployments
DISCORD_APPLICATION_ID=...       # required, all deployments
DISCORD_GUILD_ID=...             # required for bridge + tools
DISCORD_OWNER_USER_ID=...        # optional; tags messages from the human owner
DISCORD_DEFAULT_CHANNEL_ID=...   # dev shortcut
ANTHROPIC_API_KEY=...            # on agent hosts only
OPENAI_API_KEY=...               # on agent hosts only
CALF_HOST_URL=localhost          # Kafka bootstrap
```

Per-agent runtime state lives in `state/agents/<name>.json` (channel subscriptions, atomically written). On first boot of an agent, channels are seeded from `CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` or `DISCORD_DEFAULT_CHANNEL_ID`.

A2A timeout override: `CALFKIT_TOOLS_TIMEOUT_SECONDS` (default 60s, applies to `private_chat` only — other builtin tools have no default per-call timeout at the calfkit layer).

A2A category override: `CALFKIT_A2A_CHANNEL_CATEGORY` (default unset). When set, the tools process places the unified `a2a-audit` channel under a Discord category with that name, creating the category lazily on first use. Edit the category's permission overwrites once in the Discord UI to lock down audit visibility — the channel (and all its threads) inherit those overwrites. The unified channel is reused regardless of its current category, so this is non-disruptive to enable on a running deployment. A2A channel name itself is overridable via `CALFKIT_A2A_CHANNEL_NAME` (default `a2a-audit`).

## Running

Three supported modes. All share the same `.env` and `agents/*.md` — switching between them needs no code changes.

### 1. Quick start (Docker Compose)

```bash
cp .env.example .env
# Open .env and fill in (at minimum):
#   - DISCORD_BOT_TOKEN
#   - DISCORD_APPLICATION_ID
#   - DISCORD_GUILD_ID
#   - ANTHROPIC_API_KEY or OPENAI_API_KEY
# docker compose hard-errors if .env is missing — the cp step above
# is mandatory.

docker compose up --build
```

That brings up five services: Redpanda (Kafka broker), bridge, agent, router, and tools. The `--build` flag rebuilds the image when the Dockerfile or dependencies change; omit it for fast plain restarts of the existing image. `Ctrl-C` to stop; `docker compose down` to remove containers; `docker compose down -v` to also wipe the broker's data volume — note that **`-v` destroys all Kafka topic history** including in-flight A2A audit data.

**Linux note**: if your user's UID is not 1000, build with `UID=$(id -u) GID=$(id -g) docker compose build` (then `docker compose up` to start) so files the tools container writes to bind-mounted host dirs stay owned by you. macOS Docker Desktop handles UID translation automatically.

### 2. Native (no Docker for calfcord)

```bash
uv sync                                              # install dependencies
docker compose up -d redpanda                        # or bring your own Kafka

# Add to .env so every uv-run terminal picks it up automatically:
echo 'CALF_HOST_URL=localhost:19092' >> .env

# Each in its own terminal (or process supervisor):
uv run calfkit-bridge
uv run calfkit-agent                                 # all agents on one Worker
# or for crash isolation per agent:
#   uv run calfkit-agent scribe
uv run calfkit-router
uv run calfkit-tools
```

The `localhost:19092` port is Redpanda's external listener (the published port in `docker-compose.yml`). Skip the `docker compose up -d redpanda` line entirely if you have Kafka elsewhere — just point `CALF_HOST_URL` at it. Writing the value to `.env` rather than `export`ing it means every terminal `uv run` opens picks it up via `python-dotenv` without needing a per-shell re-export.

### 3. Mixing modes

Anything in between works too: run the bridge in compose while you iterate on the agent locally, or the other way around. Each process reads `.env` independently, and a shared Kafka broker is the only wire-format contract between them. Native-side processes still need `CALF_HOST_URL=localhost:19092` in `.env` (see section 2); containerized services pick up `redpanda:9092` from compose's per-service environment block.

In Discord, `@scribe hello` invokes the scribe agent via the bridge. The agent's reply appears as a webhook message under the agent's persona. If the agent's LLM uses `private_chat`, the exchange shows up in a thread under the unified `a2a-audit` channel — one thread per conversation, the thread name encoding the caller→target pair.

### Security model

The default Docker Compose layout **bind-mounts the entire project root** into the `tools` container at `/workspace`, read-write. Agents with shell/filesystem tools can therefore read or edit any file in the checkout — `agents/*.md`, `src/`, `state/`, all of it. This is the "trusted shared workspace" model: there is no per-agent sandbox, and all agents on a deployment see the same filesystem.

Implications:

- **Do not expose calfcord to untrusted users** in this default configuration. Anyone who can `@mention` an agent gets the agent's tool surface.
- **Lock down which agents have which tools** in `agents/*.md`. An agent that only needs `private_chat` should not declare `shell`.
- **To narrow the mount** (e.g. only a scratch dir), drop a `compose.override.yml` with a tighter `volumes:` block for the `tools` service.
- **To widen it** (e.g. mount `$HOME` to let agents touch other projects on the same machine), same mechanism with a wider mount.
- **To skip Docker for tools entirely**, run `calfkit-tools` natively via `uv run` while keeping bridge and agent in compose. Tools then have the full permissions of the user running the process.

## Project layout

```
src/calfkit_organization/
├── agents/        # definition, factory, runner, state, gates, routing,
│                  # peer_roster, phonebook, thinking, identifier,
│                  # loader, md_writer
├── bridge/        # gateway, ingress, outbox, egress, normalizer,
│                  # registry, history, slash, synthesized, wire,
│                  # pending_wires
├── discord/       # client wrappers (sender, persona, receiver,
│                  # settings, messages, retry_feedback)
├── router/        # ambient-channel routing agent (definition, runner,
│                  # roster, fanout, prompt)
└── tools/
    ├── builtin/   # shipped tools — fs, search, shell, web, todos,
    │              # private_chat, plus _observation / workspace helpers
    ├── discovery.py  # auto-discovery loader (walks builtin/ at import)
    └── runner.py     # calfkit-tools entry point

agents/                 # agent .md definitions (live)
state/agents/           # per-agent runtime state (channel subscriptions)
docs/                   # authoring guides + security model + design archive
.github/                # CI/CD workflows + Dependabot + issue/PR templates
Dockerfile, docker-compose.yml  # deployment
tests/                  # pytest suite
```

## Development

- Python 3.12+.
- Dependencies managed with `uv`. Use `uv add <pkg>` rather than editing `pyproject.toml` directly.
- Conventional commit prefixes on commits to `main` (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- Run the suite: `uv run pytest`.
- Authoring a new agent: see [`docs/authoring-agents.md`](./docs/authoring-agents.md).
- Authoring a new tool: see [`docs/authoring-tools.md`](./docs/authoring-tools.md).
- Full contribution guide: [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Documentation index

- [`docs/authoring-agents.md`](./docs/authoring-agents.md) — adding and configuring agents.
- [`docs/authoring-tools.md`](./docs/authoring-tools.md) — adding a builtin tool.
- [`docs/security.md`](./docs/security.md) — deployment patterns and threat model.
- [`docs/distributed-deployment.md`](./docs/distributed-deployment.md) — splitting tools and agents across hosts via `calfcord-package-tools` / `calfcord-package-agents`.
- [`docs/a2a-threads.md`](./docs/a2a-threads.md) — agent-to-agent threading via `private_chat`.
- [`docs/ambient-routing.md`](./docs/ambient-routing.md) — the router process.
- [`docs/design/`](./docs/design/) — historical design notes.

## Project governance

- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — setup, commit conventions, PR expectations.
- [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) — Contributor Covenant v2.1.
- [`SECURITY.md`](./SECURITY.md) — vulnerability reporting.

## License

Apache-2.0. See [`LICENSE`](./LICENSE).
