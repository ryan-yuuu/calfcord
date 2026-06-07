# Configuration

Every calfcord component — the substrate (broker + bridge) and the roster (agents,
tools, router, MCP) — is configured through environment variables, read from a
`.env` file (loaded via `python-dotenv`). On a native install the `calfcord` shim
reads `$CALFCORD_HOME/config/.env`; `calfcord init` writes most of this for you.
This page is the complete reference, including variables `init` doesn't set.

To start from a template instead of the wizard, copy and fill it in:

```bash
cp .env.example .env
```

[`.env.example`](../.env.example) is fully commented and is the canonical
starting point.

## Discord

These come from the [Discord setup walkthrough](./discord-setup.md) (~5 min). You
only need the **token + application id** by hand — `calfcord init` discovers your
server and default channel for you (it lists what the bot can see and you pick),
so the ID variables below are normally written for you, not copied from Discord.

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | **yes** (all deployments) | Bot token from the Developer Portal → your app → Bot tab → *Reset Token*. Treat as a secret; never commit `.env`. |
| `DISCORD_APPLICATION_ID` | **yes** (all deployments) | Numeric application ID from *General Information*. |
| `DISCORD_GUILD_ID` | recommended | Server ID for guild-scoped slash-command sync (instant; blank = global sync, ~1 h propagation). `calfcord init` auto-discovers it — set it by hand only when not using the wizard. |
| `DISCORD_OWNER_USER_ID` | optional | Your numeric user ID. Tags inbound messages from the owner and unlocks owner-only commands (`/clear`, `/thinking-effort`). |
| `DISCORD_DEFAULT_CHANNEL_ID` | optional | Channel ID used to seed the first agent's channel subscription on boot (fallback when its `CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` is unset). Auto-discovered by `calfcord init`. |

## Models / providers

Needed on **agent hosts only** (the bridge and tools processes never call an
LLM).

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | one of these | API key for `provider: anthropic` agents. |
| `OPENAI_API_KEY` | one of these | API key for `provider: openai` agents. |
| `CALFKIT_AGENT_DEFAULT_PROVIDER` | optional | Provider fallback for any agent whose `.md` omits `provider:` (e.g. the untouched seeded `assistant` before `calfcord init` runs). Resolution is `frontmatter → this var → anthropic`. `calfcord init` both sets this var *and* writes an explicit `provider:` into the agent it creates — so that agent no longer follows the var; the var remains the fallback for any other `.md` that omits `provider:`. |
| `CALFKIT_AGENT_DEFAULT_MODEL` | optional | Model fallback when an agent's `.md` omits `model:`. Lets a team track its preferred model from one place instead of editing every `.md`. Defaults to the chosen provider's project default. |

The `openai-codex` provider routes through a ChatGPT Plus/Pro subscription
instead of API credits and needs a one-time OAuth login on the host — see
[`codex-auth.md`](./codex-auth.md).

## Ambient router

Needed on the **router host only** (the optional ambient-message router — see
[`ambient-routing.md`](./ambient-routing.md)). The easiest way to set both is
`calfcord router edit`, which picks a fast/cheap model, ensures the provider's
credentials, and writes these two vars for you (`calfcord router set` does the
same non-interactively). Bring it online with `calfcord router start`.

| Variable | Required | Description |
|---|---|---|
| `CALFKIT_ROUTER_PROVIDER` | optional | Overrides the router's provider. Resolution is `this var → router.md frontmatter → in-code default`. The bundled `router.md` pins `openai-codex`, so out of the box the router needs Codex (ChatGPT-subscription) auth (`codex-auth.md`); set this to `anthropic`/`openai` to retarget without replacing `router.md`. |
| `CALFKIT_ROUTER_MODEL` | optional | Overrides the router's model. Same `this var → router.md frontmatter → in-code default` precedence; the bundled `router.md` pins `gpt-5.4-mini`. An invalid value fails loudly at boot, not silently. |

## Broker (Kafka)

The broker is the local message bus the whole org talks over. On a native install
it is part of the **substrate** that `calfcord start` brings up for you (the
supervisor runs the bundled native Tansu broker on `localhost:9092`), so you
normally leave `CALF_HOST_URL` at its default. Set it only to point the roster at
a broker that isn't the one `start` manages — most commonly a shared broker on
another host (see [`distributed-deployment.md`](./distributed-deployment.md)).
`calfcord self set-broker <host:port>` writes this var for you.

| Variable | Required | Description |
|---|---|---|
| `CALF_HOST_URL` | optional (defaults to local) | Kafka bootstrap URL(s). Default `localhost:9092` matches the substrate broker `calfcord start` runs. Bring-your-own / remote Kafka: point it at that broker. Full Docker Compose: leave unset; compose sets `tansu:9092` per-service. (Broker-in-Docker, processes-native: `TANSU_ADVERTISE=localhost docker compose up tansu`, then `localhost:9092`.) |

## Agents, tools & A2A

| Variable | Required | Description |
|---|---|---|
| `CALFKIT_AGENTS_DIR` | optional | Directory the bridge/agent processes scan for agent `.md` files. On a native install the `calfcord` shim defaults it to `~/.calfcord/agents` (so definitions survive `calfcord self update`); dev (`uv run`) and Docker keep the CWD-relative `agents/`. Override via shell env or `~/.calfcord/config/.env`. |
| `CALFKIT_STATE_DIR` | optional | Directory holding per-agent channel-subscription JSON. On a native install the shim defaults it to `~/.calfcord/state/agents` (so it persists regardless of launch directory); dev and Docker keep the CWD-relative `state/agents/`. |
| `CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` | optional | Comma-separated channel IDs seeded on an agent's **first** boot (e.g. `CALFKIT_AGENT_SCRIBE_BOOTSTRAP_CHANNELS`). Falls back to `DISCORD_DEFAULT_CHANNEL_ID`. After first boot, subscriptions live in `state/agents/<name>.json`. |
| `CALFKIT_TOOLS_TIMEOUT_SECONDS` | optional | Per-call timeout for `private_chat` (default `60`). Other built-in tools have no default per-call timeout at the calfkit layer. |
| `CALFKIT_A2A_CHANNEL_NAME` | optional | Name of the unified A2A audit channel. Code default is `private-a2a-chats`; the bundled `docker-compose.yml` overrides it to `private-a2a`. |
| `CALFKIT_A2A_CHANNEL_CATEGORY` | optional | Discord category to group the A2A audit channel under, created lazily on first use. Edit the category's permission overwrites once to lock down audit visibility — the channel and its threads inherit them. Non-disruptive to enable on a running deployment. |
| `CALFCORD_WORKSPACE_DIR` | optional | Host path the filesystem/search/shell tools resolve against. Native install: the `calfcord` shim defaults it to **the directory the workspace (`calfcord start`) was launched from** (`$PWD`, the Claude-Code model — not a hidden dir). Bare `uv run` keeps the CWD-relative `<cwd>/state/workspace/`. Docker Compose: set to `/workspace` (bind-mounted from the dedicated `./workspace` scratch dir, **not** the project root). All agents share this dir — see [`security.md`](./security.md) § 3.3. |
| `CALFCORD_SHELL_BACKEND` | optional | Force the `shell` tool backend: `tmux` \| `subprocess` \| `powershell`. Default auto-detects (tmux if installed, else subprocess). |

## Supervisor (Process Compose)

On a native install, `calfcord start` runs the substrate under a small process
supervisor ([Process Compose](https://github.com/F1bonacc1/process-compose)). The
installer bootstraps the binary into `$CALFCORD_HOME/bin/process-compose`; you
never write its config (calfcord generates it). These variables only matter if
you're overriding the bootstrap or running two installs on one host.

| Variable | Required | Description |
|---|---|---|
| `CALFCORD_HOME` | optional | Install root. Defaults to `~/.calfcord`. The shim exports it so calfcord can find `config/.env`, the agents dir, and the supervisor's state. Two installs on one host = two `CALFCORD_HOME`s. |
| `CALFCORD_PROCESS_COMPOSE_VERSION` | optional (installer-time) | Pins the Process Compose release the installer downloads. Defaults to `v1.110.0` (the version calfcord's supervisor wire contract is tested against — don't change it without reason). |
| `CALFCORD_PROCESS_COMPOSE_BIN` | optional | Absolute path to a `process-compose` binary to use instead of the bootstrapped one (dev override / packaging). Resolution: this var → `$CALFCORD_HOME/bin/process-compose` → `process-compose` on `PATH`. A stale value pointing at nothing falls through rather than masking a working binary. |
| `PC_API_TOKEN` | optional | Shared key (≥ 20 chars) for the supervisor's REST API, if you've secured it. calfcord sends it in the `X-PC-Token-Key` header; unset = unauthenticated (the common single-user case). |

The supervisor's REST port is **not** a variable — calfcord derives a stable,
non-colliding port from the absolute `CALFCORD_HOME` path (a high band that avoids
the supervisor default `8080` and the broker's `9092`), so a second
`CALFCORD_HOME` on the same host gets its own port automatically.

## State & log directories

Everything the running workspace writes lives under `$CALFCORD_HOME/state/`:

| Path | What's there |
|---|---|
| `$CALFCORD_HOME/state/logs/<name>.log` | Per-component stdout/stderr (`broker`, `bridge`, each agent, `tools`/`router`/`mcp`), plus the supervisor's own `process-compose.log`. Tail with `calfcord logs [component] [-f]`. |
| `$CALFCORD_HOME/state/health/<component>.json` | Heartbeat files each long-lived component refreshes; `calfcord doctor` and the supervisor's readiness probes read these. |
| `$CALFCORD_HOME/state/agents/<name>.json` | Per-agent channel-subscription state (see `CALFKIT_STATE_DIR` above and the next section). |
| `$CALFCORD_HOME/state/process-compose.yaml` | The generated supervisor project — derived state, regenerated on every `start`. Don't edit it. |

## Per-agent runtime state

Channel subscriptions are persisted per agent in `state/agents/<name>.json`
(atomically written). On an agent's first boot, channels are seeded from
`CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` or `DISCORD_DEFAULT_CHANNEL_ID`;
after that, the state file wins.

## Applying changes

`.env` is read **once, at process boot** — each component (every agent, the
router, the tools/MCP hosts, the bridge) loads its environment when it starts and
holds it for its lifetime. Changing a key, URL, or override in `.env` therefore
does **nothing** to an already-running process; you have to **restart the process
that reads it** for the new value to take effect. (This is the same one-shot
constraint behind the "restart to apply" note on every config-mutating command —
`agent set`, `router set`, and the interactive editors all print the matching
restart command on success.)

Which restart depends on which process reads the value you changed:

| You changed… | Restart |
|---|---|
| One agent's key, model, or provider (`agent set`/`edit`) | `calfcord agent restart <name>` |
| A key several agents share (e.g. `ANTHROPIC_API_KEY`, `CALFKIT_AGENT_DEFAULT_MODEL`) | `calfcord agent restart --all` (every agent on this host) |
| The router's provider/model (`CALFKIT_ROUTER_*`, `router set`/`edit`) | `calfcord router restart` |
| Anything the tools host reads | `calfcord tools restart` |
| Anything the MCP host reads | `calfcord mcp restart` |
| A workspace-wide value the whole roster reads (e.g. `CALF_HOST_URL`) | `calfcord stop && calfcord start`, then restart the running roster: `calfcord agent restart --all` plus `calfcord tools restart` / `router restart` / `mcp restart` for any of those you run |

> **Boot-time gotcha for workspace-wide values.** `calfcord start` brings up the
> **substrate only** (broker + bridge) — it does *not* restart the roster. So
> after changing a value the *roster* reads (like `CALF_HOST_URL`, which every
> agent, tool, router, and MCP host dials), `calfcord stop && calfcord start`
> reloads the substrate but leaves the roster running on the *old* value. Follow
> it with `calfcord agent restart --all` **and** `calfcord tools restart` /
> `router restart` / `mcp restart` for whichever roster members you run, to roll
> the whole roster onto the new value. All of these are local — they act on this
> host's running processes.

## See also

- [`discord-setup.md`](./discord-setup.md) — getting the `DISCORD_*` values.
- [`architecture.md`](./architecture.md) — which process needs which variable.
- [`authoring-agents.md`](./authoring-agents.md) — per-agent frontmatter (the
  `.md` config that complements these env vars).
