# Adopt vendored `calfkit-tools`; compose the surface explicitly

**Status:** accepted (the `private_chat` retention below is superseded by ADR 0011)

We replaced calfcord's hand-rolled builtin tools (`shell`, `read_file`,
`write_file`, `edit_file`, `grep`, `glob`, `web_fetch`, `web_search`,
`todo_view`, `todo_write` — thin wrappers over `openhands-tools` and
`smolagents`) with the vendored **`calfkit-tools`** package's calfkit tool
nodes: `terminal`, `process`, `read_file`, `write_file`, `patch`,
`search_files`, `todo`, `execute_code`, `web_search`, `web_extract`, and
`web_fetch`. This is a hard break: tool names and call shapes change, with no
compatibility shims.

> **Superseded (ADR 0011):** this ADR originally retained the first-party
> `private_chat` tool for agent-to-agent A2A. The calfkit-012 migration deletes
> `private_chat` in favour of calfkit's native `message_agent` + handoff, so the
> `ALL_TOOLS` surface is now exactly the vendored `calfkit-tools` nodes above.

## Why

- **Multi-tenancy.** The old wrappers were inconsistent and leaky: `shell`
  discarded `ctx` entirely, so every agent shared one tmux session, cwd, and
  env. The vendored nodes key all stateful state by the calling agent's
  identity (`session_key = f"{agent_name}:{deps.get('session_id','default')}"`,
  `agent_name` stamped from the unspoofable `x-calf-emitter` header) and fail
  closed when it is absent. Each agent gets its own session out of the box.
- **Less code we own.** The generic tool wrappers, the `openhands-tools` and
  `smolagents` dependencies, and their transitive weight are gone.

## Decisions

1. **Explicit composition, not auto-discovery.** The previous registry was
   built by a filesystem walk of `tools/builtin/` (`discover_tools`). That
   walk existed to remove double-registration friction when there were many
   first-party tool files — a premise that no longer holds (one first-party
   tool remains). The surface is now an explicit list, `ALL_TOOLS`, in
   `calfcord/tools/__init__.py`, narrowed/aliased by a pure transform
   (`deploy_filters.apply_deploy_filters`). **This list is the security
   boundary**: `terminal` and `execute_code` run arbitrary code on the tools
   host, so what agents can reach must be a reviewable, local decision — never
   an artifact of which installed package version publishes what. For the same
   reason we import the hermes nodes by name rather than spreading the
   package's `HERMES_NODES`, and we rejected `importlib.metadata` entry-point
   plugin discovery (it would let merely installing a package arm a tool). A
   drift-guard test fails CI if our list and the package's published set
   diverge.
2. **Agent-lifetime tenancy by default.** `session_id` is left unset, so each
   agent's tools state persists across its turns and is isolated from other
   agents. No bridge/`deps` wiring was added. Finer per-conversation scope
   remains available later by wiring a Discord thread/channel id into
   `deps["session_id"]`.
3. **`execute_code` is included.** It is the same trust class as `terminal`
   (already shipped). Operators gate it per-deployment via
   `CALFCORD_TOOLS_INCLUDE`.
4. **Shared workspace via `TERMINAL_CWD`.** The hermes local backend starts
   each session's shell in `TERMINAL_CWD` (falling back to the process cwd).
   The tools runner sets it to the calfcord workspace root (resolved from
   `CALFCORD_WORKSPACE_DIR`), so every agent's session starts in the shared,
   bind-mountable workspace while keeping per-agent isolation. This replaced
   the deleted `workspace.py` helper; an explicit operator-set `TERMINAL_CWD`
   wins.

## Consequences

- `calfkit-tools` pins `calfkit>=0.9.0,<0.11`; co-installs with calfcord's
  `calfkit~=0.10.0` (the node code uses only `agent_tool`/`ToolNodeDef`/
  `ToolContext`/`.resource()`, unchanged across 0.9→0.10).
- Tool state is in-memory and lost on a tools-process restart (as the old
  `shell`/`todo` were). Stateful nodes are correct at **one tools-process
  replica**; per-tool images can split stateful tools onto their own
  single-replica hosts via `CALFCORD_TOOLS_INCLUDE`.
- `tmux` is no longer needed (the hermes terminal uses bash + a PTY);
  `ripgrep` is still needed (for `search_files`).
- `CALFCORD_TOOLS_ALIAS` (multi-host rename) is retained as a pure clone in
  `deploy_filters`.
