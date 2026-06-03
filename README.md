# 🐮 Calfcord

[![CI](https://github.com/ryan-yuuu/calfcord/actions/workflows/ci.yml/badge.svg)](https://github.com/ryan-yuuu/calfcord/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

**Collaborate with your team of AI agents on Discord** — each with its own responsibilities and memories, all able to talk to you *and to each other*.

Distributed by design: agents and tools are independently deployable anywhere. Have agents deployed on your personal laptop, work laptop, and cloud VM all seamlessly collaborate with eachother.

<!-- Demo image, save to docs/assets/demo.gif, then uncomment the line below. -->
<!-- ![Calfcord demo](docs/assets/demo.gif) -->
> _📸 Demo coming soon

## What you get

- 💬 **Communicate with the team on Discord.** You and your agent team collaborate on work and provide updates through Discord.
- 🎭 **Agents with distinct job responsibilities and tools.** Each agent is a first-class worker with personal responsibilities, tools, and memories.
- 🌎 **Split agents and tools across hosts anywhere in the world.** Agents and tools within a team are microservices deployable anywhere, even across hosts.
- ✏️ **Easily onboard new agents to the team.** A new agent can be configured in a Markdown file, independently deployed, and added to the team in <2 minutes.
- 🤝 **Agents seamlessly collaborate.** Agents chat with each other through private chats, and every exchange is recorded in a Discord thread.
- 🧠 **Bring your own model.** Anthropic, OpenAI, other OpenAI-compatible APIs, or use your ChatGPT Plus/Pro subscription (via Codex) — set it per agent.
- 🛠️ **Built-in tools + MCP support.** Agents can get task-tracking, computer filesystem access, and web search tools by default. Plus, easily provide more tools to your team via MCP.

## Quick start

You'll need [Docker](https://docs.docker.com/get-docker/) and a Discord server
you own.

**1. Set up the Discord app** (~5 min, one time) — follow
[`docs/discord-setup.md`](./docs/discord-setup.md). It gives you the `DISCORD_*`
values below.

**2. Configure.** Copy the template and fill in the four essentials:

```bash
cp .env.example .env
```

```dotenv
DISCORD_BOT_TOKEN=...            # from the Discord setup
DISCORD_APPLICATION_ID=...       # from the Discord setup
DISCORD_GUILD_ID=...             # your server (for instant slash commands)
ANTHROPIC_API_KEY=...            # or OPENAI_API_KEY
```

**3. Add your first agent.** Calfcord ships only a Codex demo agent, so create
your own — drop a Markdown file at `agents/scribe.md`:

```markdown
---
name: scribe
display_name: Scribe
description: Friendly assistant that answers concisely.
provider: anthropic
tools: []
---

You are Scribe, a friendly AI agent. Reply concisely (1–3 sentences).
```

`tools: []` keeps this first agent text-only; [Define your own agent](#define-your-own-agent)
below shows how to add tools.

**4. Launch.**

```bash
docker compose up --build
```

This starts the four processes plus a Calfkit broker — five containers in total.

**5. Say hello.** In any channel the bot can see:

```
@scribe hello
```

A reply appears from the agent. You're live. 🎉

> Prefer running without Docker, or splitting processes across hosts? Use the
> [one-line native installer](./docs/installation.md), and see
> [running modes](./docs/architecture.md#running-modes).

## Define your own agent

An agent is one Markdown file in `agents/`:

```markdown
---
name: scribe
display_name: Scribe
description: Friendly assistant that answers concisely.
avatar_url: https://api.dicebear.com/9.x/glass/png?seed=scribe
provider: openai
model: gpt-5-mini
tools: [private_chat]
thinking_effort: medium
---

You are Scribe, a friendly AI agent. Be helpful and reply concisely (1–3 sentences).
```

The frontmatter declares identity and runtime hints; the body is the system
prompt. The filename must match `name`, and the slash command is always
`/<name>`. Drop the file in, restart `calfkit-bridge` and `calfkit-agent`, and
it's live.

Full field reference (providers, models, tool scoping, thinking effort) →
[`docs/authoring-agents.md`](./docs/authoring-agents.md).

## How it works

Calfcord has **four independent process types**:

- **`calfkit-bridge`** — the Discord gateway.
- **`calfkit-agent`** — runs the agent(s).
- **`calfkit-router`** — decides who answers un-mentioned ambient messages.
- **`calfkit-tools`** — runs the tool(s).

Any process can run anywhere.

## Configuration

`.env.example` is fully commented — the [quick start](#quick-start) covers the
four essentials, and [`docs/configuration.md`](./docs/configuration.md) is the
complete environment-variable reference.

## Documentation

- [`docs/discord-setup.md`](./docs/discord-setup.md) — create the Discord app (~5 min).
- [`docs/authoring-agents.md`](./docs/authoring-agents.md) — every agent frontmatter field.
- [`docs/authoring-tools.md`](./docs/authoring-tools.md) — add a built-in tool.
- [`docs/architecture.md`](./docs/architecture.md) — the four processes, deployment matrix, run modes.
- [`docs/configuration.md`](./docs/configuration.md) — full environment-variable reference.
- [`docs/security.md`](./docs/security.md) — deployment patterns and threat model.
- [`docs/a2a-threads.md`](./docs/a2a-threads.md) — agent-to-agent threading via `private_chat`.
- [`docs/ambient-routing.md`](./docs/ambient-routing.md) — the router process.
- [`docs/distributed-deployment.md`](./docs/distributed-deployment.md) — split tools/agents across hosts.
- [`docs/installation.md`](./docs/installation.md) — install & run calfcord natively (no Docker), the `calfcord` CLI, updates/rollback.
- [`docs/design/`](./docs/design/) — historical design notes.

## Contributing

Python 3.12+, dependencies managed with [`uv`](https://docs.astral.sh/uv/)
(`uv sync`, then `uv run pytest`). See [`CONTRIBUTING.md`](./CONTRIBUTING.md),
[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md), and
[`SECURITY.md`](./SECURITY.md).

## License

[Apache-2.0](./LICENSE).
