# Tool aliases are install config managed by `calfcord tools alias`

**Status:** accepted

Tool aliasing (`CALFCORD_TOOLS_ALIAS`, e.g. `terminal`→`terminal_eu` for
multi-host routing) is managed by a `calfcord tools alias add/list/remove`
subcommand that edits the install `.env` — **not** by a flag on a lifecycle
verb. An alias is persistent, inspectable config consumed by *multiple* roles at
boot (the tools host serves the aliased name; the agent host advertises it), so
it belongs with config, the way `calfcord mcp` config (`mcp.json`) pairs with
`mcp start`. Full spec:
[`docs/design/tool-alias-cli.md`](../design/tool-alias-cli.md).

## Decisions

1. **Install config, read by all roles — not a launch flag.** The CLI is a
   validated editor of the `CALFCORD_TOOLS_ALIAS` line in the install `.env`
   (via `_envfile.upsert`). `apply_deploy_filters` already reads that env var at
   boot, so there is **no runtime change**. Writing the shared `.env` configures
   both the tools and agent slots on a host at once (the alias is inherently
   two-sided).
2. **Alias, not include.** The command manages only `CALFCORD_TOOLS_ALIAS` (an
   additive clone, safe for every role). It never touches
   `CALFCORD_TOOLS_INCLUDE` — that is per-process narrowing, and globalising it
   into the shared `.env` would hide tools from every agent on the host.
   Narrowing stays a per-host / deploy concern.
3. **Pre-validate aliasability.** `add` rejects aliasing a tool that registers
   node-scoped resources / lifecycle hooks (today `todo`, `private_chat`). The
   runtime's `_clone_with_name` already fails closed on these
   ([ADR-0005](0005-adopt-calfkit-tools-explicit-composition.md)) — a clone can't
   safely share a node-scoped `@resource`, and aliasing them is meaningless
   (single-host, stateful by nature). The CLI surfaces the rejection at `add`
   time (a clear error) instead of as a tools-host crash on the next `start`.
   The check is structural, so a future stateful tool is auto-rejected.

## Considered and rejected

- **A `--alias SRC=DST` flag on `calfcord tools start`.** Reads nicely but models
  persistent, two-sided config as an ephemeral arg: restart-lossy (vanishes on
  the next `restart` unless re-typed), meaningless on a running slot, and it
  configures only the tools host — leaving the agent host unaware of the name, so
  the LLM still can't call it. Rejected for the config-subcommand above.

## Consequences

- No code change to the tool runtime; the feature is a CLI + `.env` editor.
- Multi-host: run `calfcord tools alias add` per host, or let `calfcord deploy`
  carry it (the alias rides the `.env`→`envFrom`/`env_file` path already).
- The deleted packaging `--rename` flag (ADR-0006) is *not* resurrected; this
  CLI is the single alias surface, so "alias" is the one term.
