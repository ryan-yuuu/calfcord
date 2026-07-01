# Configuration

Every Agent Disco component — the substrate (broker + bridge) and the roster (agents,
tools, and MCP servers) — is configured through environment variables, read from a
`.env` file (loaded via `python-dotenv`). On a native install the `disco` shim
reads `$CALFCORD_HOME/config/.env`; `disco init` writes most of this for you.
This page is the complete reference, including variables `init` doesn't set.

To start from a template instead of the wizard, copy and fill it in:

```bash
cp .env.example .env
```

[`.env.example`](../.env.example) is fully commented and is the canonical
starting point.

## Discord

These come from the [Discord setup walkthrough](./discord-setup.md) (~5 min). You
only need the **token + application id** by hand — `disco init` discovers your
server and default channel for you (it lists what the bot can see and you pick),
so the ID variables below are normally written for you, not copied from Discord.

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | **yes** (all deployments) | Bot token from the Developer Portal → your app → Bot tab → *Reset Token*. Treat as a secret; never commit `.env`. |
| `DISCORD_APPLICATION_ID` | **yes** (all deployments) | Numeric application ID from *General Information*. |
| `DISCORD_GUILD_ID` | recommended | Server ID for guild-scoped slash-command sync (instant; blank = global sync, ~1 h propagation). `disco init` auto-discovers it — set it by hand only when not using the wizard. |
| `DISCORD_OWNER_USER_ID` | optional | Your numeric user ID. Tags inbound messages from the owner and unlocks owner-only commands (`/clear`, `/thinking-effort`). |
| `DISCORD_DEFAULT_CHANNEL_ID` | optional (legacy) | Auto-discovered by `disco init` and written to `.env`, but **no longer consumed**: per-agent channel subscriptions were removed in the calfkit 0.12 migration, so an agent answers `@mention`s in any channel the bot can see. |

## Models / providers

Needed on **agent hosts only** (the bridge and tools processes never call an
LLM).

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | one of these | API key for `provider: anthropic` agents. |
| `OPENAI_API_KEY` | one of these | API key for `provider: openai` agents. |
| `CALFKIT_AGENT_DEFAULT_PROVIDER` | optional | Provider fallback for any agent whose `.md` omits `provider:` (e.g. the untouched seeded `assistant` before `disco init` runs). Resolution is `frontmatter → this var → anthropic`. `disco init` both sets this var *and* writes an explicit `provider:` into the agent it creates — so that agent no longer follows the var; the var remains the fallback for any other `.md` that omits `provider:`. |
| `CALFKIT_AGENT_DEFAULT_MODEL` | optional | Model fallback when an agent's `.md` omits `model:`. Lets a team track its preferred model from one place instead of editing every `.md`. Defaults to the chosen provider's project default. |

The `openai-codex` provider routes through a ChatGPT Plus/Pro subscription
instead of API credits and needs a one-time OAuth login on the host — see
[`codex-auth.md`](./codex-auth.md).

## Broker (Kafka)

The broker is the local message bus the whole org talks over. On a native install
it is part of the **substrate** that `disco start` brings up for you (the
supervisor runs the bundled native Tansu broker on `localhost:9092`), so you
normally leave `CALF_HOST_URL` at its default. Set it only to point the roster at
a broker that isn't the one `start` manages — most commonly a shared broker on
another host (see [`distributed-deployment.md`](./distributed-deployment.md)).
`disco self set-broker <host:port>` writes this var for you.

| Variable | Required | Description |
|---|---|---|
| `CALF_HOST_URL` | optional (defaults to local) | Kafka bootstrap URL(s). Default `localhost:9092` matches the substrate broker `disco start` runs. Bring-your-own / remote Kafka: point it at that broker. Full Docker Compose: leave unset; compose sets `tansu:9092` per-service. (Broker-in-Docker, processes-native: `TANSU_ADVERTISE=localhost docker compose up tansu`, then `localhost:9092`.) |

## Agents, tools & A2A

| Variable | Required | Description |
|---|---|---|
| `CALFKIT_AGENTS_DIR` | optional | Directory the **agent runner** (and the CLI) scan for agent `.md` files — the bridge does not read it. On a native install the `disco` shim defaults it to `~/.calfcord/agents` (so definitions survive `disco self update`); dev (`uv run`) and Docker keep the CWD-relative `agents/`. Override via shell env or `~/.calfcord/config/.env`. |
| `DISCORD_TRANSCRIPT_DB_PATH` | optional | Bridge-local SQLite store (default `state/transcripts.sqlite3`). Holds the per-turn transcripts behind the ⤵ expand toggle **and** the persisted per-agent `/thinking-effort` overrides (the `agent_overrides` table), so an override survives a bridge restart. Read only by the bridge. |
| `CALFKIT_A2A_CHANNEL_NAME` | optional | Name of the unified A2A audit channel, **read by the bridge** (the A2A projection moved from the tools process to the bridge in the calfkit 0.12 migration). Code default `private-a2a-chats`; lazy-created on the first A2A projection. |
| `CALFKIT_A2A_CHANNEL_CATEGORY` | optional | Discord category to group the A2A audit channel under, created lazily on first use. Read by the bridge. Edit the category's permission overwrites once to lock down audit visibility — the channel and its threads inherit them. Non-disruptive to enable on a running deployment. |
| `CALFCORD_WORKSPACE_DIR` | optional | Host path the terminal/filesystem/search tools resolve against. The tools runner resolves it once at boot and exports it as `TERMINAL_CWD`, the working directory the vendored hermes terminal starts each agent's shell session in. Native install: the `disco` shim defaults it to **the directory the workspace (`disco start`) was launched from** (`$PWD`, the Claude-Code model — not a hidden dir). Bare `uv run` keeps the CWD-relative `<cwd>/state/workspace/`. Docker Compose: set to `/workspace` (bind-mounted from the dedicated `./workspace` scratch dir, **not** the project root). All agents share this dir — see [`security.md`](./security.md) § 3.3. |
| `TERMINAL_CWD` | optional | Working directory the hermes terminal starts each session in, read per-call by the vendored backend. The tools runner derives it from `CALFCORD_WORKSPACE_DIR` at boot; set it explicitly only to override that derivation — an operator-set value wins and is left untouched. |
| `CALFCORD_TOOLS_ALIAS` | optional | Per-host tool aliases (`src=dst,…`) — exposes a tool under a second wire name for multi-host routing. **Managed by `disco tools alias add/list/remove`** (which validates against the live tool surface and writes this key); read at boot by every role. See [`distributed-deployment.md`](./distributed-deployment.md) § 3. |
| `CALFCORD_TOOLS_INCLUDE` | optional | Comma-separated allow-list narrowing the tools a host serves (per-host tool subsetting). Set on a tools host; read at boot. See [`distributed-deployment.md`](./distributed-deployment.md) § 2. |

## MCP servers (`mcp.json`)

MCP servers are configured in a **separate file**, `mcp.json` — not in `.env`.
Only the `calfkit-mcp` processes (and the `disco mcp` CLI) read it; agents
resolve their MCP tools from the broker, so an agent host needs no `mcp.json`.
Manage it with `disco mcp add|list|remove` and the per-server lifecycle
verbs — see [`mcp-tools.md`](./mcp-tools.md) for the full schema and workflow.

The file lives at `$CALFCORD_HOME/config/mcp.json` (next to `config/.env`),
seeded empty (`{"mcpServers": {}}`, mode `0600`) by the installer and never
clobbered. Secret values inside `mcp.json` should be `$VAR` references whose
values you set in `config/.env`; they are expanded when a server starts.

| Variable | Required | Description |
|---|---|---|
| `CALFCORD_MCP_CONFIG` | optional | Absolute path to the `mcp.json` an MCP process / the `mcp` CLI reads, overriding the default. Resolution: this var → `$CALFCORD_HOME/config/mcp.json` → `./mcp.json` (dev fallback). |

## Supervisor (Process Compose)

On a native install, `disco start` runs the substrate under a small process
supervisor ([Process Compose](https://github.com/F1bonacc1/process-compose)). The
installer bootstraps the binary into `$CALFCORD_HOME/bin/process-compose`; you
never write its config (Agent Disco generates it). These variables only matter if
you're overriding the bootstrap or running two installs on one host.

| Variable | Required | Description |
|---|---|---|
| `CALFCORD_HOME` | optional | Install root. Defaults to `~/.calfcord`. The shim exports it so Agent Disco can find `config/.env`, the agents dir, and the supervisor's state. Two installs on one host = two `CALFCORD_HOME`s. |
| `CALFCORD_PROCESS_COMPOSE_VERSION` | optional (installer-time) | Pins the Process Compose release the installer downloads. Defaults to `v1.110.0` (the version Agent Disco's supervisor wire contract is tested against — don't change it without reason). |
| `CALFCORD_PROCESS_COMPOSE_BIN` | optional | Absolute path to a `process-compose` binary to use instead of the bootstrapped one (dev override / packaging). Resolution: this var → `$CALFCORD_HOME/bin/process-compose` → `process-compose` on `PATH`. A stale value pointing at nothing falls through rather than masking a working binary. |
| `PC_API_TOKEN` | optional | Shared key (≥ 20 chars) for the supervisor's REST API, if you've secured it. Agent Disco sends it in the `X-PC-Token-Key` header; unset = unauthenticated (the common single-user case). |

The supervisor's REST port is **not** a variable — Agent Disco derives a stable,
non-colliding port from the absolute `CALFCORD_HOME` path (a high band that avoids
the supervisor default `8080` and the broker's `9092`), so a second
`CALFCORD_HOME` on the same host gets its own port automatically.

## State & log directories

Everything the running workspace writes lives under `$CALFCORD_HOME/state/`:

| Path | What's there |
|---|---|
| `$CALFCORD_HOME/state/logs/<name>.log` | Per-component stdout/stderr (`broker`, `bridge`, each agent, `tools`, each `mcp-<server>`), plus the supervisor's own `process-compose.log`. Tail with `disco logs [component] [-f]`. |
| `$CALFCORD_HOME/state/health/<component>.json` | Heartbeat files each long-lived component refreshes; `disco doctor` and the supervisor's readiness probes read these. |
| `$CALFCORD_HOME/state/transcripts.sqlite3` | Bridge-local SQLite store: per-turn transcripts (⤵ expand toggle) and persisted `/thinking-effort` overrides. See `DISCORD_TRANSCRIPT_DB_PATH` above. |
| `$CALFCORD_HOME/state/process-compose.yaml` | The generated supervisor project — derived state, regenerated on every `start`. Don't edit it. |

## Applying changes

`.env` is read **once, at process boot** — each component (every agent, the tools
host, the bridge) loads its environment when it starts and holds it for its
lifetime. Changing a key, URL, or override in `.env` therefore does **nothing** to
an already-running process; you have to **restart the process that reads it** for
the new value to take effect. (This is the same one-shot constraint behind the
"restart to apply" note on every config-mutating command — `agent set` and the
interactive editors all print the matching restart command on success.)

Which restart depends on which process reads the value you changed:

| You changed… | Restart |
|---|---|
| One agent's key, model, or provider (`agent set`/`edit`) | `disco agent restart <name>` |
| A key several agents share (e.g. `ANTHROPIC_API_KEY`, `CALFKIT_AGENT_DEFAULT_MODEL`) | `disco agent restart --all` (every agent on this host) |
| Anything the tools host reads | `disco tools restart` |
| A bridge value (`CALFKIT_A2A_CHANNEL_*`, `DISCORD_TRANSCRIPT_DB_PATH`, the Discord token) | `disco stop && disco start` (the bridge is part of the substrate) |
| A workspace-wide value the whole roster reads (e.g. `CALF_HOST_URL`) | `disco stop && disco start`, then bring the roster back up on the new value: `disco agent start --all` plus `disco tools start` (and `disco mcp start --all`) for any of those you run |

> **Boot-time gotcha for workspace-wide values.** `disco stop` tears the
> **whole** workspace down (broker + bridge **and** every agent and singleton),
> and `disco start` brings up the **substrate only** (broker + bridge) — it
> does *not* bring the roster back. So after changing a value the *roster* reads
> (like `CALF_HOST_URL`, which every agent, tool, and MCP server dials),
> `disco stop && disco start` leaves you with the substrate up but **no
> agents running**. Re-start the roster on the new value with `disco agent
> start --all` (which targets every *defined* agent — `agent restart --all` would
> be a no-op here, because nothing is running to restart) **and** `disco tools
> start` (plus `disco mcp start --all` for MCP servers) for whichever roster
> members you run. All of these are local — they act on this host's processes.

## See also

- [`discord-setup.md`](./discord-setup.md) — getting the `DISCORD_*` values.
- [`architecture.md`](./architecture.md) — which process needs which variable.
- [`authoring-agents.md`](./authoring-agents.md) — per-agent frontmatter (the
  `.md` config that complements these env vars).
- [`mcp-tools.md`](./mcp-tools.md) — the `mcp.json` MCP-server registry and the
  `disco mcp` CLI.
