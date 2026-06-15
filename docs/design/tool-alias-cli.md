# `calfcord tools alias` — Operator-Managed Tool Aliases

**Status:** Finalized for review — no code written yet.
**Scope:** A first-class CLI to manage `CALFCORD_TOOLS_ALIAS` (multi-host tool
aliasing), mirroring the `calfcord mcp add/list/remove` idiom. Persists to the
install `.env`; **no runtime change** — `apply_deploy_filters` already consumes
the env var.
**Decision record:** [ADR-0007](../adr/0007-tool-alias-cli-config.md).

## 1. Summary & motivation

Tool aliasing — exposing one tool body under a second wire name for multi-host
routing (e.g. `terminal` → `terminal_eu`, so an agent can route a call to a
specific host) — is currently only settable by hand-editing the
`CALFCORD_TOOLS_ALIAS` env var. This feature adds a validated CLI to manage it.
The CLI is a validated editor of that one `.env` line, which every role already
reads at boot.

## 2. Conceptual model (the load-bearing decisions)

- **Aliases are install-level config, not a launch flag.** They persist, are
  inspectable, and are consumed by *multiple* roles (the tools host *serves*
  `terminal_eu`; the agent host *advertises* it to the LLM). So they live in the
  install `.env`, and the lifecycle verbs (`start`/`restart`) just read them —
  exactly how `mcp` config (`mcp.json`) pairs with `mcp start`. A transient flag
  on `tools start` was rejected: it would be restart-lossy, force re-typing, and
  configure only one of the two roles that need the name.
- **Two-sided, solved by construction.** Writing to the shared install `.env`
  means both the local tools slot and the agent slot pick the alias up — the
  operator configures one place, not two.
- **Alias ≠ include.** This command manages *only* `CALFCORD_TOOLS_ALIAS` (an
  additive clone, safe for all roles). It never touches `CALFCORD_TOOLS_INCLUDE`
  (per-process narrowing that would cripple an agent host if globalized into the
  shared `.env`). Narrowing stays a per-host / deploy concern.

## 3. CLI surface

```
calfcord tools alias add <src> <dst> [--restart]
calfcord tools alias list
calfcord tools alias remove <dst>    [--restart]
```

- `add <src> <dst>` — clone tool `<src>` under the new name `<dst>`. Both
  positional, required.
- `list` — print configured aliases.
- `remove <dst>` — remove the alias whose *new name* is `<dst>` (targets are
  unique, so this is unambiguous).
- `--restart` (on `add` / `remove`) — if a workspace is running, restart the
  affected slots so the change applies immediately; otherwise print that it
  applies on next start. Default (omitted) = print guidance only.

Group help: `tools alias` → "Manage tool aliases (`CALFCORD_TOOLS_ALIAS`)."

## 4. Per-subcommand behaviour

### `add <src> <dst>`
1. Resolve the install `.env` path (§7).
2. Read current `CALFCORD_TOOLS_ALIAS`, parse to `{src: dst}`.
3. **Validate** (§5). On failure: print `error: …`, return **1**, write nothing.
4. Insert `{src: dst}`, re-serialize sorted (`a=b,c=d`),
   `upsert(env_path, {"CALFCORD_TOOLS_ALIAS": csv})`.
5. Print `aliased 'terminal' → 'terminal_eu' in <env_path>` + the restart
   guidance (§9). Return **0**.
- **Idempotent:** re-adding the identical `src=dst` is a no-op success.

### `list`
- Print one `src  →  dst` row per configured alias, sorted.
- Empty → `no tool aliases configured in <env_path>; add one with`
  `` `calfcord tools alias add <tool> <new-name>` ``. Return **0**.

### `remove <dst>`
- If an alias with target `<dst>` exists: drop it, re-serialize (write
  `CALFCORD_TOOLS_ALIAS=` when none remain — the parser treats empty as `{}`),
  `upsert`. Print `removed alias 'terminal_eu' (was 'terminal' → 'terminal_eu')`
  + restart guidance. Return **0**.
- Not found → `error: no alias 'terminal_eu' configured` (list current ones).
  Return **1**.

## 5. Validation rules (`add`)

Reuse `TOOL_NAME_REGEX` and the canonical, env-independent `ALL_TOOLS` surface.
Each violation → a distinct `error:` message + exit 1, nothing written:

1. **`src` is a real tool** — `src ∈ {n.tool_schema.name for n in ALL_TOOLS}`,
   else error listing valid tools.
2. **`src` is aliasable** — reject if `<src>` registers node-scoped resources or
   lifecycle hooks (today: `todo`, `private_chat`). The runtime's
   `_clone_with_name` already raises on these (a clone can't safely share a
   node-scoped `@resource`); the pre-check surfaces it at `add` time instead of
   as a tools-host crash on the next `start`. The check is structural ("does
   this node carry node-scoped lifecycle state?"), not a hardcoded list, so a
   future stateful tool is auto-rejected too.
   Message: `error: tool 'todo' can't be aliased (it holds per-session state);`
   `aliasing is for stateless tools like terminal/search_files/web_*`.
3. **`dst` is a valid name** — matches `TOOL_NAME_REGEX`.
4. **`dst` ≠ `src`** — no self-alias.
5. **`dst` doesn't collide with a real tool** — `dst ∉ ALL_TOOLS` names.
6. **`dst` doesn't collide with an existing alias target.**
7. **`src` isn't already aliased** — one alias per source (to change it,
   `remove` then `add`).

These mirror the strict rules the runtime parser (`_resolve_alias_map`) enforces,
so CLI and runtime behaviour stay identical.

## 6. Validation reuse / factoring

- Factor the CSV grammar out of `_resolve_alias_map` into pure
  `parse_alias_csv(raw) -> dict[str, str]` + `serialize_alias_map(d) -> str` in
  `tools/deploy_filters.py`; both the runtime and the CLI use them (single source
  of truth for the `src=dst,…` grammar).
- Add `validate_alias(src, dst, *, tool_names, aliasable_names, existing)`
  (raises `ValueError`), reusing `TOOL_NAME_REGEX`.

## 7. Persistence & path resolution

- **Store:** the `CALFCORD_TOOLS_ALIAS` key in the install `.env`, via
  `_envfile.read_env` (read) + `_envfile.upsert` (atomic, in-place, preserves
  other keys/comments, `chmod 0600`).
- **Path:** `_resolve_home()` → `init.resolve_paths(home)` → `env_path`. Works in
  **both** a native install (`$CALFCORD_HOME/config/.env`) and a dev tree (repo
  `.env`). Unlike `calfcord deploy`, this does **not** require a native install —
  it only edits `.env`, which exists in dev too.

## 8. Data flow (why there is no runtime change)

`apply_deploy_filters(ALL_TOOLS)` runs at import of `calfcord.tools` and reads
`CALFCORD_TOOLS_ALIAS` from `os.environ`. `uv run --env-file config/.env` (and
compose `env_file:` / `-e`) load `.env` into the real process env *before* that
import. So writing the `.env` line is sufficient; the alias takes effect on the
next process boot of every role. No edit to `deploy_filters` / `runner`.

## 9. Restart / apply semantics

`.env` is read at boot, so a change needs the tools host (to serve the new name)
**and** the agents (to advertise it) to restart.

- **Default (no `--restart`):** print exact guidance —
  ``run `calfcord tools restart` and `calfcord agent restart --all` to apply``
  ``(or `calfcord stop && calfcord start`)``.
- **`--restart`:** if `workspace_is_up()` (reuse `supervisor._workspace`),
  invoke the existing component-restart paths (`_run_component("tools",
  "restart")` + the agent-roster `restart --all`) and print their results. If the
  workspace isn't running, print `workspace not running; the alias applies on
  next start` (no-op, exit 0).

## 10. Multi-host & deploy propagation

- **Multi-host:** run `calfcord tools alias add` on each host (writes that host's
  `.env`), or let deploy carry it.
- **Deploy:** `calfcord deploy k8s` builds the `Secret` `--from-env-file=.env`
  (consumed via `envFrom`), and `docker` uses `env_file=.env` — so
  `CALFCORD_TOOLS_ALIAS` already propagates to all role workloads. **No
  `deploy.py` change**; covered by a verification test.

## 11. Module layout & files touched

- `cli/main.py` — add an `alias` subparser under the existing `tools` group
  (`tools_sub.add_parser("alias")` → `alias_sub` with `add`/`list`/`remove`, dest
  `tools_alias_command`; `--restart` on add/remove). Branch the dispatch: `if
  args.tools_command == "alias": return _run_tool_alias(args)` else the existing
  `_run_component(...)`. Add `_run_tool_alias(args)` (resolves `env_path`,
  dispatches).
- `cli/tool_aliases.py` — **NEW**, mirrors `mcp_admin.py`: `run_alias_add`,
  `run_alias_list`, `run_alias_remove`.
- `tools/deploy_filters.py` — add `parse_alias_csv` / `serialize_alias_map` /
  `validate_alias` (factored), reused by `_resolve_alias_map` and the CLI.

## 12. Edge cases

- Empty/whitespace `CALFCORD_TOOLS_ALIAS` already present → parsed as `{}`.
- Last alias removed → write `CALFCORD_TOOLS_ALIAS=` (empty), not a deleted line
  (keeps the key greppable; runtime treats empty as none).
- Malformed existing value (hand-edited badly) → `list`/`add`/`remove` surface a
  clear `error:` (reuse the strict parser's messages) rather than silently drop.
- `--restart` with no workspace → graceful hint, exit 0.
- Trailing/double commas in an existing value → tolerated by `parse_alias_csv`.

## 13. Testing (TDD)

- `tests/cli/test_tool_aliases.py` — add/list/remove happy paths; every
  validation error (unknown src, non-aliasable src, bad dst regex, self-alias,
  dst↔tool collision, dst↔alias collision, src already aliased); idempotent
  re-add; remove-nonexistent (exit 1); empty-list message; `.env` round-trip
  (preserves other keys; empty value on last remove); `--restart` gated on
  `workspace_is_up` (mock both branches).
- `tests/tools/test_deploy_filters.py` — unit tests for `parse_alias_csv` /
  `serialize_alias_map` / `validate_alias`.
- `tests/cli/test_main.py` — `tools alias *` dispatch wiring.
- `tests/cli/test_deploy.py` — the alias rides `.env` → manifest
  (`envFrom`/`env_file`).
- `/pytest-coverage` to 100% on new code.

## 14. Docs & ADR

- `docs/distributed-deployment.md` §3 — show `calfcord tools alias add patch
  patch_eu` as the managed way to set the alias (alongside the raw env var).
- `docs/configuration.md` — note `CALFCORD_TOOLS_ALIAS` is CLI-managed.
- `docs/using-calfcord.md` — add to the command reference.
- [ADR-0007](../adr/0007-tool-alias-cli-config.md).

## 15. Non-goals / out of scope

- Managing `CALFCORD_TOOLS_INCLUDE` (per-process narrowing — different lifecycle).
- Auto-restart by default (opt-in `--restart` only).
- Multi-region / one-source-to-many-targets / alias chains (runtime doesn't
  support; the CLI enforces the same limits).
- Per-clone tool-description override (runtime v1 limitation).
- Editing *remote* hosts' `.env` (run per host, or let `deploy` carry it).

## 16. Phased build (TDD)

1. **Factoring + validator** — `parse_alias_csv`/`serialize_alias_map`/
   `validate_alias` in `deploy_filters.py`; `_resolve_alias_map` switches to the
   shared parser.
2. **CLI handlers** — `cli/tool_aliases.py` (`add`/`list`/`remove`) over the
   install `.env`.
3. **Parser + dispatch** — `tools alias` subgroup in `cli/main.py` +
   `_run_tool_alias`.
4. **Restart UX** — guidance + opt-in `--restart` (gated on `workspace_is_up`).
5. **Deploy propagation** — verification test.
6. **Docs + ADR-0007.**
7. **Review to convergence** — sub-agent fan-out.
