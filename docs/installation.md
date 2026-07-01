# Install & run calfcord

Install calfcord on a machine with a single command and configure it with a
guided session that ends with your **first agent live in Discord** — no repo
clone, and no Python, Docker, or git to set up first. This is the path the
[README quick start](../README.md#quick-start) follows. (Want to hack on calfcord
itself instead? Don't use the installer — see
[Developing calfcord](#developing-calfcord).)

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/calfcord/main/scripts/install.sh | bash
```

You don't need Python, Docker, or git installed first — the installer handles
everything, including a native **Tansu** broker and a process supervisor (more
on both below). When it finishes, **restart your shell** (or open a new terminal)
so the `calfcord` command is on your `PATH`.

## 2. Configure — `calfcord init`

```bash
calfcord init
```

`init` is one continuous, resumable session. It asks for:

- A **model provider** (Anthropic / OpenAI / Codex subscription) and its API
  key. If you pick **Codex**, it logs you into ChatGPT inline via a device code
  — it prints a URL + one-time code you open on any device, so it works the same
  on a local machine or over SSH — instead of pointing you at a separate command.
- Your **first agent**: a name (default `assistant`), a description, a **model
  picked from a live list fetched from the provider** (you select one, so you
  can't enter a slug the provider would reject), and its **tools** — a checkbox
  of every built-in with **all pre-selected**; deselect any you don't want, or
  keep them all.
- Your **Discord bot token** (verified the instant you paste it — you'll see
  `✓ Connected as <bot>`) **and application ID**. It then shows the invite link,
  **waits while you authorize the bot, and auto-detects your server and channel**
  — no numeric IDs to copy. See [`discord-setup.md`](discord-setup.md) for the
  token + app-ID prerequisites.

It writes `~/.calfcord/config/.env` plus the agent at
`~/.calfcord/agents/<name>.md` (provider, model, and tools baked in). Because the
tools step defaults to *every* tool, a freshly-configured agent has terminal,
file-write, code-execution, and web reach — see [`security.md`](security.md#33-tools-native-broker--others-in-docker)
before exposing it.

**`init` ends live.** After config it opens your workspace, brings your agent
online, and watches Discord until it sees the first reply — so the session
finishes with a working agent, not a "now run these commands" wall. Saying
`@assistant hello` afterward is a confirmation, not the moment of truth.

It's idempotent — re-run it any time to change a setting (an existing agent of
the same name is updated in place, body preserved), and a crash or Ctrl-C
resumes where you left off instead of restarting. Prefer to edit by hand? Open
`~/.calfcord/config/.env` directly (it's commented; the full list is in
[`configuration.md`](configuration.md)):

```bash
$EDITOR ~/.calfcord/config/.env
```

## How the workspace runs

`init` leaves you with a running **workspace**: a local Kafka broker plus the
Discord bridge, kept alive in the background by a process supervisor the
installer bootstraps ([Process Compose](https://f1bonacc1.github.io/process-compose),
a single static binary, downloaded to `~/.calfcord/bin/process-compose` — the
same way the Tansu broker binary is). You never edit the supervisor's config;
calfcord generates it from your agents and `.env`.

Two layers are worth keeping straight:

- **The substrate** — the broker and the bridge. This is the always-on office.
  `calfcord start` brings it up (detached, health-gated); `calfcord stop` closes
  it. `start` brings up **only** the substrate — nothing else runs that you
  didn't ask for.
- **The roster** — your agents, the tools host, and any MCP servers. These
  are teammates that clock into the running office on demand:
  `calfcord agent start <name>`, `calfcord tools start`, and so on.

> **Reboot non-survival.** The workspace is session-scoped — it does **not**
> come back automatically after a reboot. After restarting your machine, run
> `calfcord start` again (then bring your agents back online). For a workspace
> that survives reboots, register a launchd (macOS) or systemd (Linux) user unit
> — `calfcord deploy systemd` generates a starting point (see
> [Going to production](#going-to-production)).

### The workspace runs the broker for you

calfcord's processes talk to each other through a **Kafka broker**. You don't
have to run one yourself: the installer bootstraps a single Tansu binary to
`~/.calfcord/bin/tansu`, `calfcord init` selects `CALF_HOST_URL=localhost:9092`,
and `calfcord start` launches the broker as part of the substrate. There is no
separate "start the broker" step.

Tansu's default storage is **ephemeral memory** — topics and messages reset when
the broker restarts, and calfcord re-creates the topics it needs on startup. For
persistence across restarts, configure the broker with a libsql/SQLite or
postgres store via the `STORAGE_ENGINE` env var; see
[Tansu's docs](https://docs.tansu.io/).

**Bring your own / a shared broker.** Choose "I have a broker URL" in
`calfcord init`, or point an existing install at one later:

```bash
calfcord self set-broker my-broker-host:9092
```

Running agents across machines uses **one shared broker URL** — install calfcord
on each host and point them all at the same broker. See
[`distributed-deployment.md`](distributed-deployment.md).

## 3. Day-to-day — start, status, logs

`init` already opened your workspace once. From then on, these are the commands
you live in:

```bash
calfcord start              # open the workspace (broker + bridge), detached
calfcord agent start <name> # bring an agent online (a teammate clocks in)
calfcord status             # the org board: substrate + roster health
calfcord logs -f            # tail the whole workspace (add a component to scope it)
calfcord stop               # close the workspace (stops everything it manages)
```

The minimum path to a live agent is two honest commands — open the office, then
bring a teammate in:

```bash
calfcord start
calfcord agent start assistant
```

`start` is substrate-only by design, so after a fresh `start` nothing replies
until you bring an agent online; the success banner names that next step for you.

Manage which agents exist and which are running with the `agent` group:

```bash
calfcord agent list          # agents DEFINED on disk (the .md files)
calfcord agent ps            # agents RUNNING right now
calfcord agent restart <name># reload a running agent after editing its .md
calfcord agent stop <name>   # take an agent offline
```

`logs` reads the supervisor's per-component log files (which also live on disk at
`~/.calfcord/state/logs/<name>.log`); pass a component name to scope the tail,
e.g. `calfcord logs -f bridge`.

### Sanity-check with `doctor`

`calfcord doctor` is the deliberate, authoritative preflight. It runs the
**static config checks** — config file, broker reachability, the Discord bot
token + application id, and that your agents parse — and, **when the workspace is
running**, adds a **daemon-liveness check**: that the bridge heartbeat is fresh
(a live daemon, not a wedged zombie), which — because the bridge only beats once
it is connected to Discord — also confirms the gateway is up. The exit code is
non-zero on a hard failure (a ✗) and 0 on warnings, so it gates scripts cleanly.

```bash
calfcord doctor             # add --offline to skip the live Discord token check
```

For *who is actually online right now*, use `calfcord status` / `calfcord agent
ps` — the roster is read from calfkit's live agent mesh (the old end-to-end
control-plane probe was removed in the calfkit 0.12 migration). `status` is the
cheap, glanceable view; `doctor` is the thorough config + daemon check.

### Talking to an agent

An agent answers when you `@mention` it (`@assistant hello`); a message with no
`@mention` goes unanswered by design — there is no ambient router. A mentioned
agent can also consult or hand off to a peer, which the bridge projects to an
audit channel (see [`a2a-threads.md`](a2a-threads.md)).

## Where your agents live

The installer seeds a text-only starter agent at
`~/.calfcord/agents/assistant.md`; `calfcord init` writes or updates the agent
there with the provider, model, and tools you chose. Your agents live in
`~/.calfcord/agents/` and survive `calfcord self update`. To add or remove an
agent's tools interactively, run `calfcord agent tools [<name>]`, then
`calfcord agent restart <name>` (tools are loaded at agent boot). See
[`authoring-agents.md`](authoring-agents.md) for the full field reference.

The tools host's workspace defaults to **the directory you launch the workspace
(`calfcord start`) from** — agents read and write files there, the same way
Claude Code works. Mind the trust implications before pointing it at sensitive
files: [`security.md`](security.md#33-tools-native-broker--others-in-docker).

The installer also seeds an empty MCP server registry at
`~/.calfcord/config/mcp.json` (mode `0600`, never clobbered on update). It stays
empty until you add a server with `calfcord mcp add` — that's how agents reach
external [MCP](https://modelcontextprotocol.io) tools. See
[`mcp-tools.md`](mcp-tools.md).

## Going to production

Running the same agents across machines is a deployment change, not a rewrite:
the same `.env`, the same `agents/*.md`, and the same commands work on one host
or twenty. Install calfcord on each host, point them all at the **same** broker
(`calfcord self set-broker`), and on each host `calfcord start` the substrate and
`calfcord agent start` only the agents that host should run.

When you're ready for managed deployment, `calfcord deploy` renders manifests you
can hand to an init system or orchestrator:

```bash
calfcord deploy systemd -o calfcord.service   # or: k8s, docker
```

For the full picture, run `calfcord explain topology` (one screen on how the
pieces split and why) and read
[`distributed-deployment.md`](distributed-deployment.md).

## 4. Keep it up to date

```bash
calfcord self version     # show what's installed
calfcord self status      # check whether a newer version is available
calfcord self update      # upgrade to the latest
calfcord self rollback    # undo the last update
```

## Uninstall

```bash
rm -rf ~/.calfcord
```

Then remove the `# calfcord` line the installer added to your shell profile
(`~/.zshrc`, `~/.bashrc`, or `~/.bash_profile`).

---

**Pin a version:** set `CALFCORD_REF` to a branch or commit before installing,
e.g. `… | CALFCORD_REF=v1.2 bash`. `calfcord self update` then stays on that
ref.

## Developing calfcord

Don't use the installer — clone the repo and use the standard `uv` /
`docker compose` workflow so your edits are live. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md) and the
[running modes](architecture.md#running-modes) in `architecture.md`.
