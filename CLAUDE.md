# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Calfcord is a multi-agent "organization" that lives on Discord: a team of AI agents, each with its own
responsibilities, tools, and memory, that talk to humans and to each other. It is built on the **calfkit** SDK
(a Kafka-backed event-driven microservices framework). The defining architectural property is that everything is
**distributed and independently deployable** — agents and tools are microservices that can run on different
hosts and still collaborate over a shared broker.

## Commands

Python 3.12+, dependencies managed with **`uv`**. 
- Do not hand-edit `[project.dependencies]` in `pyproject.toml` —
  use `uv add <pkg>` so `uv.lock` stays canonical.
- Use `uv run` to execute project files and tests.

Native / mixed mode (same `.env` and `agents/*.md` — no code changes to switch):

```bash
docker compose up -d redpanda                 # or bring your own Kafka
echo 'CALF_HOST_URL=localhost:19092' >> .env  # so every `uv run` terminal finds the broker
uv run calfkit-bridge      # Discord gateway
uv run calfkit-agent       # all agents on one Worker — or `uv run calfkit-agent <name>` for one
uv run calfkit-router      # ambient-message router using LLM
uv run calfkit-tools       # deploy all tools on one Worker
```

When iterating on a single agent, run that agent natively and leave the other three processes in compose —
the wire protocol is Kafka, so split mode just works.

### Console-script entry points (`pyproject.toml` → `[project.scripts]`)

- `calfkit-bridge` / `calfkit-agent` / `calfkit-router` / `calfkit-tools` / `calfkit-mcp` — the process runners.
- `calfkit-auth` — Codex (ChatGPT subscription) OAuth login.
- `calfcord-package-tools` / `calfcord-package-agents` — build slim per-tool / per-agent deployment images.
- `calfcord-mcp-codegen` — generate an MCP tool-schema module into `src/calfcord/mcp/schemas/` (see MCP below).

## Architecture

Calfcord is **four independent process types that communicate through Kafka**. Each is safe to deploy
on its own host; switching deployment styles needs no code changes. `docs/architecture.md` is the authoritative
source.

- **`calfkit-bridge`** (`src/calfcord/bridge/`) — the single Discord gateway and the source of truth for "what
  agents exist." Loads the agents registry from runtime pings from each agent, normalizes inbound Discord events to a wire format,
  publishes to per-channel Kafka topics, and posts agent replies back as persona webhooks. 
- **`calfkit-agent`** (`src/calfcord/agents/`) — runs one or more agents as calfkit `Agent` nodes. Each agent
  subscribes to its channel topics plus a private `agent.{agent_id}.in` inbox used for agent-to-agent (A2A) RPC.
- **`calfkit-router`** (`src/calfcord/router/`) — decides which agent (if any) should answer a non-`@`-mentioned
  ambient message in a watched channel. See `docs/ambient-routing.md`.
- **`calfkit-tools`** (`src/calfcord/tools/`) — runs the `private_chat` A2A tool plus the built-in fs / shell /
  search / web / todos tools.

```
Discord ⇄ bridge ⇄ Kafka ⇄ { agents, router, tools } ; tools ⇄ Discord (A2A thread projection)
```

### Decoupling invariants (do not violate)

- **The tools process is registry-free by design.** It has no read access to `agents/*.md`. Agent identities
  reach tools via a `phonebook` field the bridge places in every invocation's `deps`, which calfkit propagates
  agent → tool. Keep it this way so tools can run on a host with no shared filesystem.
- **The agent deployment path imports only MCP *schemas*, never calls `calfcord.mcp.config.load_mcp_servers`**
  (which reads `mcp.json` — transport and `$VAR` secrets). MCP server config is bridge-only.
- Cross-process Kafka topic literals live in `src/calfcord/topics.py` and `src/calfcord/control_plane/topics.py`
  so producer and consumer can't drift. Per-agent / per-channel parameterized topics stay where they're consumed.
- The Discord bridge has no shared filesystem access with agents, so it has no access to `agents/*.md` files. `agents/*.md`
  files are coupled on the agent-side and information is relayed to the bridge via a ping on startup.

### Agents are Markdown files

An agent's configuration and responsibilities are in Markdown files. Frontmatter is identity + runtime hints (`name`, `display_name`, `provider`,
`model`, `tools:`, `thinking_effort`); the body is the LLM instructions prompt. The filename must match `name`. 
The bridge auto-discovers every file at boot. Full field reference: `docs/authoring-agents.md`.

### Tools auto-discover

A tool is an `async def name(ctx: ToolContext, **kwargs) -> str` decorated with `@agent_tool`, dropped into
`src/calfcord/tools/builtin/`. Discovery is automatic on the next `calfkit-tools` boot — no registry edits, no
entry points. Agents opt in by listing the tool name in their `.md` `tools:` array. Full reference:
`docs/authoring-tools.md`.

**Error-handling convention (hard rule, applies beyond tools):** LLM-recoverable problems (bad input, 404,
network failure) `return` an `"error: ..."` string the calling LLM can adapt to; genuine infrastructure bugs
`raise RuntimeError` with caller/target/correlation context. Prefer a loud raise over a swallowed exception or a
logged-and-continued warning.

### Other subsystems

- `src/calfcord/providers/codex/` — bring-your-own-model via a ChatGPT Plus/Pro subscription (Codex OAuth, JWT,
  prompt caching). Other providers (Anthropic, OpenAI, OpenAI-compatible) are selected per agent in frontmatter.
- `src/calfcord/mcp/` — MCP tool support. `calfcord-mcp-codegen <server>` is a convention wrapper over
  `calfkit mcp codegen` that owns the server name (validated) and output path so generated schemas always land in
  `src/calfcord/mcp/schemas/` where `discovery.discover_mcp_catalog` will find them.
- `src/calfcord/packaging/` — builds slim per-tool / per-agent images for splitting across hosts
  (`docs/distributed-deployment.md`).

### Known calfkit lifecycle limitation

The bridge and `calfkit-agent` deliberately do **not** use calfkit's managed `Worker.run()` — they hand-roll
start/serve/drain because that path can't yet emit lifecycle events at precise points, co-run a second foreground
service, or cede OS-signal ownership. Don't "fix" this by switching to `Worker.run()`. Context and upstream
issues: `docs/design/calfkit-worker-lifecycle-gaps.md`.

## Conventions

- **Commits/PRs landing on `main` use conventional-commit prefixes**: `feat:`, `fix:`, `chore:`, `docs:`,
  `refactor:`, `test:`, `perf:`, `style:`. Pick the narrowest accurate one. PR titles follow the same style
  (squash-merge inherits them).
- **New behavior ships with a test.** A branch should not regress the test count vs. the latest `main` CI run.
- **Ruff clean for new/changed files.** CI's lint job is `continue-on-error` only while a small pre-existing
  baseline of errors is cleared — don't add to it and don't fix unrelated baseline errors in the same PR.
- Comments and docstrings explain *why*, not *what*.

# Sub-agents

- When planned work is large, you may spawn sub-agents to split up or parallelize the work where possible
- Always spawn sub-agents with the opus model and xhigh thinking effort
