# Configuration

All four processes are configured through environment variables, read from a
`.env` file at the project root (loaded via `python-dotenv`). Copy the template
and fill it in:

```bash
cp .env.example .env
```

[`.env.example`](../.env.example) is fully commented and is the canonical
starting point. This page is the complete reference, including variables that
aren't in the template by default.

## Discord

These come from the [Discord setup walkthrough](./discord-setup.md) (~5 min).

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | **yes** (all deployments) | Bot token from the Developer Portal → your app → Bot tab → *Reset Token*. Treat as a secret; never commit `.env`. |
| `DISCORD_APPLICATION_ID` | **yes** (all deployments) | Numeric application ID from *General Information*. |
| `DISCORD_GUILD_ID` | recommended | Server ID for guild-scoped slash-command sync (instant). Blank = global sync (~1 h propagation). Required for bridge + tools in practice. |
| `DISCORD_OWNER_USER_ID` | optional | Your numeric user ID. Tags inbound messages from the owner and unlocks owner-only commands (`/clear`, `/thinking-effort`). |
| `DISCORD_DEFAULT_CHANNEL_ID` | optional | Channel ID used to seed the first agent's channel subscription on boot (fallback when its `CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` is unset). |

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

Needed on the **`calfkit-router` host only** (the optional ambient-message
router — see [`ambient-routing.md`](./ambient-routing.md)). The easiest way to
set both is the `calfcord router setup` wizard, which picks a fast/cheap model,
ensures the provider's credentials, and writes these two vars for you.

| Variable | Required | Description |
|---|---|---|
| `CALFKIT_ROUTER_PROVIDER` | optional | Overrides the router's provider. Resolution is `this var → router.md frontmatter → in-code default`. The bundled `router.md` pins `openai-codex`, so out of the box the router needs Codex (ChatGPT-subscription) auth (`codex-auth.md`); set this to `anthropic`/`openai` to retarget without replacing `router.md`. |
| `CALFKIT_ROUTER_MODEL` | optional | Overrides the router's model. Same `this var → router.md frontmatter → in-code default` precedence; the bundled `router.md` pins `gpt-5.4-mini`. An invalid value fails loudly at boot, not silently. |

## Kafka

| Variable | Required | Description |
|---|---|---|
| `CALF_HOST_URL` | depends on run mode | Kafka bootstrap URL(s). Native: leave unset → `localhost`. Native + docker broker: `localhost:19092`. Full Docker Compose: leave unset; compose sets `redpanda:9092` per-service. |

## Agents, tools & A2A

| Variable | Required | Description |
|---|---|---|
| `CALFKIT_AGENTS_DIR` | optional | Directory the bridge/agent processes scan for agent `.md` files. On a native install the `calfcord` shim defaults it to `~/.calfcord/agents` (so definitions survive `calfcord self update`); dev (`uv run`) and Docker keep the CWD-relative `agents/`. Override via shell env or `~/.calfcord/config/.env`. |
| `CALFKIT_STATE_DIR` | optional | Directory holding per-agent channel-subscription JSON. On a native install the shim defaults it to `~/.calfcord/state/agents` (so it persists regardless of launch directory); dev and Docker keep the CWD-relative `state/agents/`. |
| `CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` | optional | Comma-separated channel IDs seeded on an agent's **first** boot (e.g. `CALFKIT_AGENT_SCRIBE_BOOTSTRAP_CHANNELS`). Falls back to `DISCORD_DEFAULT_CHANNEL_ID`. After first boot, subscriptions live in `state/agents/<name>.json`. |
| `CALFKIT_TOOLS_TIMEOUT_SECONDS` | optional | Per-call timeout for `private_chat` (default `60`). Other built-in tools have no default per-call timeout at the calfkit layer. |
| `CALFKIT_A2A_CHANNEL_NAME` | optional | Name of the unified A2A audit channel. Code default is `private-a2a-chats`; the bundled `docker-compose.yml` overrides it to `private-a2a`. |
| `CALFKIT_A2A_CHANNEL_CATEGORY` | optional | Discord category to group the A2A audit channel under, created lazily on first use. Edit the category's permission overwrites once to lock down audit visibility — the channel and its threads inherit them. Non-disruptive to enable on a running deployment. |
| `CALFCORD_WORKSPACE_DIR` | optional | Host path the filesystem/search/shell tools resolve against. Native install: the `calfcord` shim defaults it to **the directory `calfcord calfkit-tools` was launched from** (`$PWD`, the Claude-Code model — not a hidden dir). Bare `uv run` keeps the CWD-relative `<cwd>/state/workspace/`. Docker Compose: set to `/workspace` (bind-mounted from the dedicated `./workspace` scratch dir, **not** the project root). All agents share this dir — see [`security.md`](./security.md) § 3.3. |
| `CALFCORD_SHELL_BACKEND` | optional | Force the `shell` tool backend: `tmux` \| `subprocess` \| `powershell`. Default auto-detects (tmux if installed, else subprocess). |

## Per-agent runtime state

Channel subscriptions are persisted per agent in `state/agents/<name>.json`
(atomically written). On an agent's first boot, channels are seeded from
`CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` or `DISCORD_DEFAULT_CHANNEL_ID`;
after that, the state file wins.

## See also

- [`discord-setup.md`](./discord-setup.md) — getting the `DISCORD_*` values.
- [`architecture.md`](./architecture.md) — which process needs which variable.
- [`authoring-agents.md`](./authoring-agents.md) — per-agent frontmatter (the
  `.md` config that complements these env vars).
