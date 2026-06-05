# What you can do with calfcord

You finished the [quick start](../README.md#quick-start) and said hello to your first agent. This
page is the map of everything else — each thing you might want to do, paired with the one command
that does it. Run `calfcord --help` any time for the live list.

> Rule of thumb: **`calfcord run <service>`** starts a long-running process; every other command
> (`doctor`, `agent`, `router`, `mcp`, `auth`, `self`, `init`) sets things up or manages your install.

## Check that it will actually start

Before you launch anything, run a preflight — it checks your config file, broker, Discord token + app
id, and that your agents parse:

```bash
calfcord doctor            # add --offline to skip the live Discord check
```

A `✗` means it won't boot yet — fix those first. A `⚠` is just a heads-up; you can start anyway.
(Only a `✗` makes the exit code non-zero, so `doctor` works as a gate in CI.)

## Run the system

Bring calfcord to life in Discord with the four core processes — run each in its own terminal to
start (later you can put them under a process manager like systemd):

```bash
calfcord run bridge        # the Discord gateway
calfcord run agent         # all your agents — or: calfcord run agent <name> for one
calfcord run router        # answers messages that don't @-mention anyone (optional)
calfcord run tools         # built-in tools + the agent-to-agent channel
```

(Using MCP tools — external tool servers like Gmail or Calendar? You'll also run a fifth process,
`calfcord run mcp` — see [Give your agents more tools](#give-your-agents-more-tools-mcp).)

→ Spread these across different machines: [distributed-deployment.md](distributed-deployment.md).

## Build your team of agents

An agent is just a Markdown file, but the `calfcord agent` commands write and edit that file for you
— covering the whole lifecycle. After any change, restart `calfcord run agent` (and `calfcord run bridge`
too when you add or rename one, so its `/<name>` command registers).

**Add an agent** — a guided wizard for the name, provider, model, and tools:

```bash
calfcord agent create               # or name it up front: calfcord agent create scribe
```

**See your team** at a glance, or **look at one** in full:

```bash
calfcord agent list                 # add --json for scripts
calfcord agent show scribe
```

**Change an agent** — pick a field interactively, or set fields directly:

```bash
calfcord agent edit scribe
calfcord agent set scribe --model gpt-5 --thinking-effort high
```

**Change only its tools, rename it, or remove it:**

```bash
calfcord agent tools scribe
calfcord agent rename scribe penny  # moves the file, the /command, and saved state
calfcord agent delete scribe        # --yes to skip the confirmation
```

→ Every frontmatter field explained: [authoring-agents.md](authoring-agents.md).

## Let agents reply without an @-mention

Want an agent to answer ambient chatter (no `@name`)? Configure the router once, then run it:

```bash
calfcord router setup
calfcord run router
```

It's optional — without it, `@mentions` still work and un-mentioned messages just go unanswered.
→ How routing picks who answers: [ambient-routing.md](ambient-routing.md).

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

MCP tools are served by their own process, so run it alongside the others:

```bash
calfcord run mcp           # the MCP bridge that holds the server connections + secrets
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

Just need to repoint the broker?

```bash
calfcord self set-broker my-broker:9092
```

→ Every setting, in one table: [configuration.md](configuration.md).

## Keep calfcord up to date

```bash
calfcord self version      # what's installed
calfcord self status       # is a newer version available?
calfcord self update       # upgrade to the latest
calfcord self rollback     # undo the last update
```

(`calfcord self` also has `set-broker`, covered above.)

---

*The older `calfcord calfkit-<service>` names still work as aliases for `calfcord run <service>`.*
