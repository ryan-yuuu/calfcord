# Onboarding & Process-Lifecycle Redesign

**Status:** Finalized for review — no code written yet.
**Scope:** Replace the current `init` → `doctor` → run-five-`calfkit-*`-processes onboarding with a
guided, resumable setup that lands an agent replying in Discord, backed by a real process supervisor so
the substrate runs detached and teammates join a live organization on demand.
**SDK baseline:** **calfkit `0.6.0`** (bumped from 0.5.1 mid-implementation). 0.6.0 fixes the #180 Tansu
provisioning hang (connect-time pre-start hook auto-provisions the reply topic) and closes the
Worker-lifecycle gaps; the reply-topic workaround was removed and the runners simplified accordingly (see §10
and `tests/integration/test_broker_startup_provisioning.py`). No embedded/in-memory broker — Tansu stays an
external binary, so the §13 substrate design is unchanged.

---

## 1. Context & goals

The product's defining strength — *everything is distributed and independently deployable over a shared
broker* — is also onboarding's biggest obstacle. A first-time user does not care that it is Kafka-backed
microservices; they want **one agent to reply in their Discord**. The design decouples the *onboarding UX*
from the *deployment topology*: onboarding presents calfcord as one local "organization," and the
distributed nature is a deliberate **graduation**, made true by the invariant that *the same config and the
same commands work on one host or twenty — graduating is a deployment change, never a rewrite.*

Four altitudes, each with its own payoff:

| Altitude | Goal | Win |
|---|---|---|
| 1 — **Launch** | One agent live in *your* Discord | "It replied in #general!" |
| 2 — **Make it yours** | Personalize agents, tools, routing | "I built a team" |
| 3 — **Graduate** | Multi-host, external broker, Docker/systemd/K8s | "It's in prod, split across boxes" |

**Primary success metric:** time-to-first-reply. Everything below is in service of shrinking it while
*teaching* the user the mental model as they go.

**Design principles** (and where each shows up):
- *Time-to-first-value above all* — default agent works with zero creative input; one continuous guided flow.
- *Progressive disclosure* — never show the full command catalog or env-var list up front.
- *Never ask for what you can discover* — no raw Discord IDs; list guilds/channels and pick.
- *Validate at the point of entry* — verify the bot token the instant it is pasted.
- *Clean layers* — the substrate is the substrate; nothing runs that the user did not explicitly start.
- *Teach just-in-time* — one plain-language sentence at the moment each concept first matters.
- *Resumable & safe* — checkpointed; re-running never destroys anything.
- *Verify the win* — detect the first real reply, don't just assert readiness.

---

## 2. Target command surface

The single user-facing command is `calfcord` (the installed shim). Two layers, kept deliberately distinct:

**Substrate (the always-on background office):**
```
calfcord start            # broker + bridge ONLY, detached, fail-fast readiness gate, confirm-or-die
calfcord stop             # close the office (stops everything it manages)
calfcord status [--watch] # the org board: substrate health + which agents/tools/router are online
calfcord logs -f [target] # tail unified or per-component logs
calfcord doctor [--fix]   # static config checks + runtime daemon-health checks
```

**Roster (teammates that clock in/out of the running office):**
```
calfcord agent start <name>     # a teammate joins the LIVE org (idempotent by name)
calfcord agent stop <name>      # ...leaves
calfcord agent restart <name>   # reload after editing its .md
calfcord agent ps               # what's RUNNING now (≠ `agent list`, which lists DEFINED agents)
calfcord router start|stop      # receptionist (router) on/off, live (roster member; `start` needs config)
calfcord tools start|stop       # tools host on/off, live
calfcord mcp start|stop         # MCP host on/off, live (roster member; holds MCP secrets — see §12.3)
```

> MCP is a **roster** member, not substrate (there are effectively five process types, not four). The router
> holds an LLM connection like an agent, so it has a **first-class, editable config surface** (`router
> show|set|edit`, below) — not a one-shot wizard. `router start` fails fast if unconfigured. See §12.0.

**Setup & authoring (unchanged in spirit, refined):**
```
calfcord init                   # one continuous, resumable guided session → ends LIVE with a first reply
calfcord agent <list|new|edit|show|set|rename|delete|tools>
calfcord router <show|set|edit>  # configure the router's LLM (provider/model) — editable anytime, like an agent
calfcord mcp <add|codegen>
calfcord auth codex <...>
calfcord self <version|status|update|rollback|set-broker>
```

**Graduation (advanced; later in the journey):**
```
calfcord explain topology       # one screen: how the pieces split, and why
calfcord package <agents|tools> # build slim per-role images (exists today)
calfcord deploy                 # generate systemd / Docker / K8s manifests (advanced tier)
```

### Naming decision: `agent list` vs `agent ps`
- `agent list` → agents **defined** on disk (the `.md` files). *(exists today)*
- `agent ps` → agents **running** right now. *(new — Docker's vocabulary, to avoid overloading "list")*

### The minimum path to a live agent (two honest commands)
```
calfcord start                  # office opens (substrate only)
calfcord agent start assistant  # teammate clocks in → replies in Discord
```
`start` deliberately does **not** auto-start any roster — "nothing runs that you didn't start" is a trust
property. The one place that runs the whole sequence for a newcomer is the guided `init` flow, which walks
both layers *visibly* so the user learns the substrate↔roster split by doing it.

---

## 3. Architecture decisions

### 3.1 Supervisor: Process Compose (single Go binary)
We adopt **[Process Compose](https://f1bonacc1.github.io/process-compose)** as the dev/single-host process
supervisor rather than hand-rolling lifecycle management or using a Python-only/Unix-only supervisor.

Why it is the highest-leverage pick:
- **Cross-platform single static binary** (Linux/macOS/Windows) — fits "widely available, lightweight,"
  and mirrors the existing **Tansu broker bootstrap pattern** (`scripts/install.sh: ensure_tansu()`,
  `BIN_DIR=$CALFCORD_HOME/bin`). We bootstrap a second pinned binary the same way — and it keeps Python
  deps *out* of the agent path (aligns with the deployment-decoupling invariants in `CLAUDE.md`).
- **Absorbs things we would otherwise build:** dependency ordering (`depends_on`), health-gated readiness
  (`readiness_probe` + `process_healthy` conditions), autorestart (`availability.restart`), per-process log
  capture, and a clean **REST API** (`GET /processes`, `GET /process/{name}/state`,
  `GET /process/{names}/logs`, `POST /process/{name}/start|stop|restart`).
- **Autorestart:** `_worker_runtime.run_worker_until_signal()` forces a *non-zero exit on a clean,
  signal-less exit* — and all four roster runners (agents, tools, router, mcp) use it, so an uncommanded
  exit (crash *or* clean signal-less return) is non-zero and `restart: on_failure` recovers it; an
  operator-commanded `stop` is suppressed from restart by Process Compose regardless of exit code. The
  **bridge** does NOT use it — it owns its own signals and exits 0 on a clean shutdown, so `on_failure`
  would never fire to recover it from an uncommanded clean return → the substrate (broker + bridge) uses
  `restart: always`, the whole roster (agents included) uses `restart: on_failure`. The live contract is
  the `supervisor/compose.py` module docstring. See **§12.0 / §12.1 / §13.2**.

The Process Compose YAML is **derived state** generated by calfcord from the `agents/*.md` files and config;
the user never edits it. Our CLI is a thin veneer over the REST API.

**Conservative fallback (documented, not chosen):** supervisord (pip, Unix-only, XML-RPC, INI-static). We
lose Windows, the REST API, and built-in health-gating. Rejected on the stated criteria.

**Heavyweight tiers (advanced/Altitude 3):** Docker/Compose, systemd/launchd units, K8s — generated by
`calfcord deploy`, not the day-one path.

### 3.2 Layer model maps cleanly onto Process Compose
Process Compose is "processes are declared, then started," which fits the substrate/roster split:
- Generated YAML declares **substrate** (broker, bridge) with `autostart: true` and health gates.
- Every **defined agent**, plus tools and router, are declared with `disabled: true` (present but not
  started). `calfcord agent start X` = `POST /process/X/start`.
- `calfcord start` brings up only the substrate; roster entries wait for an explicit start.

### 3.3 Readiness & health: one heartbeat pattern, two consumers
The bridge has **no health endpoint today** (confirmed). We introduce one small shared affordance:

- **Heartbeat files** at `$CALFCORD_HOME/state/health/<component>.json` (`{pid, started_at, last_beat,
  status, identity}`), written by each long-lived process and refreshed every few seconds. A tiny shared
  helper module (`calfcord/health/`).
  - The **bridge writes its first beat only on Discord `on_ready`** (`bridge/gateway.py:_on_ready`, the
    existing `"gateway ready as %s"` signal) — so "bridge healthy" *means* "connected to Discord."
  - The **broker** is probed by TCP on `CALF_HOST_URL`'s port (reuse `doctor._tcp_reachable`).
- **Process Compose readiness probes** call `calfcord _healthcheck <component>` (an internal exec probe that
  reads the heartbeat/TCP), so `depends_on: { broker: process_healthy }` and the `start` readiness gate are
  driven by the same signal.
- **`status`** (frequent, glanceable) reads heartbeats — cheap.
- **`doctor`** (deliberate, authoritative) additionally does an **end-to-end control-plane probe** (reuse
  the existing discovery-ping: `control_plane` publishes `DiscoveryPingOp` on `bridge.discovery`; agents
  answer with `AgentStateEvent(cause="discovery_response")` on `agent.state`). This proves broker+bridge
  actually function together and is the *same* mechanism that will verify a remote substrate at Altitude 3.

### 3.4 Logical roster — reconstructed from the `agent.state` topic (NOT from the bridge's memory)
The bridge's `AgentRegistry` is in-memory **inside the bridge process**; the CLI runs elsewhere (possibly a
different host) with no IPC to it — so it can be neither read as a local file nor scraped from the bridge. But
the registry is a *projection of the `agent.state` Kafka topic* (num_partitions=1, no compaction), and Tansu's
consumer groups work through the standard Kafka client. So `agent ps` / the doctor deep probe reconstruct the
**live** roster via calfkit's Kafka client: **publish a `bridge.discovery` ping, collect `agent.state`
`discovery_response` events over a bounded window** (own consumer group). Only currently-running agents
respond → true liveness, no stale-entry problem. Host-agnostic and broker-agnostic (works on bring-your-own
Kafka, unlike a tansu-CLI path). This is a small **new** one-shot collector — reuse covers only
`publish_discovery_ping` + the `AgentStateEvent` schema, not the registry itself. See **§12.0 / §12.2**.

`agent ps` then unions this logical view (global) with Process Compose `GET /processes` (physical,
host-local) — flagging the "process up but never joined" case, while keeping the host-local caveat explicit
(§12.5).

### 3.5 Idempotency & duplicate prevention (free from the model)
- `agent start X` first queries the **global live roster via the control-plane probe**
  (:func:`control_plane.probe.probe_live_roster`, which reads `agent.state` over the broker org-wide). If
  `X` is already live **anywhere — including another host** — it does NOT start a duplicate and prints a
  clear message ("agent 'X' is already running in the organization"). Otherwise it proceeds to
  `POST /process/start/{X}` against the local supervisor.
- **The guard is CLI-side only — no bridge-side changes.** Because it uses the broker-wide probe (not the
  local Process Compose process list), it is **distributed-correct**: a duplicate on a second host is caught
  without the bridge rejecting anything. The bridge keeps treating a re-registration as a benign re-announce.
- **Accepted limitation:** the probe is point-in-time, so two *simultaneous* `agent start X` on different
  hosts could both see nothing and both start (a TOCTOU race). Catching that would need bridge-side
  enforcement, which is deferred out of scope. The guard covers the common case (X already running, you try
  to start another).

---

## 4. Component design

### 4.1 Process Compose integration (`calfcord/supervisor/`)
- **Binary bootstrap** — extend `scripts/install.sh` with `ensure_process_compose()` mirroring
  `ensure_tansu()`: OS/arch resolution, pinned `CALFCORD_PROCESS_COMPOSE_VERSION` (default a known-good
  tag), download to `$BIN_DIR/process-compose`, `chmod +x`, clear macOS quarantine, best-effort (a failure
  warns and points at Docker, like Tansu does).
- **YAML generator** (`supervisor/compose.py`) — render `process-compose.yaml` into
  `$CALFCORD_HOME/state/` from: the agents dir, config/.env, router/tools settings. Substrate `autostart`,
  roster `disabled`, `depends_on` health gates, `availability.restart: on_failure`, log files under
  `state/logs/<component>.log`, readiness probes calling `calfcord _healthcheck`.
- **REST client** (`supervisor/client.py`) — thin wrapper over the Process Compose REST API used by the CLI
  veneer (list/state/logs/start/stop/restart) plus the readiness-poll used by `calfcord start`.
- **Daemon launch** — `calfcord start` execs `process-compose up --detached` (project = generated YAML),
  then polls `/process/{bridge,broker}/state` until healthy or timeout; on success prints the confirmation
  banner and exits 0; on timeout/failure tears down and exits non-zero with the specific failure.

### 4.2 Health subsystem (`calfcord/health/`)
- `heartbeat.py` — `write_beat(component, identity)` + a periodic refresher hook each runner installs;
  `read_beat(component)`, `is_fresh(beat, ttl)`.
- `calfcord _healthcheck <component>` internal subcommand — exit 0/1 for Process Compose exec probes.
- Wire the bridge to write its first beat in `_on_ready` (gateway.py) and refresh on a timer; wire
  agent/router/tools/mcp runners to beat after their Worker is running.

### 4.3 CLI veneer (`calfcord/cli/`)
New subcommands dispatched by `calfcord-cli` / the shim:
- `start`, `stop`, `status [--watch]`, `logs [-f] [target]`
- `agent start|stop|restart|ps`
- `router show|set|edit|start|stop`, `tools start|stop`, `mcp start|stop`
- internal: `_healthcheck`, and the `start` readiness-gate logic.

`router show|set|edit` reuse the agent config seams (`_providers.configure_provider`, `_fields`, `_envfile`
upsert) so the router's provider/model is validated and editable exactly like an agent's; config persists in
`.env` (`CALFKIT_ROUTER_PROVIDER`/`_MODEL`). `router start` refuses to launch an unconfigured router.
`status`/`ps` render the heartbeat + REST state; `--watch` re-renders the org board on an interval (the
"dashboard" view, decoupled from `start`).

### 4.4 doctor extension (`calfcord/cli/doctor.py`)
Keep the 5 existing static checks (config, broker, token, app id, agents). Add a **runtime section** that
runs only when the daemon is up (detected via heartbeat/PID):
- daemon alive (not a zombie) via heartbeat freshness,
- broker accepting connections (existing `_tcp_reachable`),
- bridge connected (bridge heartbeat exists & fresh, includes bot identity),
- **deep probe:** control-plane discovery ping → confirm bridge answers and list registered agents,
- **drift:** agents running (Process Compose) vs. agents registered (bridge) — flag mismatches.
`--fix` auto-repairs the safe ones (free port selection, missing dirs). Findings are framed as fixes.

### 4.5 Discord auto-discovery (`calfcord/cli/discord_discovery.py`)
- On token paste: call `GET /users/@me`, echo `✓ Connected as <bot> (id …)` — instant validation
  (extends the existing `doctor._discord_username`).
- Generate the invite URL with the correct permission bitmask + intents reminder.
- **Poll** `GET /users/@me/guilds` until the bot joins a guild, then **discover** guilds and channels and
  present pick-lists — replacing the "paste the numeric ID" prompts for `DISCORD_GUILD_ID` /
  `DISCORD_DEFAULT_CHANNEL_ID`. (Channels: `GET /guilds/{id}/channels`, filter to text the bot can see.)

### 4.6 Resumable `init` session (`calfcord/cli/init.py` rework)
- **One continuous guided session** is the happy path; a checkpoint file
  (`$CALFCORD_HOME/state/setup.json`) records completed steps so a crash/close/forced detour resumes
  ("Welcome back — provider and agent done; let's finish Discord") instead of restarting.
- **Discord detour handled by block-and-poll:** print steps + deep links, then wait, polling Discord, and
  **auto-advance** the moment the bot appears — the session stays alive across the browser trip. Ctrl-C
  falls back to the checkpoint.
- **Ends LIVE, not at "now run commands":** the final guided phase orchestrates the two layers *visibly* —
  `start` (substrate, health-gated) → `agent start assistant` → watch for the first reply → 🎉 celebrate —
  teaching the substrate↔roster distinction by performing it.
- Targeted sub-flows remain for power users: `calfcord init provider|discord|agent`, `calfcord agent edit`.

---

## 5. Reuse vs. build-new (grounded)

| Area | Reuse (exists today) | Build new |
|---|---|---|
| Process lifecycle | `_worker_runtime.run_worker_until_signal` (non-zero-exit-forces-restart invariant) — pairs with PC autorestart | Process Compose bootstrap, YAML generator, REST client, `start` readiness gate |
| Roster liveness | Control-plane discovery ping + `AgentStateEvent`/`AgentDepartureEvent` (`control_plane/`), bridge `AgentRegistry` | `agent ps` union (PC state + registry), drift detection |
| Bridge readiness | `bridge/gateway.py:_on_ready` (`"gateway ready as …"`) | heartbeat files + `_healthcheck` exec probe |
| Broker | Tansu bootstrap `ensure_tansu()`; `calfcord broker` shim; `doctor._tcp_reachable` | (none — reused) |
| doctor | 5 static checks (config/broker/token/appid/agents) | runtime/daemon-health section + deep probe + drift |
| Per-agent start | agent runner already supports single-agent (`calfkit-agent <name>`) and many-per-process | per-agent PC entries; idempotent veneer |
| Install layout | `$CALFCORD_HOME/{bin,shims,config,agents,state,current}`, shim env exports | `state/health/`, `state/logs/`, `state/setup.json`, generated `process-compose.yaml` |
| Discord | `DiscordSettings`, `doctor._discord_username` (`/users/@me`) | guild/channel discovery + poll-until-joined |
| init | `init.resolve_paths`, `_envfile` upsert, `agent_create` | resumable checkpoint, block-and-poll, live-finish |

---

## 6. Delta from today's surface

| Today | Becomes |
|---|---|
| 5 foreground `calfcord run <role>` / `calfkit-*` processes in 5 terminals | `calfcord start` (detached substrate) + `calfcord agent start <name>` (roster), supervised |
| `calfcord broker` in its own terminal | broker is a substrate process under `start` (the standalone `calfcord broker` shim stays for advanced/remote use) |
| `init` ends with "run the four processes" text | `init` ends **live** with a first reply |
| `doctor` = static only | `doctor` = static + runtime daemon health |
| no running-state visibility | `calfcord status` / `agent ps` / `logs` |
| paste numeric Discord IDs | discover & pick from menus |

**Backward-compat / deprecation:** keep `calfcord run <role>` and the raw `calfkit-*` entry points working
(they're the Altitude-3 multi-host primitives and what Process Compose execs under the hood). The
shim's `init`-printed `calfcord calfkit-bridge` next-steps text is removed in favor of the live finish.

---

## 7. Implementation phases (sequenced, each independently shippable, TDD)

Per `CLAUDE.md`: implement on a branch/worktree, follow `/test-driven-development`, check completeness with
`/pytest-coverage`, keep Ruff clean on changed files.

**Phase 0 — Spikes (de-risk before committing):**
- Confirm Process Compose: detached `up`, REST readiness polling, exec readiness probes, and the
  **dynamic-add** story (new agent created after `start`). Decide reload-vs-predeclare. *(See Risks.)*
- Pin a known-good Process Compose version; verify macOS/Linux release asset naming for `ensure_*`.

**Phase 1 — Supervisor foundation:**
- `ensure_process_compose()` in installer; `supervisor/compose.py` YAML generator (substrate only first);
  `supervisor/client.py` REST wrapper. Tests: YAML rendering golden tests; client against a stub server.

**Phase 2 — Health & `start`/`stop`:**
- `health/` heartbeat + `_healthcheck`; wire bridge `on_ready` beat + broker TCP probe.
- `calfcord start` (substrate, detached, readiness gate, confirm-or-die) and `calfcord stop`.
  Tests: readiness-gate success/timeout/failure paths; heartbeat freshness.

**Phase 3 — Roster + visibility:**
- Roster entries in YAML (`disabled`); `agent start/stop/restart/ps`, `routing on|off`, `tools start|stop`;
  `status [--watch]`, `logs`. Idempotency + drift via registry union. Tests: idempotent start, ps union,
  drift detection.

**Phase 4 — doctor runtime checks:** extend `doctor` with the runtime section + deep control-plane probe +
`--fix`. Tests: daemon-up vs daemon-down branches; drift; deep-probe success/failure.

**Phase 5 — Discord auto-discovery:** token echo, invite URL, poll-until-joined, guild/channel pick-lists.
Tests: discovery against a mocked Discord API.

**Phase 6 — Resumable `init` finale:** checkpoint + block-and-poll + live finish (start → agent start →
first-reply detection). Tests: resume from each checkpoint; detour poll/auto-advance; first-reply detection.

**Phase 7 — Docs & graduation:** rewrite the README quickstart and the doc set per **§11**;
`calfcord explain topology`; `calfcord deploy` manifest generation (systemd/Docker/K8s) for Altitude 3.
Docs land *here*, last, so they never advertise commands that aren't shipped yet (see §11.5).

---

## 8. Risks & open spikes
1. **Dynamic add of a brand-new agent after `start`.** Process Compose loads its project at `up`.
   Mitigation/decision in Phase 0: predeclare all defined agents as `disabled` (covers the common case,
   since the default agent exists before first `start`); for agents created later, regenerate YAML and
   reload the project (validate the exact reload semantics; fallback is "restart substrate to pick up new
   YAML"). For onboarding this edge never arises.
2. **Process Compose API/flag stability** across versions — pin a version; wrap the REST client behind one
   module so an upgrade is localized; the CLI veneer is the stable seam (could later swap to circus/systemd
   without changing user commands).
3. **Windows** — Process Compose runs, but broker/Discord dev likely route through WSL; don't claim
   Windows-native onboarding without testing. Document WSL/Docker as the Windows path.
4. **Registry has no TTL** — a hard-crashed agent stays "registered" until restart. Acceptable: Process
   Compose process-state is the authoritative liveness; `agent ps` shows the *physical* down state and the
   union flags the stale-registry/down case. Adding registry heartbeat-loss TTL is a separate calfkit-side
   change (the schema even leaves room for `"heartbeat_loss"`), explicitly out of scope.
5. **Readiness probe shape** — confirm whether Process Compose readiness uses `exec` (our `_healthcheck`)
   vs `http_get`; the bridge has no HTTP server, so `exec` is the plan unless we add one.

---

## 9. Testing strategy
- TDD throughout (`/test-driven-development`); aim for full coverage on new modules (`/pytest-coverage`).
- Unit: YAML generation (golden), REST client (stub server), heartbeat freshness, readiness-gate branches,
  idempotent start, ps-union/drift, doctor runtime branches, Discord discovery (mocked API), init
  resume/detour/first-reply.
- Integration: a `start` → `agent start` → simulated state-event → `agent ps`/`status` reflects it, against
  a local broker, with Process Compose driving real processes (gated/marked for CI capability).
- Installer: extend `scripts/tests/test_installer.sh` to cover `ensure_process_compose`.

---

## 10. Decisions already settled (recap)
- No "try" playground.
- `start` defaults to **detached**, **substrate-only**, **fail-fast** with a synchronous readiness gate.
- Agents (and tools/router) run as **detached** processes too, **idempotent by name**.
- `agent list` (defined) vs `agent ps` (running).
- **Process Compose** is the supervisor; Docker/systemd/K8s are the advanced/deploy tier.
- `doctor` gains runtime/daemon-health checks.
- `init` is **one continuous, resumable** session that ends **live** with a first reply; block-and-poll
  bridges the Discord detour; targeted sub-flows remain for experts.
- **calfkit bumped 0.5.1 → 0.6.0** (mid-implementation): fixes the #180 provisioning hang (reply topic
  auto-provisions via a connect-time pre-start hook) and closes the Worker-lifecycle gaps. The reply-topic
  workaround was deleted; the runners simplified (tools/mcp/router rely on the managed `worker.run()`
  auto-provisioning; the hand-rolled agents/bridge/probe paths provision node topics via `topics_for_nodes`
  + their blind-spot topics through a slimmed `provision_and_start_broker`). No embedded broker — Tansu stays
  external; §13 substrate unchanged. **Tier 3** (fold agents + bridge onto the managed Worker lifecycle to
  collapse the hand-rolled run loops, per `docs/design/calfkit-worker-lifecycle-gaps.md`) is the follow-on
  cleanup, done as its own workflow.

**Locked after design review (detail in §12):**
- **Restart:** substrate (broker + bridge) = `restart: always` (the bridge owns its own signals and exits 0
  on a clean return, so `on_failure` won't fire); the whole roster — agents *and* tools/router/mcp — =
  `restart: on_failure` (all four roster runners use `run_worker_until_signal`, which forces a non-zero exit
  on any uncommanded exit). The shipped contract is the `supervisor/compose.py` module docstring.
- **Registry access:** reconstruct the roster from the **`agent.state` topic via calfkit's Kafka client**
  (publish discovery ping + collect responses) — host-agnostic and broker-agnostic; **not** a local file,
  **not** a tansu-specific CLI (unverified, and would break bring-your-own Kafka).
- **MCP = roster** (`calfcord mcp start|stop`); **`routing on` lazy-configures** (replaces `router setup`).

---

## 11. Documentation & README plan

**Goal:** make the README quickstart the shortest possible path to a first reply, then *naturally* funnel
interested users to the advanced docs by intent. The whole doc set must reflect the new substrate/roster
lifecycle without ever exposing the supervisor plumbing.

### 11.1 Target README quickstart (what the user reads)
The current README quickstart is **6 steps** (Discord app → install → `init` → start broker → `doctor` +
run four processes → say hello). The redesign collapses it to **4**, because `init` now ends *live*:

> **1. Set up the Discord app** (~5 min, one time) → `docs/discord-setup.md` (token + app id only — the
> wizard discovers your server and channel for you).
> **2. Install** (one line) → restart your shell.
> **3. `calfcord init`** — one guided session: pick a provider + model, name your agent, paste your Discord
> token (verified on the spot), invite the bot (the wizard waits and finds it), then it **opens your
> workspace, brings your agent online, and waits until it sees the first reply.**
> **4. Say hello** — `@assistant hello`. (Confirmation; init has already verified it.) 🎉

Mapping from today's steps:

| Today (README lines) | Becomes |
|---|---|
| Step 4 "Start a broker" (`calfcord broker`) | absorbed into `init` (substrate starts detached, health-gated) |
| Step 5 `doctor` + 4× `calfcord run <svc>` | absorbed into `init`'s live finish; surfaced later as day-2 commands |
| Step 6 "Say hello" | becomes confirmation, not the moment of truth |
| "paste Discord IDs" (discord-setup) | replaced by auto-discovery pick-lists |

### 11.2 "What you just built" — a teaching block (new)
Immediately after the quickstart, ~3 sentences that install the mental model the rest of the docs assume:
*"Your **workspace** — a local message bus and the Discord bridge — now runs in the background. Your agent
is a **teammate** that clocked in. Add more teammates, turn on the receptionist that answers un-@mentioned
messages, or split the team across machines — all without restarting the workspace."* This is the hook that
makes the "Going further" links feel like natural next steps rather than a reference dump.

### 11.3 "Day-to-day" mini command card (new, replaces the old four-process block)
```
calfcord status            # who's online (the org board)
calfcord agent new <name>  # define a teammate     →  calfcord agent start <name>  (bring it online)
calfcord logs -f           # watch the workspace
calfcord stop / start      # close / reopen the workspace
```

### 11.4 "Going further" — intent-based funnel to advanced docs (the explicit ask)
Replace the flat "Next steps" bullets with an **"I want to…"** table — users self-route by goal, which is
the most natural pull into advanced docs:

| I want to… | Go to |
|---|---|
| Create or customize an agent (fields, models, tools) | `docs/authoring-agents.md` |
| Give agents more tools / add MCP servers | `docs/authoring-tools.md`, `docs/mcp-tools.md` |
| Let agents answer messages without an `@mention` | `docs/ambient-routing.md` |
| Run agents across machines / go to production | `docs/distributed-deployment.md` + new deploy/topology |
| Understand how it works | `docs/architecture.md` |
| See everything I can do, task by task | `docs/using-calfcord.md` |
| Configure every setting | `docs/configuration.md` |
| Review security / threat model | `docs/security.md` |
| Fix something that's broken | `docs/troubleshooting.md` |

Keep the existing flat "Documentation" index lower in the README for completeness, but the **"I want to…"**
table is the primary funnel directly under the quickstart.

### 11.5 Broader doc-set updates the command change forces
The new lifecycle touches more than the README. Updated in Phase 7, after the commands ship:
- `docs/installation.md` — install + the `init`-ends-live model; the substrate-as-daemon; process-compose
  bootstrap mention.
- `docs/using-calfcord.md` — task-by-task rewritten around `start` / `agent start` / `status` / `logs`.
- `docs/architecture.md` — add the **substrate vs. roster runtime model**, the supervisor (Process Compose),
  and how it maps to the four process types; update "running modes."
- `docs/distributed-deployment.md` — frame as **Altitude 3 graduation**: same commands, remote broker URL;
  `calfcord deploy` manifests (systemd/Docker/K8s).
- `docs/troubleshooting.md` — daemon-not-running, readiness-gate failures, `status`/`doctor` triage, drift
  (process up but not registered).
- `docs/configuration.md` — new vars (process-compose version pin, health/log dirs) and the broker's role
  under the supervisor.
- `docs/discord-setup.md` — trim the ID-copying instructions superseded by auto-discovery (token + app id
  remain).
- **New:** a short "Running calfcord" page (substrate/roster lifecycle, `status`/`logs`/`ps`, the daemon) —
  or fold into `using-calfcord.md` / `architecture.md` rather than add a page, to avoid doc sprawl.

### 11.6 Consistency rule (hard)
Docs are edited **last (Phase 7), gated on the commands existing.** Until then, the README and docs must keep
describing *shipped* behavior — no command is documented before it works. The repo's existing
`docs/design/end-user-onboarding-plan.md` should be reconciled with or superseded by this doc to avoid two
competing onboarding designs.

---

## 12. Design-review corrections (must-fix before Phase 1)

Four independent review passes (architecture, feasibility, UX/docs, adversarial) on the finalized plan, all
verified against the codebase. **Where this section conflicts with §1–§11, §12 wins.** Phase 0 spikes may
proceed; Phase 1 code must not begin until these are folded in. Consensus verdict: the *shape* is sound, but
the health/readiness contract, the registry-access mechanism, and the secrets/shim/dev mechanics were
under-baked — as written, Phase 2–4 would "ship a green light that lies."

### 12.0 Decisions locked
- **Restart policy (shipped):** substrate (broker + bridge) → `restart: always`; the whole roster — agents
  *and* tools/router/mcp → `restart: on_failure`. All four roster runners use `run_worker_until_signal`,
  which forces a non-zero exit on any uncommanded exit (crash *or* clean signal-less return), so `on_failure`
  recovers a wedged roster member while an operator-commanded `stop` is suppressed from restart by Process
  Compose (it does not depend on exit code). The bridge does NOT use `run_worker_until_signal` — it owns its
  own signals and exits 0 on a clean shutdown, so `on_failure` would never fire to recover it from an
  uncommanded clean return; hence the substrate stays `always`. The live contract is the
  `supervisor/compose.py` module docstring (and `tests/supervisor/test_compose.py`).
- **Registry access is a broker operation** — not a file (CLI may be on another host) and not a tansu-specific
  CLI (unverified; would break bring-your-own Kafka). Reconstruct the live roster via calfkit's Kafka client:
  publish a `bridge.discovery` ping, collect `agent.state` `discovery_response` events over a bounded window
  (own consumer group). Powers `agent ps`, the doctor deep probe, and — by consuming `discord.outbox` —
  first-reply detection.
- **MCP = roster member** (`calfcord mcp start|stop`). **Router has a first-class, editable config surface**
  (`router show|set|edit`) mirroring agents — reusing `_providers.configure_provider` / `_fields` / `_envfile`,
  persisting to `.env` (`CALFKIT_ROUTER_PROVIDER`/`_MODEL`); lifecycle is `router start|stop`, and `start`
  fails fast if unconfigured. (Not a one-shot lazy wizard; not an `agents/*.md` file — the router is no
  Discord persona.)
- **tansu CLI:** confirmed via the GH README — `tansu` has `broker | cat | topic | proxy`, and
  **`tansu cat consume <topic>`** can read `agent.state` (so manual inspection / debugging the roster is
  possible and useful). But the *programmatic* path stays the broker-agnostic Kafka client, because
  bring-your-own-Kafka users have no `tansu` binary. `tansu cat consume agent.state` is a documented debug aid,
  not the product mechanism.

### 12.1 Health & readiness contract (CRITICAL)
- **Heartbeat must track Discord connection state, not just process liveness.** The bridge has no
  `on_disconnect`/`on_resumed` handlers; a timer-stamped beat stays green while a revoked token / dropped
  gateway leaves the bot silent. Add those handlers; the refresher must only mark "healthy" while the discord
  client reports connected. `status` must distinguish "process up, Discord disconnected" from "healthy."
- **Write the first beat on `on_ready` BEFORE slash-sync** (slash 429/slowness must not fail readiness).
- **Broker readiness = metadata/admin reachability, not bare TCP** (Tansu is no-auto-create; `broker.start()`
  blocks until topics exist — the hang you just fixed). Gate `start` on the **bridge** heartbeat (downstream
  of provisioning), with broker TCP as a fast-fail precondition only.
- Heartbeat carries `started_at` + a **restart counter** so `status` can surface flapping; `status` reconciles
  heartbeat freshness against PC process state before rendering green; pin TTL relative to refresh interval.
- Route the "green but no replies" symptom to `doctor` (deep probe), which alone can see it.

### 12.2 Roster via control-plane probe (CRITICAL) — how we build it
We do **not** expose the bridge's in-memory registry (no new bridge endpoint; the CLI may be on another host,
and the bridge may be down). Instead the CLI **queries the control plane directly over Kafka**, reconstructing
the live roster the same way the bridge does — the registry is just a projection of the control-plane topics.

**Mechanism — active discovery probe** (`calfcord/control_plane/probe.py`; import-safe; reuses
`publish_discovery_ping` + the `AgentStateEvent` schema + `state_event_to_definition`):
1. Build a transient calfkit `Client` from `CALF_HOST_URL`.
2. Subscribe to `AGENT_STATE_TOPIC` (partition 0 — it is `num_partitions=1`) **seeked to end-of-log**, so we
   collect only responses to *our* ping, not retained history.
3. Publish a `DiscoveryPingOp` to `BRIDGE_DISCOVERY_TOPIC` (exactly what the bridge does at `on_ready`).
4. Collect `AgentStateEvent`s for a bounded window (~1–2s), dedupe by `agent_id`, reduce to
   `AgentDefinition`s, return.

Why this shape:
- **No bridge dependency + true liveness.** The ping↔response loop is agent↔broker; only *currently running*
  agents answer — so `agent ps` is accurate even with the bridge down and never shows stale entries (sidesteps
  the no-TTL registry problem). The CLI simply plays the role the bridge plays at boot.
- **Broker-agnostic.** Works on bring-your-own Kafka, unlike a `tansu cat`-based path.

`agent ps` = this logical view (global, all hosts) ∪ Process Compose `/processes` (physical, host-local),
flagging "up here but not answering" (wedged) while treating "answering but not local" as expected multi-host,
not an error (§12.5). **doctor's deep probe** reuses the same call (a non-empty result proves broker+agents
function end-to-end). **First-reply detection** is a sibling — `wait_for_first_reply(client, agent_id, timeout)`
consuming the bridge reply topic (`discord.outbox`, confirm in spike).

**Rejected alternative — a bridge request/response "give me your registry" control topic:** simpler round-trip,
but it makes `agent ps` depend on the bridge being up, adds a new bridge handler, and reports the bridge's
possibly-stale view instead of true liveness. The probe is more decentralized and reuses existing machinery.

**Spike items (→ §12.8 #6):** low-level consume with manual partition-assign + seek-to-end (likely the same raw
broker path the bridge's `register_state_consumer` uses, or aiokafka via `client._connection` — the
semi-private seam the gaps doc already flags); whether `DiscoveryPingOp.request_id` is echoed on responses for
strict correlation (else accept any in-window response); the exact first-reply topic/schema; timeout tuning.

### 12.3 Secrets & decoupling in the generated YAML (CRITICAL)
- Generated `command:` lines **invoke the shim** (`calfcord run <role>` / `calfcord broker`) so venv +
  `--env-file` + `_default_env` come from one place — never reconstruct `uv run` flags, never inline secret
  literals into `state/process-compose.yaml`.
- **Per-process env scoping:** MCP `$VAR` secrets reach only the `mcp` (and bridge) process — never agents/
  tools (preserves the bridge-only-MCP-secrets invariant). Add a test asserting agent/tools entries get no
  MCP secret (in the spirit of `test_import_isolation.py`).
- `0600` on any secret-bearing generated file; no secrets in logs or heartbeats; heartbeat `identity` =
  display name / numeric id only (never token-derived).

### 12.4 Supervisor integration mechanics (CRITICAL/MAJOR)
- **Shim dispatch:** add `start|stop|status|logs|tools` cases (→ `calfcord-cli`) and extend the existing
  `mcp` case (today only `add|codegen`) to also route `start|stop`; update `usage()` and
  `scripts/tests/test_installer.sh`. (New top-level verbs otherwise fall through to the `uv run` passthrough
  and fail.) **`router` needs no shim change** — it already maps to `calfcord-cli` (`install.sh:413`), so
  `router show|set|edit|start|stop` all route correctly.
- **Binary resolution:** `$CALFCORD_PROCESS_COMPOSE_BIN` → `$CALFCORD_HOME/bin/process-compose` → `PATH`,
  with a clear dev-mode (`CALFCORD_HOME` unset) message; decide whether `start` is a shim verb (like
  `broker`) or a `calfcord-cli` subcommand.
- **Detached `up` is an open spike** — there is no documented whole-supervisor `--detached` flag; likely
  spawn PC headless (`-t=false`) under `setsid`/`nohup` and poll the REST API.
- **Lock + idempotency:** an exclusive lockfile (flock) under `state/` around `start`/`stop`; `start` probes
  first and short-circuits to "✓ already open"; `stop` is idempotent and reaps orphans.
- **Multi-home:** derive the PC REST port (default :8080) and broker port from `$CALFCORD_HOME`; **log
  rotation** on `state/logs/*` (pin a PC version that supports it).

### 12.5 Distributed framing (MAJOR)
- Process Compose is the **single-host** supervisor. The portable invariant is the **Kafka wire + `.env` +
  `agents/*.md`**, NOT the generated YAML. Reframe §1/§3/§11.5 accordingly.
- **Host-scope the generated YAML** (enumerate only this host's agents via the `--target` /
  `CALFKIT_AGENTS_DIR` seam). `agent ps`'s PC half is host-local; the Kafka-collector half is global.
- **Cross-host duplicate agents are NOT prevented today** — the bridge treats a second same-`agent_id`
  registration as a benign re-announce (`registry.py:221`), so both reply (double-reply / split-brain A2A).
  The redesign makes this one command away. **Decision: the duplicate-name guard is CLI-side only — NO
  bridge-side changes.** But it IS distributed-correct: `agent start X` calls `probe_live_roster` (broker-wide
  control-plane probe) first and refuses to start if `X` is already live on *any* host, with a user message.
  The only gap left to the bridge (deferred) is the simultaneous-start TOCTOU race; the common
  already-running case is fully covered CLI-side.

### 12.6 Onboarding UX failure paths (CRITICAL/MAJOR)
- **Discord detour:** visible heartbeat every ~10s with the invite link; soft timeout (~3–5 min) surfacing
  the common causes (clicked Authorize? Message-Content + Server-Members intents on? have Manage Server?); an
  **"I don't have a server"** branch; print **"Ctrl-C is safe & resumable"** *before* the wait.
- **First-reply detection:** fix the step-3/step-4 contradiction (prompt the `@assistant hello` *inside*
  init); ground detection on a `discord.outbox` consume (§12.2); bounded fallback ("org is live — try
  `@assistant hello`; if nothing, run `calfcord doctor`"). If unreliable, downgrade to "confirm registration
  + prompt to verify" rather than promising detection.
- **Channel pick-list filtered by POSTABILITY** (Send Messages + Manage Webhooks), not visibility; surface
  "can see but can't post" and zero-postable explicitly.
- **`start`'s success banner ALWAYS names the next step** (`→ calfcord agent start assistant`) so returning
  users aren't stranded at "substrate up, nothing replies."
- **Reboot non-survival stated honestly** (the daemon is session-scoped, not init-managed) and surfaced by
  `status`/`doctor`/the empty-roster reply; offer a launchd/systemd user unit as an option.
- Inline the **intents reminder** at the invite step; map a bridge-readiness timeout to the specific
  "privileged intents are probably off" hint.

### 12.7 Resumability & docs (MINOR)
- `state/setup.json`: atomic write (temp + `os.replace`), `schema_version`, and **advisory-not-authoritative**
  — every resumed step re-verifies the real artifact (token valid? agent `.md` parses?). Don't clobber a
  working guild/channel binding on re-run (show current, default to keep).
- Funnel (§11.4) adds rows for **Codex/subscription → `codex-auth.md`**, **agent-to-agent → `a2a-threads.md`**,
  and **day-to-day lifecycle** (start/stop/status/logs). Both Codex and A2A are headline features with zero
  README discoverability today.
- Doc-update list (§11.5) adds the **README body** ("Define your own agent", "How it works" — still teach
  `calfcord run …`/restart) and **`mcp-tools.md` / `codex-auth.md` / `a2a-threads.md`**; add a sweep:
  `grep all docs for 'calfcord run' / 'calfkit-'`.
- Resolve **`agent create` vs `new`** (the plan says `new`; current docs say `create`) — decide + doc; it's a
  user-facing rename if changed.
- Mark `docs/design/end-user-onboarding-plan.md` **Superseded by this doc** at its top.
- **Do not rebuild** the live model-list picker or tool multi-select — they already ship in `init`; only
  guild/channel auto-discovery and the live-finish are genuinely new (tighten §4.5/§4.6/§5).

### 12.8 Phase 0 spike list (expanded)
1. Detached `up` mechanism + REST readiness polling (§12.4).
2. **Dynamic add via `process-compose project update`** without bouncing the substrate — *hard gate* on the
   supervisor choice (§8 Risk #1).
3. Broker metadata-readiness probe, not TCP (§12.1).
4. PC shutdown grace ≥ agent departure-publish budget (so `AgentDepartureEvent` isn't SIGKILL'd).
5. PC REST port + lockfile parameterization for multi-home (§12.4).
6. Kafka-collector roster reconstruction (publish ping + collect `agent.state`) — prove host- &
   broker-agnostic (§12.2).
7. First-reply detection via `discord.outbox` consume (§12.6).
8. `process-compose` version pin + release-asset naming for `ensure_process_compose`.
9. (nicety) `tansu --help` to confirm/deny a manual consumer CLI.

---

## 13. Phase 0 result: GO on Process Compose — pinned facts (v1.110.0)

The Phase-0 hands-on spike (workflow `phase0-process-compose-gate`, 5 agents, PID evidence) returned **GO**.
These decisions are the contract for Phase 1+ and supersede earlier hedging.

### 13.1 Gate result
- Starting a pre-declared **`disabled: true`** roster slot via `POST /process/start/{name}` does **NOT** bounce
  the substrate (proven: substrate PID unchanged). This is the onboarding path and the common case.
- **Corrected by Phase 2d real-binary testing:** an `update_project` (`POST /project`) that *changes the
  process set* classifies **every** existing process as "updated" and **bounces the whole substrate** on
  v1.110.0 — and this is NOT first-update-only; it persists after priming reconciles. So `project update -f`
  is **NOT a PID-preserving way to add a late agent** (the earlier "additive in steady state" claim was wrong).
  Two operations *are* PID-stable (both verified against the real binary): the byte-identical **priming
  reconcile** (a no-op `update_project`, answered 207 Multi-Status), and starting a pre-declared
  **`disabled: true` slot via `POST /process/start/{name}`** (the onboarding path).
- **Priming reconcile is still worth it** as the once-after-`up` no-op (byte-identical → PID-stable, and it
  consumes the buggy first-update #494). **Onboarding issues zero process-set updates** (disabled-slot start
  only), so TTFV is unaffected.
- **Phase-3 implication (open):** adding a *brand-new* agent authored *after* `start` cannot use
  `update_project` without bouncing broker+bridge. Phase-3 options: accept + clearly warn of a brief substrate
  bounce for that one action; pre-declare a generous slot set; or defer to calfkit 0.5.3 hot-reload (§10).
  Does not affect onboarding.
- **Wire detail learned:** `POST /project` takes a **JSON** body (YAML parsed to an object), not raw YAML (the
  real server 400s on raw YAML); confined to `supervisor/client.py`.

### 13.2 Pinned facts
- **Version/asset:** pin `CALFCORD_PROCESS_COMPOSE_VERSION` default `v1.110.0`; asset
  `process-compose_${os}_${arch}.tar.gz` (os=darwin/linux, arch=arm64/amd64);
  `https://github.com/F1bonacc1/process-compose/releases/download/...`. `ensure_process_compose()` mirrors
  `ensure_tansu` (→ `$BIN_DIR/process-compose`, chmod +x, darwin xattr clear, warn-and-continue). Windows
  ships `.zip` → not bootstrapped (WSL/Docker).
- **Detached launch:** `process-compose up -f $H/state/process-compose.yaml -D -t=false -p <PC_PORT> -L $H/state/logs/process-compose.log`.
  Fixed `-p`+`-L` (default socket embeds PID). NOT `--no-server`.
- **REST (single seam `supervisor/client.py`):** default `localhost:8080`; derive port from `$CALFCORD_HOME`,
  pass `-p` to `up` AND every call. Routes: `GET /processes`, `GET /process/{name}` (state),
  `GET /process/info/{name}`, `GET /project/state`, `POST /process/start/{name}`,
  **`PATCH /process/stop/{name}`**, `POST /process/restart/{name}`, `GET /process/logs/{name}/{end}/{limit}`,
  `POST /project` (update). (`GET /process/{name}/state` is 404 — don't use.) Optional `PC_API_TOKEN`(≥20ch)+`X-PC-Token-Key`.
- **Readiness (exec only — bridge has no HTTP):** `readiness_probe.exec.command: "calfcord _healthcheck <component>"`,
  initial_delay 2 / period 3 / timeout 5 / success 1 / failure 3. Broker probe = metadata reachability (not
  bare TCP). `start` gates on **bridge** readiness; broker TCP is fast-fail precondition only.
- **depends_on:** bridge→`{broker: process_healthy}`; roster→broker (+ bridge where needed) `process_healthy`.
- **restart (shipped):** substrate broker+bridge → `availability.restart: always` (backoff 2,
  max_restarts 0) — the bridge owns its own signals and exits 0 on a clean return, so `on_failure` would
  never fire; the whole roster — agents *and* tools/router/mcp → `on_failure` (backoff 2, max_restarts 0):
  all four roster runners use `run_worker_until_signal`, so any uncommanded exit is non-zero. NEVER
  `exit_on_failure`. Intentional stop (`down` / `PATCH /process/stop`) does not auto-restart regardless of
  exit code.
- **log rotation:** `log_configuration.rotation { max_size_mb: 10, max_age_days: 7, max_backups: 5, compress: true }`;
  per-process `log_location: $H/state/logs/<component>.log`.
- **shutdown:** `shutdown { signal: 15, timeout_seconds: 10, parent_only: no }` (≫ ~2s departure budget).

### 13.3 Risks carried into implementation (each needs a test)
- **#494** → priming-reconcile no-op + integration test asserting substrate PID stable across prime + a real add.
- **`up -D` returns 0 ≠ healthy** → `start` polls bridge `is_ready` (+ broker metadata) with timeout, tears down on fail.
- **heartbeat must track Discord connection** (add `on_disconnect`/`on_resumed`; first beat on `on_ready` before slash-sync).
- **multi-home** → PC + broker ports derived from `$CALFCORD_HOME`; flock around start/stop.
- **uv-run SIGTERM forwarding** → integration assertion that `AgentDepartureEvent` publishes on a PC-driven stop.
- **reboot non-survival** → surfaced in `status`/`doctor`; optional launchd/systemd unit.
- **version-pin fragility** → all PC routes/flags confined to `supervisor/client.py`; upgrades re-run the Phase-0 spike.
```
