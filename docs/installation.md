# Install & run calfcord

Install calfcord on a machine with a single command and configure it with a
guided prompt — no repo clone, and no Python, Docker, or git to set up first.
This is the path the [README quick start](../README.md#quick-start) follows.
(Want to hack on calfcord itself instead? Don't use the installer — see
[Developing calfcord](#developing-calfcord).)

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/calfcord/main/scripts/install.sh | bash
```

You don't need Python, Docker, or git installed first — the installer handles
everything. When it finishes, **restart your shell** (or open a new terminal)
so the `calfcord` command is on your `PATH`.

## 2. Configure

The guided setup configures your first agent **and** the install's `.env` in one
pass:

```bash
calfcord init
```

It asks for:

- A **model provider** (Anthropic / OpenAI / Codex subscription) and its API
  key. If you pick **Codex**, it logs you into ChatGPT inline via a device code
  — it prints a URL + one-time code you open on any device, so it works the same
  on a local machine or over SSH — instead of pointing you at a separate command.
- Your **first agent**: a name (default `assistant`), a description, a **model
  picked from a live list fetched from the provider** (you select one, so you
  can't enter a slug the provider would reject), and its **tools** — a checkbox
  of every built-in with **all pre-selected**; deselect any you don't want, or
  keep them all.
- Your **Discord bot token and application ID**.
- A **Kafka broker** (see [Pick a broker](#pick-a-broker)).

It writes `~/.calfcord/config/.env` plus the agent at
`~/.calfcord/agents/<name>.md` (provider, model, and tools baked in). Because the
tools step defaults to *every* tool, a freshly-configured agent has shell,
file-write, and web reach — see [`security.md`](security.md#33-tools-native-broker--others-in-docker)
before exposing it.

It's idempotent — re-run it any time to change a setting (an existing agent of
the same name is updated in place, body preserved). Prefer to edit by hand? Open
`~/.calfcord/config/.env` directly (it's commented; the full list is in
[`configuration.md`](configuration.md)):

```bash
$EDITOR ~/.calfcord/config/.env
```

### Enable ambient routing (optional)

By default an agent answers only when you `@mention` it; messages **without** an
`@mention` simply go unanswered (nothing errors). To have an agent also answer
those ambient messages, run the optional router wizard:

```bash
calfcord router setup
```

It explains the router, defaults to **your agent's provider** plus a fast/cheap
model (the router runs one LLM call per ambient message), ensures that provider's
credentials, and saves the choice. Then start `calfcord calfkit-router` (step 3)
alongside the other processes. Skip the wizard and `@mentions` still work. See
[`ambient-routing.md`](ambient-routing.md) for how routing decides who answers.

### Pick a broker

calfcord's processes talk to each other through a **Kafka broker**, so you need
one running and must point calfcord at it.

**Easy path — a local Redpanda container** (Docker required only for this).
`calfcord init` selects `CALF_HOST_URL=localhost:19092` and prints this command
to run:

```bash
docker run -d --name calfcord-redpanda -p 19092:19092 \
  docker.redpanda.com/redpandadata/redpanda:latest \
  redpanda start --mode dev-container --smp 1 \
  --kafka-addr internal://0.0.0.0:9092,external://0.0.0.0:19092 \
  --advertise-kafka-addr internal://localhost:9092,external://localhost:19092
```

**Bring your own / a shared broker.** Choose "I have a broker URL" in
`calfcord init`, or set it later:

```bash
calfcord self set-broker my-broker-host:9092
```

Running agents across machines or environments uses **one shared broker URL** —
install calfcord on each host and point them all at the same broker. See
[`distributed-deployment.md`](distributed-deployment.md).

## Where your agents live

The installer seeds a text-only starter agent at
`~/.calfcord/agents/assistant.md`; `calfcord init` (step 2) writes or updates the
agent there with the provider, model, and tools you chose. Your agents live in
`~/.calfcord/agents/` and survive `calfcord self update`. To add or remove an
agent's tools interactively, run `calfcord agent tools [<name>]`, then restart
`calfcord calfkit-agent` (tools are loaded at agent boot). See
[`authoring-agents.md`](authoring-agents.md) for the full field reference.

The tools process's workspace defaults to **the directory you launch
`calfcord calfkit-tools` from** — agents read and write files there, the same
way Claude Code works. Mind the trust implications before pointing it at
sensitive files: [`security.md`](security.md#33-tools-native-broker--others-in-docker).

## 3. Run

Start any calfcord process with `calfcord <name>`:

```bash
calfcord calfkit-bridge     # the Discord gateway
calfcord calfkit-agent      # runs your agents
calfcord calfkit-router     # routes un-mentioned messages
calfcord calfkit-tools      # tools + the agent-to-agent channel
```

On one machine you'll usually run all four. To spread them across machines,
install calfcord on each, point them all at the **same** broker (step 2), and
run only the processes that machine should handle — see
[`distributed-deployment.md`](distributed-deployment.md).

Then, in any channel the bot can see, say hello to the starter agent:

```
@assistant hello
```

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
