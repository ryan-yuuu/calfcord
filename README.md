# calfcord

A multi-agent organization that lives on Discord. Each agent is an LLM-backed [calfkit](https://github.com/calf-ai/calfkit-sdk) node with its own Discord persona; agents collaborate with one another over Kafka, and humans interact with them through normal Discord channels and slash commands.

## Architecture

Four independent processes, each safe to deploy on its own host:

- **`calfkit-bridge`** — the single Discord gateway. Loads the agent registry from `agents/*.md`, normalizes inbound Discord events to a wire format, publishes them to per-channel Kafka topics, and posts agent replies back to Discord as persona webhooks.
- **`calfkit-agent`** — runs one or all agents as calfkit `Agent` nodes. Each agent subscribes to its configured channel topics plus a private `agent.{agent_id}.in` inbox used for direct agent-to-agent (A2A) calls.
- **`calfkit-router`** — the ambient-channel router. Decides which agent (if any) should handle a non-@-mentioned message in a watched channel. See `docs/ambient-routing.md`.
- **`calfkit-tools`** — runs the A2A `private_chat` tool plus the builtin filesystem / shell / search / web / todo tools. Intentionally decoupled from the bridge (see below).

All four communicate exclusively through Kafka. The only Discord-touching processes are the bridge (gateway + outbox) and the tools runner (projection of A2A exchanges to a per-pair audit channel).

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
- `tools` — optional list of tool names. Resolved against `calfkit_organization.tools.TOOL_REGISTRY` at agent build time. See that module for the live list of registered tools.
- `thinking_effort` — `none` | `low` | `medium` | `high` | `xhigh` | `max`. Maps to provider-specific reasoning parameters. Runtime-tunable via the `/thinking-effort` slash command.

## Agent-to-agent communication

The `private_chat` tool lets one agent's LLM send a message to another agent and receive their reply. Kafka is the system of record; Discord is a human-readable audit log.

When agent A calls `private_chat(target_agent_id="bob", content="…")`:

1. The tool resolves (or creates) the pair's `a2a-alice-bob` Discord channel via `A2AChannelResolver`.
2. The tool posts the request as A's persona in that channel (best-effort, retried once on transient Discord errors).
3. The tool invokes `agent.bob.in` via calfkit RPC, with a 60-second default timeout.
4. On reply, the tool posts B's response as B's persona in the same audit channel.
5. The tool returns the response text to A's LLM.

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

Tools timeout override: `CALFKIT_TOOLS_TIMEOUT_SECONDS` (default 60).

A2A category override: `CALFKIT_A2A_CHANNEL_CATEGORY` (default unset). When set, the tools process places every newly-created `a2a-<x>-<y>` audit channel under a Discord category with that name, creating the category lazily on first use. Edit the category's permission overwrites once in the Discord UI to lock down audit visibility — child channels inherit those overwrites. Existing channels with the canonical name are reused regardless of their current category, so this is non-disruptive to enable on a running deployment.

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

In Discord, `@scribe hello` invokes the scribe agent via the bridge. The agent's reply appears as a webhook message under the agent's persona. If the agent's LLM uses `private_chat`, the exchange shows up in the `a2a-<a>-<b>` channel between the two personas.

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
├── agents/        # definition, factory, runner, state, peer_roster, phonebook
├── bridge/        # gateway, ingress, outbox, egress (A2A channel resolver), normalizer, registry
├── discord/       # client wrappers (sender, persona, receiver, settings)
└── tools/
    ├── builtin/   # shipped tools (shell, fs, search, web, todos, private_chat)
    ├── discovery.py  # auto-discovery loader (walks builtin/ at import time)
    └── runner.py     # calfkit-tools entry point

agents/            # agent .md definitions (live)
state/agents/      # per-agent runtime state
tests/             # pytest suite
```

## Development

- Python 3.12+.
- Dependencies managed with `uv`. Use `uv add <pkg>` rather than editing `pyproject.toml` directly.
- Conventional commit prefixes on commits to `main` (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- Run the suite: `uv run pytest`.
- Authoring a new tool: see `docs/authoring-tools.md`.

## License

See `LICENSE`.
