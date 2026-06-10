# One process per MCP server, not a singleton MCP host

Each `mcp.json` server runs as its own roster slot (`mcp-<server>` →
`calfkit-mcp <server>`), unlike `tools` and `router` which are singleton
components — and unlike calfcord's own pre-removal MCP design, which hosted
every server on one `calfkit-mcp` worker. calfkit 0.9.0's `MCPToolbox` fails
its hosting Worker at boot when its server is unreachable, and MCP entries are
operator-supplied commands/URLs (the most misconfiguration-prone config in the
system), so a singleton would let one typo'd entry take down every MCP tool.

## Considered Options

- **Singleton `mcp` component** (the pre-removal design; matches the
  `tools`/`router` pattern): simpler wiring, but no failure isolation and
  whole-registry restarts on any edit. Rejected for the boot-failure coupling.
- **Per-server processes** (chosen): one bad entry can't touch siblings, each
  entry reloads independently (`mcp restart <server>`), and the lifecycle
  surface mirrors the agent roster operators already know.

## Consequences

- calfcord is **five** process types, not four (encoded in CLAUDE.md,
  `explain topology`, `architecture.md`).
- A server added to `mcp.json` after `calfcord start` has no declared
  supervisor slot and needs one `calfcord stop && calfcord start` — the same
  constraint as a brand-new agent `.md`; every CLI surface prints this hint.
- A permanently-broken entry crash-loops its own slot on the roster's
  `on_failure` + unlimited-restart policy (loud and observable, same as
  agents) rather than parking.

Do not "simplify" this back to one MCP host process; the isolation is the
point. Design: `docs/design/mcp-reintroduction.md` §D1.
