# Onboarding & CLI UX — Smoother Quickstart, Daemonized Services, Clearer Commands

**Status:** In progress. The **CLI command-surface** group shipped (top-level help, friendly
`calfcord run` / `mcp` / `auth` verbs, and `calfcord doctor`) via
[PR #34](https://github.com/ryan-yuuu/calfcord/pull/34). The **process-lifecycle** work
(`calfcord up` / `install-daemon`) and the **onboarding-sequencing** flip remain. Partly built on
[`roadmap/tansu-broker.md`](tansu-broker.md).

## Goal

Make calfcord easy for a non-developer to install and run. A first run still requires standing up a
**distributed system by hand** — a broker plus four long-lived processes in four separate terminals.
The goal is a quickstart that reaches "it works" in **one or two commands**, with long-running
services that **don't need a babysat terminal**. (The command *surface* itself — a discoverable
`--help`, friendly `run` verbs, a `doctor` preflight — is done; what remains is the *lifecycle* and
the onboarding *sequencing*.)

## Guiding principle: get to "it works" first; defer distribution

The two comparable agent frameworks both reach a working state with a **single command / single
process** and treat decoupled, multi-host deployment as an explicit *later* step:

- **OpenClaw** — `openclaw onboard --install-daemon` registers one background daemon; remote/clustered
  nodes live in advanced docs only.
- **Hermes** — `hermes` runs a single local process; distributed execution backends are step 6 ("add
  the next layer"), gated on a working local chat first.

calfcord currently does the opposite: it front-loads the hardest operational concept — operating a
four-process distributed system by hand — into the very first run. The sequencing should flip: a
**one-box, one-command** path by default; **decoupled / multi-host** deployment as the documented
advanced path ([`docs/distributed-deployment.md`](../docs/distributed-deployment.md)), not the
onboarding default.

## Shipped so far ([PR #34](https://github.com/ryan-yuuu/calfcord/pull/34))

- **Top-level help** — `calfcord -h` / `--help` / `help` all print usage.
- **Friendly run verbs** — `calfcord run <bridge|agent|router|tools|mcp>`, with `calfcord calfkit-*`
  kept as hidden aliases (resolves the "agent" overload and stops leaking the internal `calfkit-` name).
- **Surfaced commands** — `calfcord mcp <add|codegen>` and `calfcord auth` are first-class, not
  passthrough-only.
- **`calfcord doctor`** — a config preflight (config file, broker reachability, Discord token + app id,
  agents parse) with a ✓/⚠/✗ report and a non-zero exit on failure.
- **A "what you can do" guide** — [`docs/using-calfcord.md`](../docs/using-calfcord.md), the
  post-quickstart command map.

## Remaining friction

1. **Four processes in four terminals + a broker.** No single command brings the system up; the README
   only suggests "each in its own terminal, or under a supervisor" — and ships no supervisor.
2. **No runtime status.** `calfcord doctor` answers "is it *configured* right?" but there's still no
   "is it *running / connected*?" — and `calfcord self status` (an update check) muddies the word.
3. **Edit → restart loop.** Nearly every agent change ends with "restart `calfcord run agent`"; no reload.
4. **Broker is copy-paste.** A 5-line `docker run` with no lifecycle command.
5. **`init` configures but launches nothing** — the wizard ends and the user still faces the manual
   broker + four-process steps.

## Remaining work

### 1. Process lifecycle — supervise and daemonize

Two modes over the same supervision model (separate processes, never co-hosted in one event loop — see
the calfkit lifecycle constraint below):

- **Foreground supervisor — `calfcord up` / `calfcord down`.** One terminal brings up the broker + the
  four processes with merged, prefixed logs and a single Ctrl-C. The "watch it run" / iterating mode.
- **Daemon mode — `calfcord install-daemon` / `uninstall-daemon`.** Register the long-running services
  as OS-managed daemons — **launchd** on macOS, **systemd `--user`** on Linux — so they start at login,
  restart on crash, survive closing the terminal, and need no babysat window. This is OpenClaw's
  `--install-daemon` model. The **broker** (native Tansu) and the **Discord bridge** are the prime
  daemon candidates, with agent / router / tools alongside (a grouped `target` on systemd).

Design constraints to carry in:

- **Do not co-host the four in one process.** The bridge and `calfkit-agent` hand-roll start/serve/drain
  and own OS signals (`docs/design/calfkit-worker-lifecycle-gaps.md`); supervise them as separate child
  processes and sequence a graceful drain on stop. The supervisor is a process manager, not a
  replacement for `Worker.run()`.
- **The broker is a different kind of thing.** It's a persistent service holding topic/offset state —
  "ensure it's up," rather than tying its lifecycle to a session. On a native Tansu install it can be
  its own daemon; with Docker it stays a detached container.
- **Confirm clean `SIGTERM` drain** so `systemctl stop` / `launchctl unload` don't drop in-flight messages.

### 2. Round out the command surface

- **Runtime `status`.** Add `calfcord status` (is it running / connected) — distinct from `doctor`
  (config preflight). Consider renaming the update check (`calfcord self status` →
  e.g. `self check-update`) so "status" unambiguously means runtime health.
- **Broker lifecycle as commands.** `calfcord broker up|down` (and the native start from the Tansu
  roadmap) instead of a copy-pasted `docker run`. *(Depends on the Tansu native broker.)*

### 3. Onboarding / quickstart sequencing

- **`calfcord init` offers to start.** End the wizard with an optional "start calfcord now?" that runs
  `calfcord up` or installs the daemon — configuration flows straight into a running system.
- **Quickstart rewrite.** Lead with the one-box path: `install → calfcord init → calfcord up`
  (or `install-daemon`) → `@agent hello`. Move the four-process / multi-host material into the advanced
  distributed-deployment section.
- **Shrink the edit→restart loop.** Once a supervisor owns the processes, a `calfcord reload` can
  restart just the agent worker instead of "switch terminals, Ctrl-C, re-run" after every change.

## Relationship to other work

- **Builds on [Tansu native broker](tansu-broker.md).** A native broker binary is what makes
  daemonizing the broker clean for the no-Docker audience.
- **Bounded by the calfkit Worker lifecycle gap** (`docs/design/calfkit-worker-lifecycle-gaps.md`) —
  the supervisor/daemon must respect hand-rolled start/serve/drain and OS-signal ownership.
- **Backward compatibility.** Keep `calfcord calfkit-*` passthrough as hidden aliases so existing docs
  and muscle memory keep working through any rename.

## Open questions / decisions

- Foreground supervisor vs daemon vs both — which ships first? (Lean: both; foreground supervisor first.)
- Cross-platform service generation (launchd plist + systemd unit) — generate-and-register vs document.
- Where the supervisor lives — a `calfcord up` Python entrypoint vs a shim-level multiplexer.

## References

- Competitor onboarding patterns: OpenClaw (`onboard --install-daemon`, single daemon) and Hermes
  (single-process first; distribution as "add a layer").
- `docs/design/calfkit-worker-lifecycle-gaps.md` — why the four processes can't be co-hosted today.
- [`roadmap/tansu-broker.md`](tansu-broker.md) — the native broker this builds on.
- [`docs/distributed-deployment.md`](../docs/distributed-deployment.md) — the advanced path onboarding
  should defer to.
