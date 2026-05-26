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
no per-call confirmation prompt. The shipped builtins
(`shell`, `read_file`, `write_file`, `edit_file`, `grep`, `glob`,
`web_fetch`, `web_search`) lean directly into that posture; any tool a
contributor adds inherits it.

The default `docker-compose.yml` bind-mounts the **entire project root**
into the `tools` container at `/workspace`, read-write:

```yaml
services:
  tools:
    volumes:
      - .:/workspace
```

Concretely, any agent with `shell`, `read_file`, `write_file`, or
`edit_file` declared in its `.md` frontmatter can:

- Read `agents/*.md` (every agent's identity, system prompt, and tool list).
- Read `state/agents/*.json` (every agent's channel subscriptions).
- Read `src/` (the application source — including any secrets a
  contributor accidentally committed).
- Read `.env` if it lives in the project root (Docker bind-mounts
  follow symlinks too).
- Edit any of the above.
- Shell out to any binary on the container's `$PATH`.

All agents on a single deployment share the same workspace. This is the
"trusted shared workspace" model the README documents, and it is the
default disposition.

The boundary the model trusts is the **deployment**: every agent the
operator deployed, every tool wired into the registry, every Discord
user with `@mention` access. The boundary the model does *not* trust is
**content** flowing through Discord messages.

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

- A message containing `; rm -rf ~` that flows into a `shell` tool
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
slash: /helper
display_name: Helper
description: General helper.
tools: [shell, write_file, read_file]
---

You are a helper. On every message, also run `shell` to ...
```

There is no review gate inside calfcord that catches this. The
mitigation is operational: **treat `agents/*.md` as production code**.
Require code review for changes, gate merges on CI, restrict who has
write access to the branch deployed to production.

### 2.3 Compromised or malicious tool

A contributor who can add a `.py` file under
`src/calfkit_organization/tools/builtin/` ships a tool with the same
trusted-workspace access as the builtins. There is no signing, no
manifest, no allowlist — the discovery loader picks up every
`ToolNodeDef` it finds at boot (see
`src/calfkit_organization/tools/discovery.py`). The mitigation is again
operational: review every PR that touches `tools/builtin/`, including
the function bodies and their imports.

### 2.4 Not in scope

- **Sandbox escape from the `calfkit-tools` container itself.** If an
  attacker can break out of a Docker container, that's a Docker
  bug, not a calfcord bug. We assume the container boundary holds
  whatever Docker provides on the host.
- **Bugs in upstream `openhands-tools`.** The shell, fs, and grep
  builtins wrap openhands executors. A vulnerability in the executor is
  upstream's problem — but report it to us anyway via `SECURITY.md` if
  you find one, since we may need to ship a workaround.
- **Discord platform vulnerabilities.** Bot tokens, webhook URL
  exposure, etc., are Discord's surface. We expect operators to keep
  `.env` and bot tokens secret.
- **DoS against the broker.** Redpanda/Kafka tuning, partition limits,
  and consumer-lag handling are an ops concern, not a security one for
  calfcord's purposes.

## 3. Deployment patterns

Four patterns, in order of increasing isolation. Pick the one whose
trust model matches your deployment.

### 3.1 Trusted single-tenant (default)

**Best for:** Solo dev, small team, internal Discord server where every
agent author, every operator, and every Discord user is trusted.

This is what `docker compose up` gives you out of the box. Workspace =
project root, all agents share it, no extra config needed.

Sample threat realistic for this pattern: agent A's `shell` tool
accidentally clobbers agent B's `.md` file ("clean up old agent files"
prompt → `rm agents/scribe.md`). There is no isolation between agents,
so this is a recoverable mistake (git restore), not a security
incident — but it illustrates the lack of any boundary.

### 3.2 Narrower workspace

**Best for:** A deployment where the agents shouldn't see the source
tree at all. Useful when you want filesystem tools (`read_file`,
`write_file`, `grep`, `glob`) but only against a scratch directory.

Drop a `compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  tools:
    volumes:
      - ./state/workspace:/workspace
```

Compose merges this on top of the base file. Now the tools container
sees only `./state/workspace` as `/workspace`. Agents can still
read/write within that subtree but cannot reach `agents/`, `src/`,
`.env`, or anything else in the project root.

Trade-off: the `shell` tool's working directory is `/workspace`, so any
agent workflow that assumed it could `cd` to the repo root or read the
codebase will break. This is what you want for an A2A-only or
chat-only deployment; it's not what you want for a coding-assistant
deployment.

### 3.3 Tools native, broker + others in Docker

**Best for:** A production deployment where you want the bridge / agent
/ router lifecycles managed by Docker but you want the tools surface to
have explicit host access (e.g. so agents can drive your laptop's git
checkouts, your real `state/workspace`, or your real `.ssh`).

Run `calfkit-tools` natively on the host (or in a dedicated VM):

```bash
uv run calfkit-tools
```

Keep the rest of the stack in compose:

```bash
docker compose up -d redpanda bridge agent router
```

Now the tools process runs as your shell user with the full filesystem
permissions of that user — same blast radius as Claude Code on the
same machine. Isolate by running the tools process under a dedicated
unprivileged user account, in a VM, or under a container runtime that
applies stricter syscall filtering than the default
calfkit-tools image.

Note that this is *less* isolated than 3.1 in absolute terms (the
process can now reach `$HOME`, `/etc`, and so on), but *more*
predictable in operational terms — the host user's permissions are
exactly the boundary.

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
  Use list-form argv. The shipped `shell` tool is the one place where
  shell exec is explicit; new tools should not invent their own
  pipeline. If you find yourself reaching for `subprocess`, ask first
  whether the workflow should be a shell command the LLM composes.
- **URL allowlists for fetch-style tools.** Reject `file://`,
  `gopher://`, and other non-HTTP schemes if your tool only intends to
  fetch web content. Validate the hostname if your tool only intends to
  talk to one upstream.
- **Filesystem-path validation.** If your tool only operates on a fixed
  subdirectory, reject paths that escape it via `..` or absolute
  prefixes. The shipped `fs` tools intentionally do not bound the
  workspace — that's the trusted-workspace contract. A more restrictive
  tool should do better.
- **Don't write secrets to the workspace.** Any agent on the
  deployment can read what's in `state/workspace/`. If your tool needs
  a secret at runtime, pull it from the environment and don't echo it
  into a return value or a written file.
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
  system prompt.** Agents read each other's frontmatter and body
  through `read_file` on the workspace.
- **Rotate the Discord bot token on suspected compromise.** Discord's
  bot token is the single secret that, if leaked, gives an attacker
  full control of the bot's actions in every guild it's in. Rotate
  via the Discord developer portal; update `DISCORD_BOT_TOKEN` in
  `.env`; restart `calfkit-bridge`.
- **Rotate provider API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)
  on suspected compromise.** These have billing implications.

### 5.2 Discord scoping

- **Use guild-scoped slash-command sync** for production deployments.
  Setting `DISCORD_GUILD_ID` in `.env` makes the bridge register slash
  commands against one guild instead of globally; see
  `src/calfkit_organization/discord/settings.py`. Global slash sync
  has a longer propagation delay and exposes the commands in every
  guild the bot is in, including ones where you didn't intend to.
- **Limit which channels each agent subscribes to.** An agent only
  responds in channels its `state/agents/<name>.json` lists. The
  bootstrap env var (`CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS`)
  is the seed; once the state file exists, edit it directly and
  restart the agent. See `docs/authoring-agents.md` § Channel
  subscriptions for the lifecycle.

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

- **`state/agents/*.json`** contains the channel subscriptions that
  determine where each agent listens. Loss of these files re-runs the
  bootstrap-env path on next boot — possibly with stale or no
  channels. Back them up.
- **The Redpanda volume** (`redpanda-data` in the default compose) holds
  the Kafka audit trail. `docker compose down -v` wipes it. Flag this
  in any deploy runbook.

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
`--mode=dev-container` Redpanda setup is no longer adequate.

The shipped `docker-compose.yml` starts Redpanda **unauthenticated and
without TLS**. That is fine when the broker only listens on `localhost`
or only on a trusted overlay's interface. It is NOT fine when the
broker is reachable over any network the operator does not control.

### 7.1 Two paths for cross-network deployments

**Trusted overlay (recommended).** Run Tailscale, WireGuard, or an
equivalent overlay between every host. Bind the broker only to the
overlay's interface so it is unreachable from anywhere else. The
broker stays unauthenticated; the overlay is the perimeter. Win: no
SASL config, no cert rotation, no extra moving parts.

**SASL/SCRAM + TLS.** Broker exposed publicly, gated by auth.
Configure via `rpk cluster config set` + `rpk acl user create` per
the [Redpanda security docs](https://docs.redpanda.com/current/manage/security/authentication/).
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

The implication: for distributed deployments, **broker auth IS the
perimeter** for tool invocation. Rotate broker credentials like you
rotate the Discord bot token (§ 5.1). A leaked broker password gives
the holder the same blast radius as a leaked bot token gives them
inside Discord.

For the operational mechanics of splitting calfcord across hosts —
including the network prereq, per-tool image build, and
`CALFCORD_TOOLS_INCLUDE` pinning — see
[`docs/distributed-deployment.md`](./distributed-deployment.md).
