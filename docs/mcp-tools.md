# MCP tools

Model Context Protocol (MCP) servers let your agents call tools that live
*outside* calfcord — a GitHub server, a docs search endpoint, anything that
speaks MCP. calfcord hosts each configured server as its own roster process,
advertises the tools that server publishes onto the message bus, and lets any
agent grant itself those tools with one line of frontmatter.

This page is both the how-to (set one up end to end) and the reference
(`mcp.json` schema, selector grammar, lifecycle and reload rules). For the
*why* behind the design — per-server isolation, the secrets boundary, runtime
discovery — see the [design note](./design/mcp-reintroduction.md); for where
the MCP processes sit in the topology, see
[`architecture.md`](./architecture.md#the-four-processes).

> Terminology used throughout: an **MCP server** is the external program or
> endpoint you point calfcord at (the thing in `mcp.json`); a **toolbox** is
> the calfkit node calfcord runs to host it; a **slot** is that toolbox's
> Process Compose process (`mcp-<server>`).

## The shape of it

- You declare servers in **`mcp.json`** (the same `{"mcpServers": {...}}`
  schema Cursor and Claude Code use). One entry per server.
- Each server runs as **its own process** — slot `mcp-<server>`, command
  `calfcord run mcp <server>` — that hosts exactly one calfkit toolbox. One
  process per server is deliberate: a toolbox whose server is unreachable
  fails its own worker at boot, and MCP server configs (operator-supplied
  commands and URLs) are the most misconfiguration-prone config in the system,
  so one bad entry must never take down sibling servers.
- That toolbox connects to the server, lists its tools, and **advertises**
  them on the compacted `mcp.capabilities` topic — tool names, JSON schemas,
  and the dispatch topic `mcp_server.<name>`.
- An agent grants itself MCP tools with an `mcp/<server>` or
  `mcp/<server>/<tool>` entry in its `tools:` frontmatter. The agent resolves
  those selectors against the advertisement **per turn** — runtime discovery,
  no schemas committed to the repo, no agent restart when a server's tool list
  changes.
- Only the `mcp-<server>` processes (and the `calfcord mcp` CLI) ever read
  `mcp.json`. Agents read the advertisement from the broker, so on a
  distributed deploy the agent hosts hold no MCP config and no MCP secrets.

## Set up an MCP server end to end

The whole flow is: **add** the server to `mcp.json`, **start** its process,
**grant** the tools to an agent, **restart** the agent, then **use** it.

### 1. Add the server

`calfcord mcp add` with no transport flag drops you into a short wizard — name,
transport (stdio or HTTP), command or URL, an env/header loop, a JSON preview,
then an optional start:

```bash
calfcord mcp add
```

Or do it non-interactively (scripting / CI). A stdio server launched by a local
command:

```bash
calfcord mcp add github \
  --command "npx -y @modelcontextprotocol/server-github" \
  --env GITHUB_TOKEN          # bare NAME means env GITHUB_TOKEN=$GITHUB_TOKEN
```

An HTTP (Streamable HTTP) server:

```bash
calfcord mcp add docs \
  --url https://docs.example.com/mcp \
  --header "Authorization=Bearer \$DOCS_TOKEN"
```

Both modes funnel into the same validated, atomic writer, so a wizard answer
and a flag can never persist different things. The entry is shape-checked with
the *loader's own* validator before anything touches disk — the writer can
never produce a file a server boot would reject. `--dry-run` prints the merged
JSON and writes nothing; `--force` replaces an existing entry; `--start` (or
answering yes in the wizard) starts the slot immediately.

`$VAR` references are nudged but not required: a literal value earns a one-line
"consider a `$VAR`" note and ships as-is (the file is `0600` and matches the
configs people paste from). Keep credentials in `config/.env` and reference
them as `$VAR` — see the [reference](#mcpjson-reference) below.

### 2. Start its process

```bash
calfcord mcp start github
```

This brings the `mcp-github` slot online; the toolbox connects to the server,
lists its tools, and advertises them. Confirm it is up and advertising:

```bash
calfcord mcp list           # transport summary + best-effort running state
calfcord logs mcp-github -f # follow the toolbox's own log
```

> **New-server caveat.** A server added to `mcp.json` *after* `calfcord start`
> has no declared supervisor slot yet — exactly like a brand-new agent `.md`.
> `calfcord mcp start <server>` will print a hint to that effect; reload the
> workspace once so the generated supervisor config picks up the new slot:
>
> ```bash
> calfcord stop && calfcord start
> calfcord mcp start github
> ```
>
> An already-declared server needs no reload. See
> [Lifecycle and reload](#lifecycle-and-reload).

### 3. Grant the tools to an agent

Add an MCP selector to the agent's `tools:` list. Either edit the `.md`
directly:

```yaml
tools: [read_file, mcp/github]          # every tool the github server advertises
```

or use the interactive editor, which offers an `mcp/<server>` row per
configured server plus live per-tool rows when the broker is reachable:

```bash
calfcord agent tools scribe
```

See [Selector grammar](#selector-grammar) for `mcp/<server>` vs.
`mcp/<server>/<tool>`.

### 4. Restart the agent

An agent bakes its `tools:` list into its calfkit node at boot, so a *new
selector in the `.md`* needs an agent restart to take effect — the same rule as
adding a builtin tool:

```bash
calfcord agent restart scribe
```

(This is distinct from a server's tool list *changing* — that needs no agent
restart; see [Runtime discovery](#runtime-discovery).)

### 5. Use it

The agent's LLM now sees each advertised tool under the name the server
advertises (there is no rename layer). A tool call dispatches over
`mcp_server.github` to the toolbox, which forwards it to the MCP server and
returns the result.

## `mcp.json` reference

`mcp.json` lives at `$CALFCORD_HOME/config/mcp.json` (next to `config/.env`).
The installer seeds an empty `{"mcpServers": {}}` at `0600` on first install
and never clobbers it. Resolution order for the path a process uses:

1. `$CALFCORD_MCP_CONFIG` — an explicit operator override (wins).
2. `$CALFCORD_HOME/config/mcp.json` — the installed location.
3. `./mcp.json` — the dev fallback for repo-checkout (`uv run`) runs.

The schema is the one Cursor and Claude Code use, so you can paste entries
straight from those tools' docs:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"}
    },
    "docs": {
      "type": "http",
      "url": "https://docs.example.com/mcp",
      "headers": {"Authorization": "Bearer $DOCS_TOKEN"}
    }
  }
}
```

### Per-server fields

| Transport | Required | Optional | Notes |
|---|---|---|---|
| **stdio** | `command` (string) | `args` (list of strings), `env` (string→string map), `cwd` (string), `"type": "stdio"` | A local command this host launches (npx / uvx / a binary). The default when `command` is present. |
| **HTTP** | `"type": "http"`, `url` (string) | `headers` (string→string map) | A running Streamable HTTP MCP endpoint. (Streamable HTTP covers SSE — use `"type": "http"`.) |

The key sets are closed: an unknown key (a typo like `"evn"`) fails the load
loudly, naming the server and the bad key, rather than silently spawning a
server without its credentials. A single entry can't be both stdio and HTTP.

### Environment-variable expansion

Any string value may reference an environment variable:

- `$VAR` and `${VAR}` expand against the process environment **at load time**.
- `$$` escapes a literal `$`.
- **Literal values are allowed** but discouraged — keep secrets in
  `config/.env` and reference them as `$VAR`.
- An **unset** reference fails the load, naming the variable — never a silent
  empty string. An unbalanced `${` also fails loudly.

Expansion happens only when the server *starts* (it is the secrets-touching
step). Enumeration paths — `mcp list`, supervisor slot generation, the tool
picker — read names without expanding values, so they work on hosts where the
referenced secrets are unset.

### Server names

A server name must match `[a-z0-9_]{1,64}`. The name doubles as the dispatch
topic segment (`mcp_server.<name>`), the `mcp.json` key, and the slot suffix
(`mcp-<name>`), so lowercase-plus-underscore is the safe intersection. An
invalid name is rejected at add time and at load time.

## Selector grammar

Agents declare MCP tools in the *same* `tools:` list as builtins, using an
`mcp/` prefix:

| Selector | Grants |
|---|---|
| `mcp/<server>` | **Every** tool the server currently advertises (a wildcard — a server that later advertises new tools enlarges the agent's surface). |
| `mcp/<server>/<tool>` | **Exactly** that one tool. The tool segment matches `[a-zA-Z0-9_-]{1,128}` (the upstream server's own name, which may be mixed-case or hyphenated). |

Merge rules when a server appears more than once: a bare `mcp/<server>`
subsumes that server's explicit `mcp/<server>/<tool>` entries (the wildcard
wins); explicit-only entries dedupe into a fixed include set.

Two properties worth internalizing:

- **MCP is never part of the "all builtins" default.** Omitting `tools:`
  entirely grants every builtin — but *no* MCP tools. MCP grants are always
  explicit.
- **Validation at parse/write time is syntax-only.** Whether the named server
  is configured or running is a runtime concern — there is no static catalog to
  check a selector against (that is the point of runtime discovery). A
  malformed selector (bad grammar) is rejected immediately, naming the
  offending line.

## Runtime discovery

The toolbox is the source of truth for *which tools exist right now*. It
publishes a capability record to the compacted `mcp.capabilities` topic on
startup, re-publishes on the server's `tools/list_changed` notification and on
a heartbeat, and an agent resolves its selectors against that record **on every
turn**. The consequences:

- **No schemas in the repo.** Tool schemas live on the bus, not in committed
  files. (The removed MCP support required committing generated schema modules;
  this design has none.)
- **A server's tool list can change without an agent restart.** If the github
  server starts advertising a new tool, an agent holding `mcp/github` picks it
  up on its next turn — no restart. (Changing the agent's *own `.md` selectors*
  still needs an agent restart, because the `tools:` list is baked in at boot —
  see [step 4](#4-restart-the-agent).)
- **A down server degrades the turn, it never blocks the agent.** Selection is
  non-strict by policy: an agent declaring `mcp/github` boots and answers
  normally even if the github server is down or not yet started — the affected
  tools just drop out of that turn, with the degradation logged. Declaring an
  MCP tool must not hold the agent hostage to the server's uptime.

Clean shutdown of a toolbox tombstones its record (the tools disappear from the
view); a *crash* leaves the last-known record in place, so agents keep the
tools they had until the slot recovers.

## Lifecycle and reload

The `calfcord mcp` verbs mirror the agent roster verbs, so the muscle memory
carries over:

```bash
calfcord mcp start <server>      # bring a server online (start of a running slot = restart in place)
calfcord mcp stop <server>       # take it offline
calfcord mcp restart <server>    # reload after editing its mcp.json entry
calfcord mcp start --all         # the "re-pick up mcp.json" sweep — see below
calfcord mcp stop --all          # stop every RUNNING mcp- slot on this host
calfcord mcp restart --all       # restart every RUNNING mcp- slot on this host
```

A few semantics to keep straight:

- **`start` of a running slot restarts it in place.** That is also the
  documented way to re-apply an edited `mcp.json` entry for one server
  (`restart` does the same).
- **`start --all` is the re-pick-up command.** It sweeps every *configured*
  server in `mcp.json` and (re)starts each — the way to pick up newly added
  entries across the board. `stop --all` / `restart --all` instead operate on
  the *running* `mcp-` slots (they act on what exists, not what is configured).
- **A server added after `calfcord start` needs a one-time workspace reload.**
  The supervisor config is derived from `mcp.json` at `start`, so a server you
  added afterward has no declared slot. `calfcord mcp start <server>` detects
  this (a `4xx` from the supervisor) and prints the reload hint:
  `calfcord stop && calfcord start`. After that one reload, the slot exists and
  the ordinary verbs apply. Removing a server is the mirror image — the slot
  disappears on the next reload.

## Distributed deployments

The secrets boundary is what makes MCP clean to split across hosts: only the
`mcp-<server>` processes read `mcp.json` and hold the credentials. Agents
resolve from the broker's capability view, so an agent host needs **no**
`mcp.json` and **no** MCP secrets.

So a natural split is to run the MCP servers (with their credentials) on one
host and the agents elsewhere:

```bash
# On the MCP host — where mcp.json and its secrets live:
calfcord mcp add github --command "npx -y @modelcontextprotocol/server-github" --env GITHUB_TOKEN
calfcord stop && calfcord start     # declare the new mcp-github slot
calfcord mcp start github

# On an agent host — no mcp.json, no GITHUB_TOKEN, just the selector in the .md:
calfcord agent restart scribe       # scribe's tools: includes mcp/github
```

The capability view even surfaces servers *other* hosts run, so
`calfcord agent tools` on the agent host can still offer the github server's
live per-tool rows. The toolbox is an ordinary calfkit node, so two hosts
running the same server are competing consumers on its dispatch topic — a
legitimate scale-out, not a split-brain (unlike duplicate agents). See
[`distributed-deployment.md`](./distributed-deployment.md) for the broader
multi-host story.

`calfcord deploy k8s` renders one `Deployment` per configured server
(`calfkit-mcp <server>`). `calfcord deploy docker` does **not** yet cover MCP
servers — run those on a native or systemd host alongside a Docker broker, or
add the services to your compose file by hand.

> The `mcp.capabilities` topic is a compacted control-plane topic
> (`cleanup.policy=compact`). On a native install with provisioning enabled,
> calfkit's `ensure_topic` creates it. On a production broker that disallows
> auto-creation, create it out of band with the compact cleanup policy.

## Troubleshooting

**A server won't start.** Check the toolbox's own log — the connection failure
(bad command, unreachable URL, auth rejected by the server) is there:

```bash
calfcord logs mcp-<server>
```

A toolbox whose server is unreachable fails its worker at boot by design, so
the supervisor will keep retrying it; the log names the cause.

**`calfcord mcp start <server>` prints a workspace-reload hint.** The server
isn't a declared slot yet (you added it after `calfcord start`). Run
`calfcord stop && calfcord start` once, then start it. See
[Lifecycle and reload](#lifecycle-and-reload).

**An agent isn't seeing the tools.** Walk the chain:

1. Is the server running and advertising? `calfcord mcp list` shows running
   state; `calfcord logs mcp-<server>` confirms it listed tools and advertised.
   If it's down, the agent degrades that turn silently — start the server.
2. Does the agent's `.md` actually declare the selector, and did you restart
   the agent after adding it? A new `mcp/...` line needs
   `calfcord agent restart <name>` ([step 4](#4-restart-the-agent)).
3. For an `mcp/<server>/<tool>` selector, does the server advertise a tool by
   exactly that name? The LLM-facing name is the server's own — check the
   toolbox log's tool listing.

**`environment variable 'X' (referenced as '$X') is not set`.** A `$VAR` in
`mcp.json` has no value in the server process's environment. Set it in
`config/.env` (the file the runner loads) and restart the server.

**`invalid MCP server name`.** The name (in `mcp.json` or in a selector)
violates `[a-z0-9_]{1,64}`. Rename the server — the name is also a Kafka topic
segment and a process-slot suffix, so the constraint is hard.

## See also

- [`design/mcp-reintroduction.md`](./design/mcp-reintroduction.md) — the full
  design and rationale (isolation, secrets boundary, runtime discovery).
- [`authoring-agents.md`](./authoring-agents.md#33-tools-optional) — the
  `tools:` frontmatter field, including MCP selectors.
- [`architecture.md`](./architecture.md#the-four-processes) — where the
  `calfkit-mcp` process sits.
- [`configuration.md`](./configuration.md#mcp-servers-mcpjson) — `mcp.json`
  location and the `CALFCORD_MCP_CONFIG` override.
- [`security.md`](./security.md#51-secrets) — handling MCP credentials.
