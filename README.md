# calfcord

A multi-agent organization that lives on Discord. Each agent is an LLM-backed [calfkit](https://github.com/calf-ai/calfkit-sdk) node with its own Discord persona; agents collaborate with one another over Kafka, and humans interact with them through normal Discord channels and slash commands.

## Architecture

Three independent processes, each safe to deploy on its own host:

- **`calfkit-bridge`** — the single Discord gateway. Loads the agent registry from `agents/*.md`, normalizes inbound Discord events to a wire format, publishes them to per-channel Kafka topics, and posts agent replies back to Discord as persona webhooks.
- **`calfkit-agent`** — runs one or all agents as calfkit `Agent` nodes. Each agent subscribes to its configured channel topics plus a private `agent.{agent_id}.in` inbox used for direct agent-to-agent (A2A) calls.
- **`calfkit-tools`** — runs the A2A `private_chat` tool. Intentionally decoupled from the bridge (see below).

All three communicate exclusively through Kafka. The only Discord-touching processes are the bridge (gateway + outbox) and the tools runner (projection of A2A exchanges to a per-pair audit channel).

## Decoupled deployment

The three processes have intentionally different access requirements:

| Resource                              | Bridge | Agent           | Tools |
|---------------------------------------|:------:|:---------------:|:-----:|
| `agents/*.md` (local files)           |   yes  | yes (own only)  |  no   |
| Discord bot token (env var)           |   yes  | yes             |  yes  |
| Kafka broker                          |   yes  | yes             |  yes  |
| LLM provider API key                  |   —    | yes             |  —    |

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
- `model` — provider-specific model id. Defaults: `claude-sonnet-4-5` / `gpt-5-mini`.
- `tools` — optional list of tool names. Resolved against `calfkit_organization.tools.TOOL_REGISTRY` at agent build time. Currently `private_chat` is the only registered tool.
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

## Running locally

```bash
uv sync                               # install dependencies
docker compose up -d                  # or any Kafka broker reachable at CALF_HOST_URL

# Each in its own terminal (or process supervisor):
uv run calfkit-bridge
uv run calfkit-agent                  # all agents on one Worker
# or for crash isolation per agent:
#   uv run calfkit-agent scribe
uv run calfkit-tools
```

In Discord, `@scribe hello` invokes the scribe agent via the bridge. The agent's reply appears as a webhook message under the agent's persona. If the agent's LLM uses `private_chat`, the exchange shows up in the `a2a-<a>-<b>` channel between the two personas.

## Project layout

```
src/calfkit_organization/
├── agents/        # definition, factory, runner, state, peer_roster, phonebook
├── bridge/        # gateway, ingress, outbox, egress (A2A channel resolver), normalizer, registry
├── discord/       # client wrappers (sender, persona, receiver, settings)
└── tools/         # TOOL_REGISTRY, private_chat, calfkit-tools runner

agents/            # agent .md definitions (live)
state/agents/      # per-agent runtime state
tests/             # pytest suite
```

## Development

- Python 3.11+.
- Dependencies managed with `uv`. Use `uv add <pkg>` rather than editing `pyproject.toml` directly.
- Conventional commit prefixes on commits to `main` (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- Run the suite: `uv run pytest`.

## License

See `LICENSE`.
