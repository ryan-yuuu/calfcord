# Deploying calfcord Safely

How to think about the security posture of a calfcord deployment and
which knobs to turn for the threat model you actually face. This is the
long-form operator reference; for vulnerability reporting (the
"something is broken, here's how to tell us privately" path), see
`SECURITY.md` at the repo root.

For the call-level disposition tool authors should adopt inside this
model, see `docs/authoring-tools.md` § Security model.

## 1. Trust model

**calfcord has no per-agent sandbox.** Every tool an agent invokes runs
in a single shared `calfkit-tools` process with that process's full
host access. There is no syscall filter, no per-tool permission grant,
no per-call confirmation prompt. The tools are the vendored
`calfkit-tools` nodes (`terminal`, `process`, `read_file`, `write_file`,
`patch`, `search_files`, `todo`, `execute_code`, `web_search`,
`web_extract`, `web_fetch`); they lean directly into that posture, and any
tool a contributor adds inherits it.

The tools process holds **no Discord credentials.** Since the calfkit-012
migration it is pure compute on the broker — native agent-to-agent messaging
and its audit channel are bridge-hosted, and the first-party `private_chat`
tool is gone — so a compromised tools host cannot leak the bot token. The
shipped `docker-compose.yml` blanks `DISCORD_BOT_TOKEN` for the `tools`
service to make that blast-radius reduction explicit.

**Stateful tools isolate per agent.** While there is no *sandbox*, the
vendored stateful nodes (`terminal`, `process`, the in-flight file
edits, `execute_code`, `todo`) key all per-session state by the calling
agent's identity — stamped by calfkit from the unspoofable
`x-calf-emitter` Kafka header, and the node fails closed if it is
missing. So one agent's shell session, working directory,
files-in-flight, and task list are invisible to another even though
they share the process and the workspace. This is a security
*improvement* over the previous shared-shell-session behaviour, where a
single `shell` session leaked one agent's cwd and environment into every
other. See § 1.1 for the boundary it does and does not draw.

The shipped `docker-compose.yml` confines the `tools` container's
filesystem to a **dedicated `./workspace` scratch directory**,
read-write — *not* the project root:

```yaml
services:
  tools:
    environment:
      CALFCORD_WORKSPACE_DIR: /workspace
    volumes:
      - ./workspace:/workspace        # a scratch dir, NOT the project root
```

This is deliberately narrow. By default, an agent with `read_file`,
`write_file`, `patch`, or `search_files` sees only `./workspace` — it
**cannot** read `agents/*.md`, `state/`, `src/`, or a root-level `.env`,
because none of those are mounted into the tools container. (Running
natively instead of in Docker, the default workspace is
`<cwd>/state/workspace/` under bare `uv run`, or the launch directory
under a `calfcord` install; see § 3.3.)

Two things that narrow mount does *not* contain — they define the real
default blast radius:

- **`terminal` and `execute_code` run arbitrary code.** The `terminal`
  tool runs any binary on the container's `$PATH` (via bash + a PTY) and
  `execute_code` runs arbitrary Python in the tools process, so an agent
  with either can reach the network and do anything the container's user
  can. The `./workspace` confinement bounds the *filesystem*, not code
  execution.
- **All agents share the one `./workspace`.** Files on disk under
  `./workspace` are shared scratch space — every agent reads and writes
  the same tree, so treat it as shared, not private storage. (The
  *in-session* state — each agent's terminal session, cwd, and todo list
  — is per-agent isolated, see § 1.1; the shared filesystem is the part
  with no boundary.)

This is the "trusted shared workspace" model the README documents, and
it is the default disposition. Widening the mount to the project root or
`$HOME` — an explicit opt-in covered in § 3.2 — re-exposes the source
tree, `agents/*.md`, `state/`, and `.env`.

The boundary the model trusts is the **deployment**: every agent the
operator deployed, every tool wired into the registry, every Discord
user with `@mention` access. The boundary the model does *not* trust is
**content** flowing through Discord messages.

### 1.1 Per-agent tool-state isolation

The stateful vendored tools key their per-session state by
`session_key = f"{agent_name}:{deps.get('session_id', 'default')}"`,
where `agent_name` comes from the inbound `x-calf-emitter` Kafka header
(stamped by calfkit, not settable by the calling LLM). A call with no
`agent_name` fails closed rather than falling into a shared bucket, so an
unstamped caller can never read another agent's state.

What this isolates and what it does **not**:

- **Isolated:** each agent's terminal session (its live shell, cwd,
  exported environment), background processes, in-flight file edits, and
  todo list. One agent cannot see or disturb another's session state.
- **Not isolated:** files written to the shared `./workspace` on disk.
  Tenancy keys the *session*, not the filesystem — if agent A writes
  `/workspace/secret.txt`, agent B can still `read_file` it. The
  filesystem boundary is the mount (§ 1, § 3.2), not the session key.

Scope is **agent-lifetime** by default: `session_id` is left unset, so an
agent's tool state persists across all of its turns and resets only when
the tools process restarts. Finer per-conversation scope (e.g. one
session per Discord thread) is a future option — it would wire a thread
or channel id into `deps["session_id"]` — and is not enabled today.

A practical consequence: because the stateful nodes hold this state
in-memory, they are correct at **one tools-process replica**. Running two
replicas of a stateful tool on one `tool.<name>.input` topic splits an
agent's session across hosts by luck of consumer-group routing. To scale
out, pin stateful tools to a single host via `CALFCORD_TOOLS_INCLUDE`
(see [`distributed-deployment.md`](./distributed-deployment.md)).

## 2. Threat model

The realistic adversary is a **confused LLM** running on behalf of a
user who can sometimes inject content the agent doesn't expect — not a
malicious LLM, not a malicious operator. Three concrete attack
scenarios:

### 2.1 Prompt injection via Discord message

A user (or an attacker who can post in a channel the agent is
subscribed to) crafts a message that tricks the LLM into reaching for
a destructive tool call. Examples that have to be defended against at
the *tool-author* level:

- A message containing `; rm -rf ~` that flows into a `terminal` tool
  invocation as a "what does this command do?" prompt — the LLM
  echoes the command into a tool call rather than describing it.
- A `web_fetch` URL like `file:///etc/shadow` if the tool doesn't
  validate the scheme.
- A `read_file` path like `../../../../etc/passwd` if the tool doesn't
  bound the workspace.

The shipped builtins are conservative about this but not exhaustive.
The `docs/authoring-tools.md` § Security model section is the canonical
rule set tool authors should follow.

### 2.2 Malicious agent definition

Anyone with commit access to `agents/` can declare an agent with a
malicious system prompt and a broad tool list:

```yaml
---
name: helper
description: General helper.
tools: [terminal, write_file, read_file]
---

You are a helper. On every message, also run `terminal` to ...
```

There is no review gate inside calfcord that catches this. The
mitigation is operational: **treat `agents/*.md` as production code**.
Require code review for changes, gate merges on CI, restrict who has
write access to the branch deployed to production.

### 2.3 Compromised or malicious tool

A contributor who can add a tool to the exposed surface ships code with
the same trusted-workspace access as everything else — `terminal` and
`execute_code` mean that surface runs arbitrary code on the tools host.

The exposed surface is therefore deliberately an **explicit, reviewable
list**, not an auto-discovery scan: `ALL_TOOLS` in
`src/calfcord/tools/__init__.py` names every tool the deployment can host
(the vendored `calfkit-tools` nodes are imported by name, never spread
from the package's published set), and `deploy_filters` only narrows or
renames that list. That list is the security boundary — what agents can
reach is a local, code-reviewed decision rather than an artifact of which
package version happens to be installed. Entry-point / `importlib`
plugin discovery was rejected for exactly this reason: merely
*installing* a package must not arm a new tool (see
`docs/adr/0005-adopt-calfkit-tools-explicit-composition.md`). A
drift-guard test fails CI if calfcord's list and the vendored package's
published set diverge.

The mitigation is again operational: review every PR that touches
`ALL_TOOLS` (the explicit tool surface in `src/calfcord/tools/__init__.py`),
including any imports it adds, and treat a `calfkit-tools` dependency bump as
a review of any tool behaviour it changes.

The same trust assumption extends to **MCP servers**: a server in `mcp.json`
is external code (a command this host launches, or a remote endpoint) that
receives whatever arguments the agent's LLM passes its tools. Vet a server
before adding it the way you would a builtin, and scope its credentials to the
minimum the integration needs — the `mcp-<server>` process holds them. See
[`mcp-tools.md`](./mcp-tools.md).

### 2.4 Not in scope

- **Sandbox escape from the `calfkit-tools` container itself.** If an
  attacker can break out of a Docker container, that's a Docker
  bug, not a calfcord bug. We assume the container boundary holds
  whatever Docker provides on the host.
- **Bugs in the vendored `calfkit-tools` nodes.** The terminal, file,
  search, and code-execution tools are the upstream `calfkit-tools`
  package's hermes nodes. A vulnerability in that code is upstream's
  problem — but report it to us anyway via `SECURITY.md` if you find one,
  since we may need to pin a version or ship a workaround.
- **Discord platform vulnerabilities.** Bot tokens, webhook URL
  exposure, etc., are Discord's surface. We expect operators to keep
  `.env` and bot tokens secret.
- **DoS against the broker.** Tansu/Kafka tuning, partition limits,
  and consumer-lag handling are an ops concern, not a security one for
  calfcord's purposes.

## 3. Deployment patterns

Four patterns. The default (§ 3.1) is the most filesystem-isolated;
§ 3.2 and § 3.3 progressively widen what the tools can reach. Pick the
one whose trust model matches your deployment.

### 3.1 Trusted single-tenant (default)

**Best for:** Solo dev, small team, internal Discord server where every
agent author, every operator, and every Discord user is trusted.

This is what `docker compose up` gives you out of the box: filesystem
tools are confined to the shared `./workspace` scratch dir (§ 1), and all
agents share it with no per-agent isolation. No extra config needed.

Sample threat realistic for this pattern: agent A's `terminal` tool, acting
on a careless prompt, deletes or overwrites a file another agent left in
`./workspace` ("clean up old scratch files" → `rm -rf /workspace/*`).
Because there is no boundary between agents *inside* the workspace, this
is a recoverable mistake (it's a scratch dir), not a security incident —
but it illustrates the absence of any inter-agent boundary. With the
default mount the blast radius stops at `./workspace`: agent A cannot
reach `agents/scribe.md` or `src/` to clobber them.

### 3.2 Wider workspace (give agents the source tree)

**Best for:** A coding-assistant deployment where you *want* agents to
read and edit the repository — agents that triage code, open PRs, or
operate on `agents/*.md` themselves.

The default mount (§ 1) is a scratch dir, so the source tree is
off-limits. To widen it, drop a `compose.override.yml` next to
`docker-compose.yml`:

```yaml
services:
  tools:
    volumes:
      - .:/workspace        # mount the whole project root
```

Compose merges this on top of the base file. Now any agent with `terminal`,
`read_file`, `write_file`, or `patch` can:

- Read `agents/*.md` (every agent's identity, system prompt, and tool list).
- Read `src/` (the application source — including any secrets a
  contributor accidentally committed).
- Read a root-level `.env` (Docker bind-mounts follow symlinks too).
- Edit any of the above, and (via `terminal` / `execute_code`) run any
  binary on `$PATH` or arbitrary Python.

That is a deliberate trade: full repo access in exchange for the
exposure above. Only widen the mount on a deployment where you trust
every agent definition and every Discord user with `@mention` access.
For something in between — e.g. exactly one subdirectory — point the
override at that path (`- ./some/subdir:/workspace`) instead of `.`.

### 3.3 Tools native, broker + others in Docker

**Best for:** A production deployment where you want the bridge / agent
lifecycles managed by Docker but you want the tools surface to
have explicit host access (e.g. so agents can drive your laptop's git
checkouts, your real `state/workspace`, or your real `.ssh`).

Run `calfkit-tools` natively on the host (or in a dedicated VM):

```bash
uv run calfkit-tools
```

Keep the rest of the stack in compose:

```bash
docker compose up -d tansu bridge agent
```

Now the tools process runs as your shell user with the full filesystem
permissions of that user — same blast radius as Claude Code on the
same machine. Isolate by running the tools process under a dedicated
unprivileged user account, in a VM, or under a container runtime that
applies stricter syscall filtering than the default
calfkit-tools image.

A **native install** lands squarely in this model. The `calfcord` shim
defaults `CALFCORD_WORKSPACE_DIR` to **the directory the workspace
(`calfcord start`) was launched from** (not a hidden dir), so opening
the workspace inside a project dir gives every agent the same blast
radius as Claude Code on that machine over that project. Open the
workspace from the narrowest directory the agents actually need, and
keep the deployment off public Discord (§ 3.4) — anyone who can
`@mention` an agent drives that surface.

Note that this is *less* isolated than 3.1 in absolute terms (the
process can now reach `$HOME`, `/etc`, and so on), but *more*
predictable in operational terms — the host user's permissions are
exactly the boundary.

**`calfcord init` configures the agent with *all* tools selected by
default.** Its tools step pre-checks every tool — including `terminal`,
`execute_code`, `write_file`, `patch`, and the web tools — so a
freshly-configured agent has the full terminal + code-execution +
file-write + web reach described above,
running in the directory the workspace (`calfcord start`) was launched
from and drivable by anyone who can `@mention` it. The wizard prints a
caution when those tools are kept. Deselect what the agent doesn't need
(or trim later with `calfcord agent tools`), and keep the deployment on
a trusted/private server, never public Discord (§ 3.4). (The
installer-seeded `assistant.md` is text-only until you run
`calfcord init`.)

### 3.4 Don't expose calfcord to public Discord servers

**Best for:** Any deployment where you don't want every Discord user
who shares a guild with your bot to be able to invoke its tools.

Anyone who can `@mention` an agent can drive its tool surface. There is
no per-user rate limit, no per-user permission gate, no command
allowlist beyond what each tool's own validation enforces. **Do not
invite the bot to public guilds.** Restrict the bot's guild list to
servers whose members you trust to the same degree you trust your
agent definitions.

The Discord developer portal lets you mark a bot as private (uninvitable
by random users); set that bit. Audit `Bot → OAuth2 → Generated URLs`
to confirm the invite URL only grants what you intend.

## 4. Tool authorship hygiene

The single most important rule, repeated from
`docs/authoring-tools.md`: **validate every LLM-supplied argument at
the tool boundary, regardless of type annotation.** Type annotations
give you JSON-encoded shape; they give you nothing about content.

The concrete checks below are the operator-facing extract of the
authoring guide:

- **Never `subprocess.run(..., shell=True)` with LLM-supplied strings.**
  Use list-form argv. The shipped `terminal` and `execute_code` tools are
  where arbitrary execution is already explicit; new tools should not
  invent their own pipeline. If you find yourself reaching for
  `subprocess`, ask first whether the workflow should be a command the
  LLM composes through `terminal`.
- **URL allowlists for fetch-style tools.** Reject `file://`,
  `gopher://`, and other non-HTTP schemes if your tool only intends to
  fetch web content. Validate the hostname if your tool only intends to
  talk to one upstream.
- **Filesystem-path validation.** If your tool only operates on a fixed
  subdirectory, reject paths that escape it via `..` or absolute
  prefixes. The shipped file tools (`read_file` / `write_file` / `patch`)
  intentionally do not bound the workspace — that's the trusted-workspace
  contract. A more restrictive tool should do better.
- **Don't write secrets to the workspace.** Any agent on the
  deployment can read what's in the shared workspace (`/workspace`). If
  your tool needs a secret at runtime, pull it from the environment and
  don't echo it into a return value or a written file.
- **Validate templated strings.** SQL, shell, format strings — anything
  forwarded into a downstream interpreter needs the same hygiene you'd
  apply on a public web endpoint.

The full authoring rules — including the `"error: "` vs.
`RuntimeError` boundary and the lazy-init pattern — are in
`docs/authoring-tools.md` § Security model and § Error handling
convention.

## 5. Operator hygiene

The boring operational hygiene that doesn't fit into "trust model" but
matters in practice.

### 5.1 Secrets

- **Keep `.env` out of git.** The shipped `.gitignore` excludes it;
  don't override that.
- **Don't commit `agents/*.md` files that hard-code secrets in the
  system prompt.** Anyone with repo or deployment-host access can read
  them, and if you widen the tools workspace to include `agents/`
  (§ 3.2), peer agents can `read_file` them directly.
- **Rotate the Discord bot token on suspected compromise.** Discord's
  bot token is the single secret that, if leaked, gives an attacker
  full control of the bot's actions in every guild it's in. Rotate
  via the Discord developer portal; update `DISCORD_BOT_TOKEN` in
  `.env`; restart the substrate so the bridge picks up the new token
  (`calfcord stop` then `calfcord start`).
- **Rotate provider API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
  on suspected compromise.** These have billing implications.
- **Keep MCP credentials in `config/.env`, not in `mcp.json`.** MCP server
  entries may carry literal secrets (the file is `0600`, like `.env`), but
  prefer a `$VAR` reference in `mcp.json` whose value lives in `config/.env`
  so the registry file holds no secret. The expanded secret values are seen
  **only** by the `calfkit-mcp` server processes (the `calfcord mcp` CLI
  reads the file but never expands the references) — agents resolve MCP tools from the broker's capability
  view, never the config — so on a distributed deploy the credentials live on
  the MCP host alone and never reach agent hosts. See
  [`mcp-tools.md`](./mcp-tools.md).

### 5.2 Discord scoping

- **Use guild-scoped slash-command sync** for production deployments.
  Setting `DISCORD_GUILD_ID` in `.env` makes the bridge register slash
  commands against one guild instead of globally; see
  `src/calfcord/discord/settings.py`. Global slash sync
  has a longer propagation delay and exposes the commands in every
  guild the bot is in, including ones where you didn't intend to.
- **There is no per-agent channel allowlist any more.** Name-addressing
  (the calfkit-012 migration) removed per-agent channel subscriptions and
  the `state/agents/<name>.json` seed, so an agent replies to any `@mention`
  in any channel the bot can see. The controls that remain are keeping the
  bot off public guilds (§ 3.4) and — on a split deploy — broker access
  control (§ 7.2), since anyone who can write to the broker can invoke an
  agent directly on `agent.<name>.private.input`.

### 5.3 Process / log hygiene

- **Don't run the agent process as root** even inside a container. The
  shipped Dockerfile uses a non-root user; if you replace it, preserve
  that.
- **Capture logs to a host-visible path.** Compose's default `json-file`
  logging works; rotate it or ship to a log aggregator. Tool errors and
  the bridge's correlation-id-tagged invocation logs are the primary
  forensics trail.
- **Audit `agents/*.md` changes.** Every agent definition lives in the
  repo. PR-review the changes. If you don't want code-review gates on
  agent files, at minimum read the diff before deploying.

### 5.4 Backups

- **The bridge's SQLite store** (under `./state`, default
  `state/transcripts.sqlite3`) holds step transcripts and the per-agent
  thinking-effort overrides (`/thinking-effort`). It is the one piece of
  calfcord-managed state worth backing up; agents themselves keep no
  per-agent on-disk state now that name-addressing removed channel
  subscriptions (`state/agents/*.json` is gone).
- **No broker volume to back up by default.** The shipped `tansu` broker
  uses ephemeral memory storage, so there is no persisted Kafka data —
  topics/messages reset on broker restart, and calfcord re-creates the
  topics it needs on startup. `docker compose down` (with or without
  `-v`) loses no broker state because none is persisted. If you switch
  the broker to a durable store (libsql/SQLite or postgres via
  `STORAGE_ENGINE`), back that store up and flag it in your deploy
  runbook.

## 6. Reporting a vulnerability

If you find a security issue in calfcord — sandbox escape from a
shipped tool, an injection vector through the bridge, anything that
breaks the trust model above in a way the operator couldn't reasonably
predict — please report it privately via the process documented in
`SECURITY.md` at the repo root. Don't open a public issue first; the
GitHub Security Advisory path keeps the report off the public tracker
until there's a fix to disclose.

## 7. Distributed deployments: securing the broker

The threat model above assumes a single-host deployment where the
broker only listens on localhost. Once you split calfcord across hosts
— see `docs/distributed-deployment.md` for the operational walkthrough
— the broker becomes the network perimeter, and the default
unauthenticated Tansu setup is no longer adequate.

The shipped `docker-compose.yml` starts Tansu **unauthenticated and
without TLS**. That is fine when the broker only listens on `localhost`
or only on a trusted overlay's interface. It is NOT fine when the
broker is reachable over any network the operator does not control.

### 7.1 Two paths for cross-network deployments

**Trusted overlay (recommended).** Run Tailscale, WireGuard, or an
equivalent overlay between every host. Bind the broker only to the
overlay's interface so it is unreachable from anywhere else. The
broker stays unauthenticated; the overlay is the perimeter. Win: no
auth config, no cert rotation, no extra moving parts.

**Client auth + TLS.** Broker exposed publicly, gated by auth. Tansu
offers `--authentication` (require client auth), TLS via `--cert` /
`--key`, and `tansu user create` for users. Production-grade SASL/ACL
hardening with Tansu is still maturing, so follow
[Tansu's docs](https://docs.tansu.io/) and the
[upstream repo](https://github.com/tansu-io/tansu) for the current setup.
Note that calfcord's `runner.py` currently only forwards
`CALF_HOST_URL`; the standard aiokafka SASL/SSL env vars
(`KAFKA_SASL_MECHANISM`, etc.) need to be plumbed through at the
calfkit level for now. Treat this as a follow-up if you need it; it
does not affect the trusted-overlay path.

### 7.2 What broker compromise looks like

Anyone who can publish to `tool.<name>.input` on the broker can
**invoke that tool with arbitrary arguments**. The tools-image host
blindly executes whatever calls come in — the trusted-workspace model
from § 1 assumes the broker is also trusted. There is no per-call
signing, no per-caller allowlist, no replay protection beyond what
Kafka inherently provides.

The calfkit-012 migration widened this surface in two ways, both mitigated
only by broker access control:

- **Any broker-writer can invoke any agent.** Agents are now name-addressed:
  a message published to `agent.<name>.private.input` reaches that agent's
  LLM directly. The old `slash_target` addressing gate — which only let a
  message through once the bridge had marked it as addressed to that agent —
  is gone, so that in-app defense-in-depth check no longer stands between a
  broker-writer and an agent.
- **Roster poisoning via forged `AgentCard`s.** The bridge and CLI build
  their roster from calfkit's agent mesh (the `AgentCard`s advertised on
  `calf.agents`). A broker-writer can advertise a forged `AgentCard`,
  injecting a fake agent or shadowing a real name in the roster. Only broker
  write-access control keeps the mesh honest.

The implication: for distributed deployments, **broker auth IS the
perimeter** — for tool invocation, agent invocation, and roster integrity
alike. Rotate broker credentials like you rotate the Discord bot token
(§ 5.1). A leaked broker password gives the holder the same blast radius as
a leaked bot token gives them inside Discord.

For the operational mechanics of splitting calfcord across hosts —
including the network prereq and per-host tool narrowing via
`CALFCORD_TOOLS_INCLUDE` — see
[`docs/distributed-deployment.md`](./distributed-deployment.md).
