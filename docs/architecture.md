# Architecture

Calfcord is a set of independent processes that communicate **exclusively
through Kafka**. Each is safe to deploy on its own host, and switching between
deployment styles (supervised native, all-in-Docker, or a mix) needs no code
changes — they share the same `.env` and `agents/*.md`.

Two layers sit on top of those processes — a **substrate** that is always on and
a **roster** of teammates that clock in and out. The
[runtime model](#runtime-model-substrate--roster) below is how you operate them;
the process list and decoupling invariants that follow are what is actually
running underneath.

## The four processes

- **`calfkit-bridge`** — the single Discord gateway, a **pure calfkit
  `Client`** (the caller surface — no embedded Worker, no consumers). It owns
  all Discord I/O: it normalizes inbound events, resolves each `@mention`
  against calfkit's live agent **mesh**, invokes the agent by name
  (`client.agent(<name>).start(...)`), drains that run's event `stream()` for
  live progress and the A2A audit projection, and posts the reply back as a
  persona webhook. It derives each agent's persona from the agent's `name` and
  **does not read `agents/*.md`**.
- **`calfkit-agent`** — runs one or all agents as calfkit `Agent` nodes,
  addressed **by name** on an automatic private input topic
  (`agent.{name}.private.input`). There are no per-channel topic subscriptions
  and no addressing gate. Each agent declares `peers=[Messaging(...)/
  Handoff(...)]` (from its `a2a`/`handoff` frontmatter) for native A2A.
- **`calfkit-tools`** — runs the vendored `calfkit-tools` nodes: terminal /
  process / filesystem / search / code-execution / web / todo. There is no
  first-party tool anymore (A2A is native — the old `private_chat` tool is
  gone). Intentionally decoupled from the bridge (see below).
- **`calfkit-mcp`** — one MCP server's toolbox (a calfkit `MCPToolboxNode`).
  Each server in `mcp.json` becomes its own process (slot `mcp-<server>`) that
  connects to that external
  [Model Context Protocol](https://modelcontextprotocol.io) server, lists its
  tools, and advertises them on the compacted `mcp.capabilities` topic. One
  process per server is deliberate: a toolbox whose server is unreachable fails
  its own worker at boot, so one bad entry can't take down sibling servers.
  Agents pick the tools up from the advertisement, never the config — see
  [`mcp-tools.md`](./mcp-tools.md).

The **bridge is now the only Discord-touching process** — it hosts both the
Discord gateway and the A2A audit projection (the unified channel is named
`private-a2a-chats` by default, overridable via the bridge's
`CALFKIT_A2A_CHANNEL_NAME`). The tools process no longer touches Discord.

```mermaid
flowchart LR
    Discord(("Discord"))
    Discord <--> Bridge[calfkit-bridge]
    Bridge <--> Kafka{{Kafka / Tansu}}
    Kafka <--> Agents[calfkit-agent]
    Kafka <--> Tools[calfkit-tools]
    Kafka <--> Mcp[calfkit-mcp]
    Mcp <--> External(("MCP servers"))
```

## Decoupled deployment

The four processes have intentionally different access requirements:

| Resource                              | Bridge | Agent           | Tools | MCP |
|---------------------------------------|:------:|:---------------:|:-----:|:---:|
| `agents/*.md` (local files)           |   no   | yes (own only)  |  no   | no  |
| `mcp.json` + MCP secrets              |   no   | no              |  no   | yes |
| Discord bot token (env var)           |   yes  | no              |  no   | no  |
| Kafka broker                          |   yes  | yes             |  yes  | yes |
| LLM provider API key                  |   —    | yes             |  —    | —   |

The tools deployment is **registry-free by design**: it has no read access to
`agents/*.md` and holds no roster. A tool body only needs the calling agent's
identity (the unspoofable `x-calf-emitter` header) and the per-call `deps` the
bridge sets — it no longer receives a `phonebook`, because A2A is native
(calfkit resolves the peer directory inside the agent runtime) and the
**bridge**, not a tool, projects A2A to Discord. Practical consequences:

- The tools process runs on a host with no shared filesystem with the bridge
  and **no Discord token** (a blast-radius shrink from the old design).
- The bridge is the only Discord-touching process.
- The calfkit-native agent **mesh** (`calf.agents`) is the source of truth for
  "what agents exist and are online"; the bridge and CLI read it via
  `client.mesh` (no hand-rolled registry).

For splitting tools and agents across multiple hosts (narrowing a host's tool
surface with `CALFCORD_TOOLS_INCLUDE`, the multi-host `CALFCORD_TOOLS_ALIAS`
pattern, and broker auth/TLS), see
[`distributed-deployment.md`](./distributed-deployment.md).

### The MCP secrets boundary

`calfkit-mcp` extends the same decoupling to external tools, and adds a secrets
boundary the table above shows: **only the `mcp-<server>` processes read
`mcp.json`** (the commands, URLs, and credentials). Each toolbox advertises its
tools — names, JSON schemas, and a dispatch topic — onto the compacted
`mcp.capabilities` control-plane topic. Agents resolve their `mcp/...`
selectors against that capability view **per turn**, never against the config
file, so:

- agent hosts need no `mcp.json` and hold no MCP secrets — the credentials stay
  on the host running the server;
- a server's tool list can change with no agent restart (runtime discovery);
- a down server degrades the affected turn with a warning instead of blocking
  the agent (selection is non-strict).

See [`mcp-tools.md`](./mcp-tools.md) for the full lifecycle and selector
grammar, and [`design/mcp-reintroduction.md`](./design/mcp-reintroduction.md)
for the rationale.

## Runtime model: substrate + roster

The processes above are the *what*; this is the *how you run them*. Calfcord
splits the running system into two layers so the always-on plumbing is separate
from the teammates you stand up on demand:

- **Substrate** — the **broker** (Kafka / Tansu) and the **bridge**. This is the
  office: the message bus plus the single Discord gateway. `calfcord start`
  brings up *only* the substrate, detached and **health-gated** — it does not
  return until the bridge reports it is connected to Discord (the bridge writes
  its first heartbeat on Discord `on_ready`, so "substrate healthy" *means*
  "connected to Discord"). The broker is a fast-fail precondition; the bridge
  readiness is what the gate waits on.
- **Roster** — the **agents** and the **tools** host (plus any **MCP
  servers**). These are the teammates that clock into the live office. `start`
  deliberately does **not** auto-start any roster member — "nothing runs that
  you didn't start" is a trust property — so you bring each one online
  explicitly: `calfcord agent start <name>`, `calfcord tools start`,
  `calfcord mcp start <server>` (and the matching `stop`). `calfcord agent
  restart <name>` reloads a running agent after you edit its `.md`.

The minimum path to a live agent is two honest commands — open the office, then
clock a teammate in:

```bash
calfcord start                  # substrate only (broker + bridge), health-gated
calfcord agent start assistant  # roster: a teammate joins the live org → replies in Discord
```

`calfcord init` runs this whole sequence for a newcomer, walking both layers
visibly so the substrate↔roster split is learned by doing it. After that,
`calfcord status` is the org board (substrate health + which roster members are
online), `calfcord agent ps` shows what is *running* now (vs. `agent list`, which
shows what is *defined* on disk), and `calfcord logs [component] [-f]` tails the
unified or per-component logs.

### Process Compose supervises one host

On a single host the substrate and roster are managed by
**[Process Compose](https://f1bonacc1.github.io/process-compose)**, a
cross-platform single-binary supervisor that calfcord bootstraps the same way it
bootstraps the Tansu broker (a pinned binary under `$CALFCORD_HOME/bin`, kept out
of the agent Python path). The supervisor configuration is **derived state**:
calfcord generates `$CALFCORD_HOME/state/process-compose.yaml` from your
`agents/*.md` and config — you never hand-edit it. The CLI verbs are a thin
veneer over the supervisor's REST API.

This is where the layer split becomes mechanical. In the generated config the
substrate (broker, bridge) is declared `autostart`, while every defined agent,
plus the tools host and any MCP servers, is declared but **disabled** — present
but not started until you run its `start`. The supervisor absorbs the lifecycle
work calfcord would otherwise hand-roll:

- **Dependency ordering and health gates** — `depends_on` keeps the bridge from
  starting until the broker is healthy, and keeps roster members waiting on the
  substrate. Both the dependency gates and the `start` readiness poll are driven
  by the *same* signal: an exec readiness probe that runs
  `calfcord _healthcheck <component>` against the per-component heartbeat files
  under `$CALFCORD_HOME/state/health/`.
- **Autorestart** — the broker and bridge exit 0 on a clean return, so they use
  `restart: always`; the whole roster (agents, tools, MCP servers) runs
  on the [`run_worker_until_signal`](../src/calfcord/_worker_runtime.py) helper
  that forces a non-zero exit on a clean, signal-less return, so it uses
  `restart: on_failure`. An intentional `stop` does not trigger a restart.
- **Per-process log capture** — each component's stdout/stderr lands at
  `$CALFCORD_HOME/state/logs/<component>.log` (what `calfcord logs` tails).

### The same commands graduate to distributed

The substrate/roster model is host-agnostic. The substrate just needs to be
reachable over a broker URL, so graduating from one host to many does not change
the commands — it changes *where the broker is*. Point a second host's
`CALF_HOST_URL` at the shared substrate (`calfcord self set-broker <url>`) and
the same `calfcord agent start <name>` / `calfcord tools start` clock a teammate
into the *same* live org from a different machine. Idempotency is enforced
org-wide over the broker, not per host: `agent start` first probes the live
agent **mesh** (`client.mesh`, calfkit's native `calf.agents` heartbeat view)
across the broker, so it will not start a duplicate of an agent already online
anywhere in the organization. Liveness is now passive heartbeat-staleness
(~90 s), so a graceful stop tombstones immediately while a *crashed* agent can
still show as online until its heartbeat lapses.

For the multi-host walkthrough (per-host broker config, per-host tool narrowing,
and broker auth/TLS) see
[`distributed-deployment.md`](./distributed-deployment.md).

## Running modes

The same processes can run three ways, all sharing one `.env` and `agents/*.md`.
Pick by intent — operating an install, hacking on the source, or containerizing.

### 1. Supervised native (primary)

The end-user path. The native installer (`curl … | bash`) drops a `calfcord`
shim under `~/.calfcord/`; `calfcord init` walks you through first-run config and
ends with your first agent online. From then on you operate the
[substrate and roster](#runtime-model-substrate--roster) through the shim, and
**Process Compose supervises everything on the host** — health-gated startup,
dependency ordering, autorestart, and per-component logs, no terminal-juggling:

```bash
calfcord start                  # substrate (broker + bridge), detached, health-gated
calfcord agent start assistant  # roster: clock a teammate in → replies in Discord
calfcord status                 # org board: substrate + roster health
calfcord logs -f                # follow the unified supervisor log
calfcord stop                   # close the office (stops the supervised substrate)
```

On a native install the agent and state directories are pinned under the install
home so they survive `calfcord self update` and are found from any directory —
`CALFKIT_AGENTS_DIR` → `~/.calfcord/agents`, `CALFKIT_STATE_DIR` →
`~/.calfcord/state/agents` — while the tools **workspace follows the launch
directory** (`CALFCORD_WORKSPACE_DIR` defaults to the `$PWD` where the substrate
was launched, the Claude-Code model — agents act where you opened the office).
See the [README quick start](../README.md#quick-start) and
[`installation.md`](./installation.md) for the walkthrough, and
[`configuration.md`](./configuration.md) for overriding any of those dirs.

### 2. Low-level `uv run` (development)

For working *on* calfcord, you can run each process in the foreground yourself,
bypassing the shim and the supervisor. This is the dev/debug path; it keeps the
CWD-relative defaults (`agents/`, `state/agents/`, `state/workspace/`):

```bash
uv sync                                              # install dependencies
calfcord broker                                      # native Tansu broker — or bring your own Kafka

# Add to .env so every uv-run terminal picks it up automatically:
echo 'CALF_HOST_URL=localhost:9092' >> .env

# Each in its own terminal:
uv run calfkit-bridge
uv run calfkit-agent                                 # all agents on one Worker
# or for crash isolation per agent:
#   uv run calfkit-agent scribe
uv run calfkit-tools
uv run calfkit-mcp <server>                          # one MCP server from mcp.json (dev: ./mcp.json)
```

`localhost:9092` is the default Kafka port the native Tansu broker listens on.
Skip `calfcord broker` if you have Kafka elsewhere — just point `CALF_HOST_URL`
at it. Tansu's default storage is ephemeral memory, so topics/messages reset on
broker restart and calfcord re-creates the topics it needs on startup. Writing
the value to `.env` rather than `export`ing it means every `uv run` terminal
picks it up via `python-dotenv` without a per-shell re-export.

> The `calfcord broker` and `calfcord run <bridge|agent|tools|mcp>` shim
> verbs are the same low-level escape hatches surfaced for when you want one
> process in the foreground without the supervisor. The supervised native path
> above is what most installs use.

### 3. Docker (advanced)

The bundled `docker-compose.yml` runs the broker and each process as a service,
which is the right tier when you want containerized isolation or a reproducible
image build. Each process reads `.env` independently, and a shared Kafka broker
is the only wire-format contract between them, so modes also **mix**: run the
bridge in compose while you iterate on an agent natively, or the reverse.
Native-side processes need `CALF_HOST_URL=localhost:9092` in `.env`; containerized
services pick up `tansu:9092` from compose's per-service environment block. (To
run only the broker in Docker but the calfcord processes natively on the host,
advertise the host address: `TANSU_ADVERTISE=localhost docker compose up tansu`,
then point the native processes at `localhost:9092`.)

For unattended production hosts, `calfcord deploy <systemd|k8s|docker>` renders
the matching manifests (a systemd unit, Kubernetes resources, or a Docker
artifact) from your current config and agents, so you can hand the supervised
model off to the host's own init system or an orchestrator. See
[`distributed-deployment.md`](./distributed-deployment.md).

### Process lifecycle

The Kafka-**hosting** roster processes — `calfkit-agent`, `calfkit-tools`, and
each `calfkit-mcp` — run on calfkit's **managed** `Worker` lifecycle
(start/serve/drain and topic provisioning in one place) via the shared
[`run_worker_until_signal`](../src/calfcord/_worker_runtime.py) helper's blocking
`Worker.run()`. Each `calfkit-agent` **auto-advertises its `AgentCard`** on the
native mesh (`calf.agents`) — there is no calfcord presence/departure plumbing
anymore, and no control-plane roster to maintain.

The **bridge is different**: it is a pure calfkit `Client` (the caller surface),
**not a Worker**. It co-runs the Discord gateway (a foreground WebSocket), owns
SIGINT/SIGTERM, and — because it hosts no nodes — **owns the broker lifecycle
itself**. On a no-auto-create broker (Tansu) it provisions its own durable inbox
topic so terminal replies and intermediate step events (both publish to that
inbox) can land, and it closes the broker on an ordered shutdown after draining
in-flight `@mention` handlers.

The historical analysis of the gaps that forced the old hand-rolled loops — and
the upstream feature requests that closed them
([calfkit-sdk #165–#168](https://github.com/calf-ai/calfkit-sdk/issues/165)) —
is kept for reference in
[`design/calfkit-worker-lifecycle-gaps.md`](./design/calfkit-worker-lifecycle-gaps.md).

## Agent-to-agent communication

A2A is **native to calfkit**, not a tool. An agent that declares `a2a: true`
(the default) gets calfkit's auto-injected `message_agent(name, message)` tool,
whose description carries the live peer directory from the mesh; calfkit
dispatches the consult to the peer's private input topic and folds the reply
back into the tool result. An agent that declares `handoff: true` (also the
default) can transfer the turn to a peer, which answers the original human. Both
are declared in frontmatter — see
[`authoring-agents.md`](./authoring-agents.md#8-agent-to-agent-a2a-consult--handoff).

Consults are **stateless** — the peer answers on a fresh conversation, with no
replay of prior A2A turns. Kafka is the system of record; Discord is a
human-readable audit log, and the **bridge** (not a tool) renders it: it watches
each `@mention` run's event `stream()`, pairs each `message_agent` call with its
reply by `tool_call_id`, and projects consults and handoffs into per-turn threads
in the unified A2A channel.

See [`a2a-threads.md`](./a2a-threads.md) for the full projection design.

## Project layout

```
src/calfcord/
├── agents/        # definition, factory, runner, loader, md_writer,
│                  # thinking, memory, identifier
├── bridge/        # gateway (pure Client), mention_handler, roster,
│                  # history, normalizer, slash, overrides, persona_resolve,
│                  # progress + steps_*, a2a_dispatch, a2a_project, egress,
│                  # reply_poster, transcripts, wire
├── discord/       # client wrappers (sender, persona, avatar, receiver,
│                  # settings, messages, typing, retry_feedback)
├── tools/
│   ├── __init__.py       # explicit tool surface (ALL_TOOLS) — all
│   │                     # vendored calfkit-tools nodes (no first-party tools)
│   ├── deploy_filters.py # pure INCLUDE/ALIAS transform -> TOOL_REGISTRY
│   └── runner.py         # calfkit-tools entry point
└── mcp/           # MCP integration: selector (frontmatter grammar),
                   # agent_select (frontmatter -> MCPToolbox handles),
                   # config + config_write (mcp.json), capability_read
                   # (live per-tool display), runner (calfkit-mcp entry point)

agents/                 # agent .md definitions (live)
config/mcp.json         # MCP server registry (native install; 0600)
state/                  # runtime state: logs, health beats, transcripts.sqlite3
docs/                   # authoring guides + security model + design archive
.github/                # CI/CD workflows + Dependabot + issue/PR templates
Dockerfile, docker-compose.yml  # deployment
tests/                  # pytest suite
```
