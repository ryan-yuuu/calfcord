# MCP Server Tools

How calfcord agents reach tools hosted by external [Model Context
Protocol](https://modelcontextprotocol.io) (MCP) servers — Gmail, Google
Drive, a database gateway, anything that speaks MCP — and how an operator
wires a new server in. This is the companion to
[`authoring-tools.md`](./authoring-tools.md) (the in-repo `@agent_tool`
builtins) and [`authoring-agents.md`](./authoring-agents.md) (the agent
`tools:` field). Read those first; this doc only covers what's MCP-specific.

The MCP integration is built around one hard constraint: **the agent
deployment never holds an MCP credential and never opens an MCP
connection.** Everything below follows from that split.

## 1. What it is

An MCP server exposes a set of tools over a transport — a stdio subprocess
(launched with a command like `npx -y @some/mcp-server`) or a Streamable
HTTP endpoint. calfcord lets an agent advertise those tools to its LLM by
naming them in the ordinary frontmatter `tools:` list, alongside the
builtins:

```yaml
tools:
  - shell                 # builtin (unchanged)
  - read_file             # builtin (unchanged)
  - mcp/gmail             # ALL tools from the "gmail" MCP server
  - mcp/drive/search      # ONE tool ("search") from the "drive" server
```

To the agent's LLM, each selected MCP tool appears under a flattened name,
`<server>_<tool>` — `mcp/gmail` exposing a `search` tool advertises as
`gmail_search`; `mcp/drive/search` advertises as `drive_search`. Builtin
names are untouched (`shell`, `read_file`, …). The flattening keeps the
server's namespace explicit in every name the LLM and the audit trail see,
so two servers can both export a `search` tool without collision.

When the LLM calls `gmail_search`, the call travels the same Kafka path
every calfcord tool call travels — but it terminates at a *different*
process than the builtins do. That process, the **MCP bridge**, is the only
thing in the system that ever talks the MCP wire protocol.

## 2. Agent-facing syntax

The `tools:` entry forms, resolved at agent boot:

| Selector              | Meaning                                                      | LLM-facing name(s)        |
| --------------------- | ------------------------------------------------------------ | ------------------------- |
| `mcp/<server>`        | Every tool the `<server>` schema module advertises.          | `<server>_<tool>` for each |
| `mcp/<server>/<tool>` | One specific tool. `<tool>` is the **raw** MCP tool name and may contain hyphens (`mcp/gmail/list-labels`). | `<server>_<tool>`, the raw tool name appended **verbatim** — a hyphen is kept, so `mcp/gmail/list-labels` → `gmail_list-labels` |

A few rules worth internalizing:

- **`<server>` is the schema module name**, which equals the key in
  `mcp.json` (see §5). It is *not* arbitrary — it must resolve against
  the committed catalog.
- **`<tool>` is the raw MCP tool name**, exactly as the server advertises
  it over the protocol. It is the name you see in the generated schema
  module, and it is allowed to contain hyphens. The `<server>_<tool>`
  flattening is what the LLM sees; the bridge dispatches on the raw name.
- **Selectors and builtins coexist in one list.** There is no separate
  `mcp_tools:` field — everything is one `tools:` array.
- **Order doesn't matter.** Whether `mcp/gmail` precedes or follows
  `shell` has no effect.

### The "all builtins + some MCP" limitation

This is the one sharp edge, so it gets its own callout. The `tools:` field
has exactly one default-expansion behavior:

- **Omit `tools:` entirely** → the agent gets EVERY registered builtin and
  **NO MCP tools**. The default expansion is builtins-only by design; the
  agent process has no reason to advertise MCP schemas the author didn't
  ask for.

There is **no shorthand for "all builtins PLUS one MCP tool."** The moment
you add a single `mcp/...` selector you must write an explicit list, and an
explicit list turns off the builtins-default — you now get exactly what you
typed. To run an agent with most builtins and a couple of MCP tools, spell
the builtins out:

```yaml
# WANT: all-ish builtins plus Gmail search. There is no "all + mcp" form;
# list the builtins you actually want alongside the selector.
tools:
  - shell
  - read_file
  - write_file
  - edit_file
  - grep
  - glob
  - web_fetch
  - web_search
  - todo_view
  - todo_write
  - private_chat
  - mcp/gmail/search
```

(If this list feels tedious, that's the intended friction: an agent that
reaches an external service usually wants a *narrower* tool surface, not a
wider one. Treat the explicit list as a prompt to prune.)

### Routers declare no tools

The singleton ambient-routing agent (`role: router`, built in code from a
bundled `router.md`; see [`ambient-routing.md`](./ambient-routing.md))
declares **no tools at all** — no builtins and therefore no `mcp/...`
selectors. Routers only emit a structured routing decision; they never call
a tool. Don't put MCP selectors on a router.

## 3. The two-deployment model

MCP tools split calfcord's existing tool path into two independently
deployable halves. The familiar builtins continue to run in
`calfkit-tools`; MCP tools run in a new process. Both answer over Kafka, so
the agent can't tell — and doesn't need to know — where a given tool lives.

### `calfkit-agent` — schema-only, credential-free

The agent deployment imports **only the committed MCP schema files** plus a
name→schema catalog (`MCP_CATALOG`). For every `mcp/...` selector an agent
declares, the agent process:

1. looks the server/tool up in the catalog,
2. advertises the matching tool schema to the LLM under `<server>_<tool>`,
   and
3. when the LLM calls it, publishes the call to Kafka topic
   `mcp.<server>.<tool>.input` and awaits the result on
   `mcp.<server>.<tool>.output`.

That is the *entire* extent of the agent's involvement. It **never spawns
an MCP subprocess, never opens an HTTP connection to an MCP server, and
never reads an MCP credential.** The schema modules are committed, static
Python — importing them touches no network and needs no secret. This is the
same isolation guarantee that keeps `calfkit-tools` registry-free (see
[`architecture.md`](./architecture.md#decoupled-deployment)), extended to
MCP: the deployment that talks to an LLM holds nothing it doesn't need.

### `calfkit-mcp` — the MCP bridge

The new **MCP bridge** deployment (`calfkit-mcp`, entry point
`calfcord.mcp.runner:main`) is the half that does the real work. It reads
the server declarations from `mcp.json` (via `calfcord.mcp.config.load_mcp_servers`), and for each one:

- spawns the MCP subprocess over stdio (for `--command` servers), or opens
  the Streamable HTTP connection (for `--url` servers), and
- subscribes to the `mcp.<server>.<tool>.input` topics, forwards each call
  into the live MCP session, and publishes the result to
  `mcp.<server>.<tool>.output`.

The bridge is the only process that holds MCP transport and credentials.
Secrets reach it from its own environment via calfkit's `$VAR` expansion
(see §6) — they are **never committed** to the repo and never travel to the
agent.

```
agent process (LLM picks gmail_search, emits Call)
        │  imports committed schema only — no MCP connection, no secret
        ▼  Kafka topic  mcp.gmail.search.input
calfkit-mcp bridge (live MCP session: stdio subprocess or HTTP)
        │  holds the MCP transport + $VAR-expanded credentials
        ▼  Kafka topic  mcp.gmail.search.output
agent process (LLM sees the tool result string)
```

### Isolation guarantee, restated

Put bluntly: an attacker who compromises a `calfkit-agent` container gets
the LLM provider key and whatever the builtins can reach — but **not** the
Gmail OAuth token, the database password, or any other MCP credential.
Those live only on the `calfkit-mcp` host. Conversely, the bridge never
runs LLM inference and never reads `agents/*.md`. The blast radius of each
half is strictly smaller than a combined process would be. See
[`security.md`](./security.md) for the broader threat model this slots
into.

## 4. Codegen workflow

The agent half advertises tool schemas it cannot fetch at runtime (it has
no MCP connection), so the schemas are **generated ahead of time and
committed** as per-server Python modules under
`src/calfcord/mcp/schemas/`. One module per server; the **module name
equals the server name** (`schemas/gmail.py` → server `gmail`).

Generate (or regenerate) a module with **`calfcord-mcp-codegen`**. It
connects to the server once, enumerates its tools, writes the module to the
right place, and verifies it registers:

```bash
# stdio server (launched as a subprocess):
uv run calfcord-mcp-codegen gmail \
  --command "npx -y @some-org/gmail-mcp-server"

# Streamable HTTP server:
uv run calfcord-mcp-codegen drive \
  --url https://mcp.example.com/drive \
  --token "$DRIVE_MCP_TOKEN"
```

There is **no `-o`**: the wrapper computes the output path
(`src/calfcord/mcp/schemas/<server>.py`) from the server name, so the module
always lands where discovery looks. It also validates the name against the
selector grammar *before* connecting, keeps the generated class name and the
filename in sync, and after writing re-runs discovery to confirm the server
actually registered (warning if it didn't — an empty/stale module, or a
digit-leading tool name).

Codegen requires the `mcp-codegen` extra, which is already declared in
`pyproject.toml` (`calfkit[mcp-codegen]`) — a plain `uv sync` pulls it in,
no extra install step.

Under the hood `calfcord-mcp-codegen` delegates to **`calfkit mcp codegen`**,
forwarding every flag verbatim (so new calfkit codegen options work without a
change here). Reach for the calfkit command directly only when you need to
write somewhere other than the discovery directory — then *you* own the
binding contract: the positional sets only the **generated class name**, and
the **`-o` filename** is what's load-bearing (discovery keys the catalog by
the module filename, which must equal the `mcp.json` server key (§5) and the
`<server>` segment agents type in their selectors), so the two can silently
diverge.

```bash
uv run calfkit mcp codegen gmail \
  --command "npx -y @some-org/gmail-mcp-server" \
  -o src/calfcord/mcp/schemas/gmail.py
```

**Commit the generated module.** It is the contract the agent deployment
reads at boot. A schema change (the upstream server added or renamed a
tool) is a code change like any other: regenerate, review the diff, commit.

### CI drift check

Because the committed schema can drift from the live server, codegen has a
`--check` mode that regenerates in-memory and compares against the
committed file, exiting non-zero on any difference (and writing nothing):

```bash
# In CI — fails the build if schemas/gmail.py is stale (--check is
# forwarded straight through to calfkit):
uv run calfcord-mcp-codegen gmail \
  --command "npx -y @some-org/gmail-mcp-server" \
  --check
```

Wire one `--check` invocation per server into CI. A red drift check means
the upstream server's tool surface changed under you — regenerate locally,
review what moved, and commit the refreshed module before the next deploy.
(Note that `--check` still connects to the live server, so CI needs the
same Node/`npx` and `$VAR` secrets the bridge needs — see §6.)

## 5. Declaring a server in `mcp.json`

The schema module is what the *agent* reads; `mcp.json` is what the
*bridge* reads. They are deliberately separate: the agent needs the static
schema, the bridge needs the live transport + secrets. Both are keyed by
the same server name.

Server declarations live in `mcp.json` at the repo root (override the path
with `CALFCORD_MCP_CONFIG`). They are **bridge-only** — the agent deployment
never reads this file. The bridge loads it via
`calfcord.mcp.config.load_mcp_servers`, which attaches each entry's committed
schema from `MCP_CATALOG`. The file uses the de-facto `mcpServers` format
(Claude Desktop / Cursor / Cline / Gemini CLI):

```json
{
  "mcpServers": {
    "gmail": {
      "command": "npx",
      "args": ["-y", "@some-org/gmail-mcp-server"],
      "env": { "GMAIL_OAUTH_TOKEN": "$GMAIL_OAUTH_TOKEN" }
    },
    "drive": {
      "type": "http",
      "url": "https://mcp.example.com/drive",
      "headers": { "Authorization": "Bearer $DRIVE_MCP_TOKEN" }
    }
  }
}
```

Things to note:

- **The server key ties back to the committed schema.** `load_mcp_servers`
  attaches `MCP_CATALOG["<server>"]` to each entry, so the bridge advertises
  and dispatches against the exact tool set the agents were built to see. A
  key with no committed schema module fails the load (codegen it first, §4).
- **The key *is* the server name.** calfkit derives the wire topics
  `mcp.<name>.<tool>.*` from it (the loader sets `name=<key>`), so the key
  must equal the schema module name and the `<server>` segment agents type in
  their selectors. There is no separate `name=` to keep in sync.
- **Secrets are `$VAR` references, not literals.** calfkit expands `$VAR`
  (and `${VAR}`) against the bridge's environment when it *parses* `mcp.json`
  at load time — so the file stays committable (no token ever lands in the
  repo) but an unset variable fails the load immediately (§6). HTTP bearer
  auth goes in a `headers` entry (`"Authorization": "Bearer $TOKEN"`); there
  is no separate `token` field. Set the real values in the bridge's
  environment.

### Generating the entry with `calfcord-mcp-add`

You can hand-write the entry above, or generate it with **`calfcord-mcp-add`**
— the bridge-side companion to `calfcord-mcp-codegen` (§4). Codegen writes the
*schema* the agent reads; this writes the *transport* entry the bridge reads.
Both are keyed by the same server name. Unlike codegen, this command **never
connects to the server** — the entry is built purely from the flags, so it is
offline and instant.

```bash
# stdio — the secret is referenced by env-var NAME and written as a $VAR
uv run calfcord-mcp-add gmail \
  --command "npx -y @some-org/gmail-mcp-server" \
  --env GMAIL_OAUTH_TOKEN

# HTTP — auth as a header whose value MUST contain a $VAR reference
uv run calfcord-mcp-add drive \
  --url https://mcp.example.com/drive \
  --header "Authorization=Bearer $DRIVE_MCP_TOKEN"
```

What it owns so you can't get it wrong:

- **It refuses to write a literal secret.** Every `--env` / `--header` value
  must carry a `$VAR` reference (the bullet above); a literal value is rejected
  unless you pass `--allow-literal` for a genuinely non-secret value (e.g.
  `--header "Content-Type=application/json"`). `--env NAME` is shorthand for
  `NAME=$NAME`; use `--env KEY=$HOST_VAR` when the subprocess key and the host
  variable differ.
- **It validates the entry** against calfkit's reference schema
  (`calfkit.mcp.mcp_json_schema()`) before writing — the *un-expanded* shape, so
  none of the `$VAR` secrets need to be set in your shell.
- **It merges, never clobbers.** The entry is added under `mcpServers`
  (creating `mcp.json` if absent, honoring `CALFCORD_MCP_CONFIG`); an existing
  server is left alone unless you pass `--force`. Use `--dry-run` to print the
  merged result without writing.
- **It warns if you haven't codegen'd the schema yet** — the inverse of
  codegen's verify, so the two-command pair stays in sync.

The server name is validated against the same selector grammar codegen enforces
(`[a-z0-9_]{1,64}`), so a typo fails here rather than as an `unknown server` at
bridge boot.

## 6. Required environment (bridge only)

The agent deployment needs **nothing new** for MCP — no Node, no MCP
secret. Everything below is the **bridge's** (`calfkit-mcp`) requirement:

- **A Node runtime / `npx`** for any stdio server launched with an `npx`
  command. The bridge spawns the subprocess; if `npx` isn't on its `$PATH`,
  the server fails to start. (Servers launched with a non-Node command
  obviously need whatever *that* command needs instead.)
- **Each server's declared `$VAR` secrets.** Whatever variables the
  `env` / `headers` fields in `mcp.json` reference must be present in the
  bridge's environment (its `.env`, the compose service's `environment:`
  block, or the container's runtime env). A missing variable makes
  `mcp.json` fail to parse and **crashes the bridge at boot** with
  `McpConfigError: environment variable 'X' is unset` (calfkit expands
  `$VAR` while parsing `mcp.json`, §5) — it does **not** wait for a
  connection attempt.

Keep these on the bridge host and nowhere else — that placement *is* the
isolation guarantee from §3.

## 7. Adding a new MCP server, end to end

1. **Codegen the schema module** into `src/calfcord/mcp/schemas/<server>.py`
   (§4), and commit it.
2. **Declare the server** in `mcp.json` — run `calfcord-mcp-add <server>
   --command/--url …` (or hand-write the entry) to add it under `mcpServers`
   with its command/url plus `$VAR` secret references (§5). The bridge
   attaches `MCP_CATALOG["<server>"]` automatically.
3. **Reference it from an agent** by adding `mcp/<server>` or
   `mcp/<server>/<tool>` to that agent's `tools:` list (§2). Restart
   `calfkit-bridge` and `calfkit-agent` so the agent registry re-reads the
   `.md`.
4. **Deploy the `calfkit-mcp` bridge** (or restart it if already running)
   with the server's `$VAR` secrets and, for stdio servers, a Node runtime
   in its environment (§6).

Steps 1–2 are a single reviewed commit; step 3 is an agent edit; step 4 is
an ops action. The agent edit and the bridge deploy are independent — an
agent that names `mcp/gmail` boots fine as long as the *schema* is
committed, even before the bridge is up; tool *calls* simply hang
(awaiting `mcp.gmail.*.output`) until the bridge is serving.

## 8. Troubleshooting

Lead with the symptom, as elsewhere in [`troubleshooting.md`](./troubleshooting.md).

- **Boot error: unknown MCP server.** An agent declares `mcp/<server>` for
  a `<server>` with no committed schema module. **Both** halves fail fast:
  the bridge validates every agent's selectors against the catalog at
  registry load (and usually starts first, per §7), and the agent process
  re-validates as it builds its tool surface — whichever boots first raises
  an error naming the bad server and listing the valid ones. Fix: run
  codegen for that server (§4) and commit the module, or correct the typo
  in the agent's `tools:`.
- **Boot error: unknown MCP tool.** An agent declares `mcp/<server>/<tool>`
  where `<server>` exists but `<tool>` isn't in its schema module. Same
  both-halves fast-fail (bridge at registry load, agent at build), with the
  valid tool names for that server listed. Fix: use one of the listed names
  (remember `<tool>` is the *raw* MCP name, hyphens and all), or regenerate
  the schema if the upstream server actually renamed the tool.
- **Boot error: malformed selector.** Something like `mcp/`, `mcp//search`,
  or `mcp/gmail/search/extra` — a selector that doesn't match
  `mcp/<server>` or `mcp/<server>/<tool>`. The selector *shape* is checked
  at frontmatter parse time, so **both** the bridge (which parses every
  agent's `.md` at registry load) and the agent process reject it with the
  expected forms. Fix: use exactly one or two segments after `mcp/`.
- **An MCP tool call hangs and never returns.** The agent advertised the
  schema (so the call published to `mcp.<server>.<tool>.input`) but no
  bridge is serving that topic. Fix: deploy/restart `calfkit-mcp` (§6).
  This is the expected behavior when the schema is committed but the bridge
  isn't up — the agent half intentionally has no way to know the bridge is
  missing.
- **Bridge can't start a stdio server.** The `calfkit-mcp` host is missing
  Node/`npx` (for `npx`-launched servers) or the command otherwise isn't on
  `$PATH`. Fix: install the runtime on the bridge host. The agent host
  never needs it.
- **Bridge crashes at boot: `McpConfigError: environment variable 'X' is
  unset`.** A `$VAR` secret referenced in `mcp.json` is **unset** in the
  bridge's environment. calfkit expands `$VAR` while parsing `mcp.json`
  (§5), so an unset variable fails the load before any connection is
  attempted — the bridge never starts. Fix: set the variable on the bridge
  (only) and restart.
- **Bridge connects but the server rejects auth.** A `$VAR` secret
  referenced in `mcp.json` is **set but wrong** in the bridge's
  environment. The value expanded fine at load, so the bridge boots
  and connects, but the server rejects the (bad) credential at connect time.
  Fix: set the correct value on the bridge (only) and reconnect. In both
  cases the agent deployment is uninvolved — it holds no MCP credentials by
  design.
- **CI drift check is red.** The committed `schemas/<server>.py` no longer
  matches the live server (a tool was added, removed, or its schema
  changed). Fix: regenerate locally (§4), review the diff, and commit the
  refreshed module.
- **Boot WARNING: `schemas supplied for servers not in mcp.json`.** Benign.
  The bridge attaches the whole committed catalog, so any server with a
  committed schema that *this* `mcp.json` doesn't host is named once at boot.
  Expected when a host deliberately serves a subset of the catalog (another
  bridge serves the rest) — it is **not** an error, and the bridge still hosts
  everything declared in `mcp.json`.
