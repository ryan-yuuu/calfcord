# What you can do with Agent Disco

You finished the [quick start](../README.md#quick-start): `disco init` opened your workspace, brought
your first agent online, and you said hello to confirm it. This page is the map of everything else — each
thing you might want to do, paired with the one command that does it. Run `disco --help` any time for the
live list.

> Mental model: your **workspace** is the always-on substrate — a local message bus (the broker) and the
> Discord bridge — that `disco init` started in the background. Everything else is the **roster**: your
> agents, the tools host, and any MCP servers. Roster members clock in and out on demand while the
> workspace stays open. `disco start` opens the workspace; `disco ... start` brings each teammate online.

## Check that it will actually start

Before you open the workspace (or after editing config), run a preflight — it checks your config file, broker,
Discord token + app id, and that your agents parse:

```bash
disco doctor            # add --offline to skip the live Discord check
```

A `✗` means it won't boot yet — fix those first. A `⚠` is just a heads-up; you can start anyway.
(Only a `✗` makes the exit code non-zero, so `doctor` works as a gate in CI.)

## Open and close the workspace

`init` already opened the workspace for you. To open it again after a reboot — or after `disco stop` —
bring up the substrate:

```bash
disco start             # broker + bridge, detached and health-gated
```

`start` launches **only the substrate** (the broker and the Discord bridge), waits until the bridge is
actually ready, and returns. No agents run yet — you start those on demand (next section). It's detached, so
it keeps running in the background after the command exits; it does **not** survive a reboot, so re-run
`disco start` after one.

Close everything down with:

```bash
disco stop              # closes the workspace (stops the supervised substrate)
```

> The substrate runs under a small process supervisor (Process Compose) that `disco init` bootstrapped for
> you. You never edit it — Agent Disco generates the supervisor's config from your agents on disk. See
> [architecture.md](architecture.md) for the substrate/roster runtime model, or run
> `disco explain topology` for a one-screen tour of how the pieces split and why.

## See who's online

```bash
disco status            # the org board: substrate + roster health
```

`status` shows the substrate (broker + bridge) and any roster members that are running, with each one's state.
If the workspace isn't open, it says so and points you at `disco start`.

To see just the agents — including any running on another machine — use the roster board:

```bash
disco agent ps          # RUNNING agents (vs. `agent list`, which shows DEFINED agents)
```

`agent ps` unions two views: the agents online across the whole org (read from calfkit's live **mesh**,
host-agnostic) and the agents running locally on this host. An agent online on another host shows as
`running on another host` — that's expected in a [distributed setup](distributed-deployment.md), not an
error. (Liveness is heartbeat-based, so a crashed agent can linger as "online" for up to ~90s before it
lapses.)

## Watch the logs

Every supervised component writes its output to `$CALFCORD_HOME/state/logs/<name>.log`. Tail them without
hunting for the files:

```bash
disco logs              # one-shot dump of every component, each line labeled
disco logs -f           # follow all components live (Ctrl-C to stop)
disco logs bridge -f    # follow just one — e.g. bridge, broker, or an agent name
```

Because `logs` reads the files straight off disk, it works even when the supervisor isn't running — so
"what did the broker say before it died?" is always answerable.

## Build your team of agents

An agent is just a Markdown file, but the `disco agent` commands write and edit that file for you —
covering the whole lifecycle.

**Add an agent** — a guided wizard for the name, provider, model, and tools:

```bash
disco agent create               # or name it up front: disco agent create scribe
```

A brand-new agent isn't in the running workspace yet, so the supervisor doesn't know about it. After creating
one, **reload the workspace** so it picks up the new agent, then bring the agent online:

```bash
disco stop && disco start     # reload the workspace so it sees the new agent
disco agent start scribe         # clock the agent in
```

(Already-defined agents don't need the reload — only a *newly created* one does. The bridge also re-registers
its `/<name>` slash command on this reload.)

**Bring an agent online or take it offline** — clock a teammate in or out without touching anyone else:

```bash
disco agent start scribe
disco agent stop scribe
```

**See your team** at a glance, or **look at one** in full:

```bash
disco agent list                 # DEFINED agents; add --json for scripts
disco agent show scribe
```

**Change an agent** — pick a field interactively, or set fields directly:

```bash
disco agent edit scribe
disco agent set scribe --model gpt-5 --thinking-effort high
```

A running agent bakes its config at startup, so after editing an agent's `.md`, **restart it** to apply the
change:

```bash
disco agent restart scribe       # reload a running agent after editing its .md
```

**Change only its tools, rename it, or remove it:**

```bash
disco agent tools scribe
disco agent rename scribe penny  # moves the file, the /command, and saved state
disco agent delete scribe        # --yes to skip the confirmation
```

→ Every frontmatter field explained: [authoring-agents.md](authoring-agents.md).

## How agents get invoked

Agents reply when you **`@mention`** them (`@scribe summarize this`). There is no
ambient auto-answering — a message with no `@mention` goes unanswered by design,
and there is no router to configure. A mentioned agent can also consult or hand
off to a peer; that agent-to-agent traffic is projected to an audit channel (see
[a2a-threads.md](a2a-threads.md)).

## Run the built-in tools host

The terminal, filesystem, search, code-execution, web, and todo tools run in
their own host. Bring it online when an agent needs them:

```bash
disco tools start
disco tools stop
```

For multi-host deploys you can expose a tool under a second name (so an agent
can route a call to a specific host) — `disco tools alias` manages that:

```bash
disco tools alias add terminal terminal_eu   # expose `terminal` also as `terminal_eu`
disco tools alias list
disco tools alias remove terminal_eu          # by the new name (--restart to apply now)
```

→ What each tool does and how to write your own: [authoring-tools.md](authoring-tools.md).
The multi-host alias story: [distributed-deployment.md](distributed-deployment.md).

## Give agents external tools over MCP

Want your agents to call a GitHub server, a docs endpoint, or any other Model
Context Protocol (MCP) server? Add it to `mcp.json` and Agent Disco runs it as its
own roster process that advertises its tools on the bus:

```bash
disco mcp add                    # interactive wizard (name → transport → command/URL → env)
disco mcp list                   # configured servers + best-effort running state
disco mcp start github           # bring one online (start of a running slot = restart in place)
disco mcp start --all            # re-pick up mcp.json: (re)start every configured server
disco mcp stop github            # take it offline
disco mcp restart github         # reload after editing its mcp.json entry
disco mcp remove github          # delete the entry (--force to skip the confirm)
```

A server you add *after* `disco start` isn't a declared supervisor slot yet,
so reload the workspace once (`disco stop && disco start`) before starting
it — `disco mcp start` prints this hint when it applies. Then grant the tools
to an agent by adding an `mcp/<server>` (or `mcp/<server>/<tool>`) entry to its
`tools:` list — `disco agent tools <name>` offers those rows — and restart
the agent to apply.

→ The full end-to-end walkthrough, `mcp.json` schema, and selector grammar:
[mcp-tools.md](mcp-tools.md).

## Use your ChatGPT subscription as the model

Don't want to pay per token? Sign in with a ChatGPT Plus/Pro account (Codex) — a device-code flow
that works the same locally or over SSH:

```bash
disco auth codex login
disco auth codex status         # confirm you're signed in
```

Then set an agent's `provider: openai-codex`.
→ Details: [codex-auth.md](codex-auth.md).

## Change your configuration later

Re-run the wizard to change your provider, Discord token, or broker — it keeps anything you don't
retype:

```bash
disco init
```

Just need to repoint the broker (for example, to move agents onto a remote one)?

```bash
disco self set-broker my-broker:9092
```

→ Every setting, in one table: [configuration.md](configuration.md).

## Run across machines or in production

The same commands work when the broker lives on another host: point each machine at it and run `disco
start` and `disco agent start <name>` per host. When you're ready to put the substrate under a real
process manager, generate a manifest:

```bash
disco deploy systemd            # or: k8s | docker; -o PATH to write to a file
```

→ The full distributed story: [distributed-deployment.md](distributed-deployment.md).

## Keep Agent Disco up to date

```bash
disco self version      # what's installed
disco self status       # is a newer version available?
disco self update       # upgrade to the latest
disco self rollback     # undo the last update
```

(`disco self` also has `set-broker`, covered above.)

## Advanced: run a single component by hand

`disco start` and the roster commands are the supported way to run Agent Disco. When you need to run one
component in the foreground — for debugging, or in a container where the supervisor isn't available — there's
a low-level escape hatch:

```bash
disco run bridge        # the Discord gateway
disco run agent <name>  # one agent in the foreground
disco run tools         # the built-in tools host
disco run mcp <server>  # one MCP server's toolbox (from mcp.json)
disco broker            # the bundled native broker, standalone
```

These are the same processes the supervisor manages for you — reach for them only when you specifically want a
single foreground process. For everyday use, prefer `disco start` + `disco agent start <name>`.
