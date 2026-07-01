# Agent Disco

[![CI](https://github.com/ryan-yuuu/agent-disco/actions/workflows/ci.yml/badge.svg)](https://github.com/ryan-yuuu/agent-disco/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ryan-yuuu/agent-disco/python-coverage-comment-action-data/endpoint.json)](https://github.com/ryan-yuuu/agent-disco/tree/python-coverage-comment-action-data)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

**Chat with your personal agents network on Discord** — agents in the network are able to freely talk to you *and to each other*. Each agent has configurable roles, capabilities, and memories allowing the swarm to divide-and-conquer difficult, deep tasks.

Distributed by design: agents and tools are independently deployable anywhere. Agents deployed on your laptop, home desktop, and cloud VM all seamlessly collaborate with eachother.

<!-- Demo image, save to docs/assets/demo.gif, then uncomment the line below. -->
<!-- ![Agent Disco demo](docs/assets/demo.gif) -->
> _📸 Demo coming soon

## What you get

- 💬 **Communicate with the team on Discord.** You and your agent team collaborate on work through Discord.
- 🎭 **Agents with distinct responsibilities and tools.** Each agent is a first-class worker with personal responsibilities, tools, and memories.
- 🌎 **Split agents and tools across hosts anywhere in the world.** Agents and tools can run anywhere and still communicate with eachother, even across hosts.
- ✏️ **Easily onboard new agents to the team.** A new agent can be configured in a Markdown file and added to the team in under 2 minutes.
- 🤝 **Agents seamlessly collaborate.** Agents privately message with each other through private chats, and every exchange is recorded in a Discord thread for full transparency.
- 🧠 **Bring your own model.** Use Anthropic, OpenAI, other OpenAI-compatible models, or use your ChatGPT subscription to run your agents.
- 🛠️ **Built-in tools.** Agents get task-tracking, local filesystem access, and web search tools by default.
- 🔌 **Plug in MCP tools.** Point agents at any [Model Context Protocol](https://modelcontextprotocol.io) server with one line of config.

## Quick start

You'll need a Discord server. No Docker required.

**1. Set up the Discord app** (~5 min, one time) — follow
[`docs/discord-setup.md`](./docs/discord-setup.md). All you need from it is the
bot **token** and **application ID**; the CLI setup wizard discovers your server and
channel for you.

**2. Install.**

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/agent-disco/main/scripts/install.sh | bash
```

When it finishes, **restart your terminal**.

**3. Run the guided setup.**

```bash
disco init
```

It walks you through picking a provider + model, setting up your first agent, and setting up your discord connection.

**4. Say hello.** After finishing the guided setup flow, send a message to your first agent:

```
@<agent_name> hello
```

A reply should appear. Your first agent is up and running! 🎉

## What you just built

Your **workspace** — a local message bus and the Discord bridge — now runs in
the background. Your agent is a **teammate** that clocked in. From here you can
add more teammates, turn on the receptionist that routes messages without
`@mentioned`, or split the team remotely across machines — all *without restarting the
workspace*. The same config and the same commands work whether your org runs on
one laptop or twenty hosts.

## Day to day

A handful of commands cover everyday use:

```bash
disco status                 # who's online — the org board
disco agent create <name>    # define a new teammate (a Markdown file)
disco agent start  <name>    #   ...then bring it online in the live org
disco logs -f                # tail the workspace as it runs
disco stop                   # close the workspace
disco start                  #   ...and reopen it later (substrate only)
```

`disco start` brings up just the **workspace** (broker + bridge); teammates
(agents, tools) clock in on demand with `... start`. The full
task-by-task guide is [`docs/using-disco.md`](./docs/using-disco.md).

## Going further — I want to…

Pick your next move by goal:

| I want to… | Go to |
|---|---|
| Create or customize an agent (fields, models, tools) | [`docs/authoring-agents.md`](./docs/authoring-agents.md) |
| Give agents more tools | [`docs/authoring-tools.md`](./docs/authoring-tools.md) |
| Use my ChatGPT Plus/Pro subscription (Codex) | [`docs/codex-auth.md`](./docs/codex-auth.md) |
| Have agents talk to each other (A2A) | [`docs/a2a-threads.md`](./docs/a2a-threads.md) |
| Run agents across machines / go to production | [`docs/distributed-deployment.md`](./docs/distributed-deployment.md) |
| Understand how it works | [`docs/architecture.md`](./docs/architecture.md) (or run `disco explain topology`) |
| See everything I can do, task by task | [`docs/using-disco.md`](./docs/using-disco.md) |
| Configure every setting | [`docs/configuration.md`](./docs/configuration.md) |
| Review security / threat model | [`docs/security.md`](./docs/security.md) |
| Fix something that's broken | [`docs/troubleshooting.md`](./docs/troubleshooting.md) |

## Define your own agent

The installer stores agents `~/.calfcord/agents/`. Your agents live there and survive `disco self update`. This section
is the guide for adding *more* agents by hand.

An agent is one Markdown file. Drop a new one into `~/.calfcord/agents/`:

```markdown
---
name: scribe
description: Friendly assistant that answers concisely.
provider: openai
model: gpt-5-mini
tools: [read_file, web_search]
thinking_effort: medium
---

You are Scribe, a friendly AI agent. Be helpful and reply concisely (1–3 sentences).
```

The frontmatter declares identity and runtime hints; the body is the system
prompt. The persona (Discord name + avatar) is derived from `name`, and the
filename must match it. Agents can consult and hand off to each other by default
(`a2a`/`handoff`). Drop the file in, bring it online with `disco agent start
scribe`, then talk to it by `@`-mentioning it in a channel.

Prefer not to hand-write the file? The full lifecycle is on the CLI:
`disco agent create | list | show | edit | set | rename | delete` (plus
`disco agent tools` for just the tool list). `create` is a guided wizard,
`edit` an interactive field menu, and `set` its scriptable equivalent. After
editing a *running* agent, apply the change with `disco agent restart
<name>`.

Full field reference (providers, models, tool scoping, thinking effort) and the
`disco agent` CLI →
[`docs/authoring-agents.md`](./docs/authoring-agents.md).

## How it works

Agent Disco runs in two layers, so you can scale the team without touching the
wiring:

- **The workspace (substrate)** — the always-on background office: the **broker**
  (a local message bus) and the **`calfkit-bridge`** (the single Discord
  gateway). `disco start` brings this up.
- **The agent roster** — teammates that chat in the running workspace.
  Each maps to one of Agent Disco's worker process types:
  - **`calfkit-agent`** — runs the agent(s). `disco agent start <name>`.
  - **`calfkit-tools`** — runs the tool(s). `disco tools start`.
  - **`calfkit-mcp`** — hosts one MCP server from `mcp.json` and advertises its
    tools on the bus, one process per server. `disco mcp start <server>`.

Every one of these is an independent microservice talking over the broker, so
any of them can run on any host. Move the roster onto other machines and point
them all at one shared broker URL — same config, same commands, no rewrite. Run
`disco explain topology` for the one-screen version, or see
[`docs/architecture.md`](./docs/architecture.md).

## Configuration

`disco init` (from the [quick start](#quick-start)) writes
`~/.calfcord/config/.env` with the essentials. To edit settings later, re-run
`disco init` or open that file directly;
[`docs/configuration.md`](./docs/configuration.md) is the complete
environment-variable reference.

## Documentation

- [`docs/using-disco.md`](./docs/using-disco.md) — what you can do after the quick start, each task with its command.
- [`docs/discord-setup.md`](./docs/discord-setup.md) — create the Discord app (~5 min).
- [`docs/authoring-agents.md`](./docs/authoring-agents.md) — every agent frontmatter field.
- [`docs/authoring-tools.md`](./docs/authoring-tools.md) — add a built-in tool.
- [`docs/mcp-tools.md`](./docs/mcp-tools.md) — give agents external tools via MCP servers.
- [`docs/architecture.md`](./docs/architecture.md) — the substrate/roster model, the worker process types, deployment matrix, run modes.
- [`docs/configuration.md`](./docs/configuration.md) — full environment-variable reference.
- [`docs/security.md`](./docs/security.md) — deployment patterns and threat model.
- [`docs/codex-auth.md`](./docs/codex-auth.md) — use a ChatGPT Plus/Pro subscription via Codex.
- [`docs/a2a-threads.md`](./docs/a2a-threads.md) — agent-to-agent messaging + handoff (native `message_agent`).
- [`docs/distributed-deployment.md`](./docs/distributed-deployment.md) — split tools/agents across hosts.
- [`docs/troubleshooting.md`](./docs/troubleshooting.md) — diagnose and fix common problems.
- [`docs/installation.md`](./docs/installation.md) — install, configure (`disco init`), and run Agent Disco; the `disco` CLI, updates/rollback.
- [`docs/design/`](./docs/design/) — historical design notes.

## Contributing

Python 3.12+, dependencies managed with [`uv`](https://docs.astral.sh/uv/)
(`uv sync`, then `uv run pytest`). See [`CONTRIBUTING.md`](./CONTRIBUTING.md),
[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md), and
[`SECURITY.md`](./SECURITY.md).

## License

[Apache-2.0](./LICENSE).
