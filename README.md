# 🐮 Calfcord

[![CI](https://github.com/ryan-yuuu/calfcord/actions/workflows/ci.yml/badge.svg)](https://github.com/ryan-yuuu/calfcord/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

**Spin up a team of AI agents that live in your Discord** — each with its own
persona, each defined in a single Markdown file, all able to talk to you *and to
each other*.

<!-- TODO: replace with a real capture. Best shot: a channel showing
     `@scribe hello` and the reply rendered under the agent's persona name +
     avatar. Save under docs/assets/ and swap the line below in. -->
<!-- ![Calfcord demo](docs/assets/demo.gif) -->
> _📸 Demo coming soon — a Discord channel where you `@mention` an agent and it
> replies under its own persona._

## What you get

- 🎭 **Agents as Discord personas.** Each agent replies under its own display
  name and avatar via webhooks — not a single shared bot voice.
- 📝 **One file per agent.** Frontmatter is the agent's identity; the body is its
  system prompt. Drop the file in `agents/`, and `/<name>` works in Discord.
- 🤝 **Agents collaborate.** They can call each other with `private_chat`, and
  every exchange is audited in a Discord thread you can read.
- 🧠 **Bring your own model.** Anthropic, OpenAI, or a ChatGPT Plus/Pro
  subscription (via Codex) — set it per agent.
- 🛠️ **Built-in tools.** Shell, files, web search/fetch, todos, and more —
  opt-in per agent, so you control each one's reach.

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

**3. Launch.**

```bash
docker compose up --build
```

This starts five services: a Kafka broker (Redpanda), the bridge, an agent
worker, the router, and the tools runner.

**4. Say hello.** In any channel the bot can see:

```
@scribe hello
```

A reply appears **under the agent's own persona**. You're live. 🎉

> Prefer running without Docker, or splitting processes across hosts? See
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
model: gpt-5-nano
tools: [private_chat]
thinking_effort: medium
---

You are Scribe, a friendly AI agent. Be helpful and reply concisely (1–3 sentences).
```

The frontmatter declares identity and runtime hints; the body is the LLM's
system prompt. The filename must match `name`, and the Discord slash command is
always `/<name>`. Drop the file in, restart the agent, and it's live.

Full field reference (providers, models, tool scoping, thinking effort) →
[`docs/authoring-agents.md`](./docs/authoring-agents.md).

## How it works

Calfcord is **four independent processes** wired together over Kafka:
`calfkit-bridge` (the Discord gateway), `calfkit-agent` (runs the agents),
`calfkit-router` (decides who answers un-mentioned messages), and
`calfkit-tools` (runs the tools and the agent-to-agent channel). Kafka is the
only contract between them, so any process can run anywhere.

```mermaid
flowchart LR
    Discord(("Discord")) <--> Bridge[calfkit-bridge]
    Bridge <--> Kafka{{Kafka}}
    Kafka <--> Agents[calfkit-agent]
    Kafka <--> Router[calfkit-router]
    Kafka <--> Tools[calfkit-tools]
    Tools <--> Discord
```

Full process model, the decoupled-deployment access matrix, and project layout →
[`docs/architecture.md`](./docs/architecture.md).

## Configuration

`cp .env.example .env` and fill in the four values shown in the
[quick start](#quick-start). `.env.example` is fully commented; the complete
environment-variable reference lives in
[`docs/configuration.md`](./docs/configuration.md).

## ⚠️ Security

By default, Docker Compose **bind-mounts the project root** into the tools
container read-write. Any agent with `shell` / file tools can therefore read or
edit anything in the checkout. This is the intended "trusted shared workspace"
model — so **don't expose Calfcord to untrusted users**, and only grant each
agent the tools it actually needs. Details and hardening →
[`docs/security.md`](./docs/security.md).

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
- [`docs/design/`](./docs/design/) — historical design notes.

## Contributing

Python 3.12+, dependencies managed with [`uv`](https://docs.astral.sh/uv/)
(`uv sync`, then `uv run pytest`). See [`CONTRIBUTING.md`](./CONTRIBUTING.md),
[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md), and
[`SECURITY.md`](./SECURITY.md).

## License

[Apache-2.0](./LICENSE).
