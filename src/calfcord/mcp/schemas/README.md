# MCP tool schemas (generated — do not hand-edit)

This directory holds **committed** MCP tool schema modules, one per MCP
server. Each file is the output of:

```sh
calfkit mcp codegen <server> -o src/calfcord/mcp/schemas/<server>.py
```

Conventions enforced by discovery
(`calfcord.mcp.discovery.discover_mcp_catalog`):

- **Module name == server name.** `schemas/gmail.py` describes the
  `gmail` server, which is what a `mcp/gmail` (or `mcp/gmail/search`)
  selector in an agent's frontmatter resolves against.
- **One `McpToolDef` constant per tool.** The generator emits top-level
  `NAME = McpToolDef(...)` constants (plus a class wrapper that discovery
  ignores). Discovery collects the top-level instances directly.
- Underscore-prefixed modules and attributes are skipped (private support
  code), mirroring the builtin-tool discovery walk.

## Regenerating

These files are checked in so the **agent** deployment can build its tool
catalog without any MCP transport or credentials. Regenerate whenever an
upstream server's tool surface changes, and verify in CI with `--check`:

```sh
calfkit mcp codegen <server> -o src/calfcord/mcp/schemas/<server>.py --check
```

**Do not hand-edit generated files** — manual changes are lost on the next
codegen run and can desync the committed schema from the live server.
