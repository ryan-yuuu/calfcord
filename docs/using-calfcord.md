# What you can do with calfcord

You finished the [quick start](../README.md#quick-start): `calfcord init` opened your workspace, brought
your first agent online, and you said hello to confirm it. This page is the map of everything else — each
thing you might want to do, paired with the one command that does it. Run `calfcord --help` any time for the
live list.

> Mental model: your **workspace** is the always-on substrate — a local message bus (the broker) and the
> Discord bridge — that `calfcord init` started in the background. Everything else is the **roster**: agents,
> the tools host, the ambient router, and any MCP servers. Roster members clock in and out on demand while the
> workspace stays open. `calfcord start` opens the workspace; `calfcord ... start` brings each teammate online.

## Check that it will actually start

Before you open the workspace (or after editing config), run a preflight — it checks your config file, broker,
Discord token + app id, and that your agents parse:

```bash
calfcord doctor            # add --offline to skip the live Discord check
```

A `✗` means it won't boot yet — fix those first. A `⚠` is just a heads-up; you can start anyway.
(Only a `✗` makes the exit code non-zero, so `doctor` works as a gate in CI.)

## Open and close the workspace

`init` already opened the workspace for you. To open it again after a reboot — or after `calfcord stop` —
bring up the substrate:

```bash
calfcord start             # broker + bridge, detached and health-gated
```

`start` launches **only the substrate** (the broker and the Discord bridge), waits until the bridge is
actually ready, and returns. No agents run yet — you start those on demand (next section). It's detached, so
it keeps running in the background after the command exits; it does **not** survive a reboot, so re-run
`calfcord start` after one.

Close everything down with:

```bash
calfcord stop              # closes the workspace (stops the supervised substrate)
```

> The substrate runs under a small process supervisor (Process Compose) that `calfcord init` bootstrapped for
> you. You never edit it — calfcord generates the supervisor's config from your agents on disk. See
> [architecture.md](architecture.md) for the substrate/roster runtime model, or run
> `calfcord explain topology` for a one-screen tour of how the pieces split and why.

## See who's online

```bash
calfcord status            # the org board: substrate + roster health
```

`status` shows the substrate (broker + bridge) and any roster members that are running, with each one's state.
If the workspace isn't open, it says so and points you at `calfcord start`.

To see just the agents — including any running on another machine — use the roster board:

```bash
calfcord agent ps          # RUNNING agents (vs. `agent list`, which shows DEFINED agents)
```

`agent ps` unions two views: agents answering across the whole org (true liveness, host-agnostic) and the
agents running locally. An agent answering from another host shows as `running on another host` — that's
expected in a [distributed setup](distributed-deployment.md), not an error.

## Watch the logs

Every supervised component writes its output to `$CALFCORD_HOME/state/logs/<name>.log`. Tail them without
hunting for the files:

```bash
calfcord logs              # one-shot dump of every component, each line labeled
calfcord logs -f           # follow all components live (Ctrl-C to stop)
calfcord logs bridge -f    # follow just one — e.g. bridge, broker, or an agent name
```

Because `logs` reads the files straight off disk, it works even when the supervisor isn't running — so
"what did the broker say before it died?" is always answerable.

## Build your team of agents

An agent is just a Markdown file, but the `calfcord agent` commands write and edit that file for you —
covering the whole lifecycle.

**Add an agent** — a guided wizard for the name, provider, model, and tools:

```bash
calfcord agent create               # or name it up front: calfcord agent create scribe
```

A brand-new agent isn't in the running workspace yet, so the supervisor doesn't know about it. After creating
one, **reload the workspace** so it picks up the new agent, then bring the agent online:

```bash
calfcord stop && calfcord start     # reload the workspace so it sees the new agent
calfcord agent start scribe         # clock the agent in
```

(Already-defined agents don't need the reload — only a *newly created* one does. The bridge also re-registers
its `/<name>` slash command on this reload.)

**Bring an agent online or take it offline** — clock a teammate in or out without touching anyone else:

```bash
calfcord agent start scribe
calfcord agent stop scribe
```

**See your team** at a glance, or **look at one** in full:

```bash
calfcord agent list                 # DEFINED agents; add --json for scripts
calfcord agent show scribe
```

**Change an agent** — pick a field interactively, or set fields directly:

```bash
calfcord agent edit scribe
calfcord agent set scribe --model gpt-5 --thinking-effort high
```

A running agent bakes its config at startup, so after editing an agent's `.md`, **restart it** to apply the
change:

```bash
calfcord agent restart scribe       # reload a running agent after editing its .md
```

**Change only its tools, rename it, or remove it:**

```bash
calfcord agent tools scribe
calfcord agent rename scribe penny  # moves the file, the /command, and saved state
calfcord agent delete scribe        # --yes to skip the confirmation
```

→ Every frontmatter field explained: [authoring-agents.md](authoring-agents.md).

## Let agents reply without an @-mention

Want an agent to answer ambient chatter (no `@name`)? Configure the optional router once, then bring it
online:

```bash
calfcord router edit                # configure provider + model interactively
calfcord router start               # bring the router online (needs config)
```

`router show` prints the current config and `router set` changes it non-interactively (for scripts/CI);
`router stop` takes it offline. Routing is optional — without it, `@mentions` still work and un-mentioned
messages just go unanswered.
→ How routing picks who answers: [ambient-routing.md](ambient-routing.md).

## Run the built-in tools host

The filesystem, shell, search, web, and todos tools — plus the agent-to-agent `private_chat` channel — run in
their own host. Bring it online when an agent needs them:

```bash
calfcord tools start
calfcord tools stop
```

→ What each tool does and how to write your own: [authoring-tools.md](authoring-tools.md).

## Give your agents more tools (MCP)

Add an external tool server (Gmail, Calendar, …) — `add` and `codegen` use the same server name and
transport:

```bash
calfcord mcp add gmail --command "npx -y @org/gmail-mcp" --env GMAIL_TOKEN
calfcord mcp codegen gmail --command "npx -y @org/gmail-mcp"
```

`add` records how to reach the server in `mcp.json` (here, that it reads `GMAIL_TOKEN` from your
environment); `codegen` connects to the server and generates the tool schema agents load. Then list
the tool in an agent's `tools:`. MCP tools are namespaced `mcp/<server>` — for example,
`tools: [mcp/gmail]`.

MCP tools are served by their own roster host, so bring it online alongside the others:

```bash
calfcord mcp start         # the MCP host that holds the server connections + secrets
calfcord mcp stop
```

→ Full MCP workflow: [mcp-tools.md](mcp-tools.md).

## Use your ChatGPT subscription as the model

Don't want to pay per token? Sign in with a ChatGPT Plus/Pro account (Codex) — a device-code flow
that works the same locally or over SSH:

```bash
calfcord auth codex login
calfcord auth codex status         # confirm you're signed in
```

Then set an agent's `provider: openai-codex`.
→ Details: [codex-auth.md](codex-auth.md).

## Change your configuration later

Re-run the wizard to change your provider, Discord token, or broker — it keeps anything you don't
retype:

```bash
calfcord init
```

Just need to repoint the broker (for example, to move agents onto a remote one)?

```bash
calfcord self set-broker my-broker:9092
```

→ Every setting, in one table: [configuration.md](configuration.md).

## Run across machines or in production

The same commands work when the broker lives on another host: point each machine at it and run `calfcord
start` and `calfcord agent start <name>` per host. When you're ready to put the substrate under a real
process manager, generate a manifest:

```bash
calfcord deploy systemd            # or: k8s | docker; -o PATH to write to a file
```

→ The full distributed story: [distributed-deployment.md](distributed-deployment.md).

## Keep calfcord up to date

```bash
calfcord self version      # what's installed
calfcord self status       # is a newer version available?
calfcord self update       # upgrade to the latest
calfcord self rollback     # undo the last update
```

(`calfcord self` also has `set-broker`, covered above.)

## Advanced: run a single component by hand

`calfcord start` and the roster commands are the supported way to run calfcord. When you need to run one
component in the foreground — for debugging, or in a container where the supervisor isn't available — there's
a low-level escape hatch:

```bash
calfcord run bridge        # the Discord gateway
calfcord run agent <name>  # one agent in the foreground
calfcord run router        # the ambient router
calfcord run tools         # the built-in tools host
calfcord run mcp           # the MCP host
calfcord broker            # the bundled native broker, standalone
```

These are the same processes the supervisor manages for you — reach for them only when you specifically want a
single foreground process. For everyday use, prefer `calfcord start` + `calfcord agent start <name>`.
