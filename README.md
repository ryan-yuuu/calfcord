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

You'll need a Discord server you own. [Docker](https://docs.docker.com/get-docker/)
is optional — only for running a Kafka broker locally (step 5).

**1. Set up the Discord app** (~5 min, one time) — follow
[`docs/discord-setup.md`](./docs/discord-setup.md). It gives you the bot token
and application ID you'll enter in step 4.

**2. Install.** One line, no Python/Docker/git needed first:

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/calfcord/main/scripts/install.sh | bash
```

When it finishes, **restart your shell** (or open a new terminal) so the
`calfcord` command is on your `PATH`.

**3. Configure.** Run the guided setup — it sets up your first agent *and* the
install's `.env`. It starts with the agent: a name (default `assistant`), a
description, a model provider (Anthropic / OpenAI / Codex subscription) and its
API key, a model **picked from a live list fetched from the provider** (you
select one — you can't mistype an invalid slug), and its tools (every built-in,
**all selected by default** — deselect any you don't want). Then it asks for
your Discord bot token and application ID, and finally a Kafka broker. It writes
`~/.calfcord/config/.env` plus `~/.calfcord/agents/<name>.md`. Pick **Codex** and
it logs you in inline via a device code — a URL + one-time code you open on any
device, so it works the same locally or over SSH:

```bash
calfcord init
```

**4. Start a broker.** Calfcord's processes talk over Kafka. The easy local
option is one Redpanda container (`calfcord init` selects
`CALF_HOST_URL=localhost:19092` and prints this command):

```bash
docker run -d --name calfcord-redpanda -p 19092:19092 \
  docker.redpanda.com/redpandadata/redpanda:latest \
  redpanda start --mode dev-container --smp 1 \
  --kafka-addr internal://0.0.0.0:9092,external://0.0.0.0:19092 \
  --advertise-kafka-addr internal://localhost:9092,external://localhost:19092
```

Already have a broker? Pick "I have a broker URL" in `calfcord init`, or run
`calfcord self set-broker <host:port>`.

**5. Run the four processes** (each in its own terminal, or under a supervisor):

```bash
calfcord calfkit-bridge     # the Discord gateway
calfcord calfkit-agent      # runs your agents
calfcord calfkit-router     # routes un-mentioned messages
calfcord calfkit-tools      # tools + the agent-to-agent channel
```

**6. Say hello.** In any channel the bot can see:

```
@assistant hello
```

A reply appears from your starter agent. You're live. 🎉

> **Next steps**
> - **Customize your agent / add tools** → `calfcord agent tools`, then restart
>   `calfcord calfkit-agent`. Field reference:
>   [`docs/authoring-agents.md`](./docs/authoring-agents.md).
> - **Enable ambient routing (optional)** → `@mentions` work without it;
>   un-mentioned messages just go unanswered. To have an agent answer those too,
>   run `calfcord router setup` and start `calfcord calfkit-router`. Details:
>   [`docs/ambient-routing.md`](./docs/ambient-routing.md).
> - **Run agents across machines** → install calfcord on each host and point
>   them all at one shared broker URL —
>   [`docs/distributed-deployment.md`](./docs/distributed-deployment.md).
> - **Developing calfcord?** Don't use the installer — clone the repo and use
>   the `uv` / `docker compose` workflow:
>   [`CONTRIBUTING.md`](./CONTRIBUTING.md) and
>   [running modes](./docs/architecture.md#running-modes).

## Define your own agent

The installer seeds a provider-agnostic starter, `assistant`, in
`~/.calfcord/agents/` — text-only **until** `calfcord init` configures it (init
bakes in the provider, model, and tools you picked, replacing the pristine
seed). Your agents live there and survive `calfcord self update`. This section
is the guide for adding *more* agents by hand.

An agent is one Markdown file. Drop a new one into `~/.calfcord/agents/`:

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
`/<name>`. Drop the file in, restart `calfcord calfkit-bridge` and
`calfcord calfkit-agent`, and it's live.

Prefer not to hand-write the file? The full lifecycle is on the CLI:
`calfcord agent create | list | show | edit | set | rename | delete` (plus
`calfcord agent tools` for just the tool list). `create` is a guided wizard,
`edit` an interactive field menu, and `set` its scriptable equivalent; restart
`calfcord calfkit-agent` after any change to apply it.

Full field reference (providers, models, tool scoping, thinking effort) and the
`calfcord agent` CLI →
[`docs/authoring-agents.md`](./docs/authoring-agents.md).

## How it works

Calfcord has **four independent process types**:

- **`calfkit-bridge`** — the Discord gateway.
- **`calfkit-agent`** — runs the agent(s).
- **`calfkit-router`** — decides who answers un-mentioned ambient messages.
- **`calfkit-tools`** — runs the tool(s).

Any process can run anywhere.

## Configuration

`calfcord init` (from the [quick start](#quick-start)) writes
`~/.calfcord/config/.env` with the essentials. To edit settings later, re-run
`calfcord init` or open that file directly;
[`docs/configuration.md`](./docs/configuration.md) is the complete
environment-variable reference.

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
- [`docs/installation.md`](./docs/installation.md) — install, configure (`calfcord init`), and run calfcord; the `calfcord` CLI, updates/rollback.
- [`docs/design/`](./docs/design/) — historical design notes.

## Contributing

Python 3.12+, dependencies managed with [`uv`](https://docs.astral.sh/uv/)
(`uv sync`, then `uv run pytest`). See [`CONTRIBUTING.md`](./CONTRIBUTING.md),
[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md), and
[`SECURITY.md`](./SECURITY.md).

## License

[Apache-2.0](./LICENSE).
