# mcp.json uses the Cursor/Claude-Code schema and allows literal secrets

`mcp.json` adopts the `{"mcpServers": {...}}` shape Cursor and Claude Code use
(stdio `command/args/env/cwd`, HTTP `type/url/headers`) so operators can paste
entries straight from upstream MCP-server docs, and **literal secret values
are legal** — a deliberate reversal of the pre-removal policy, which refused
any value without a `$VAR` reference unless `--allow-literal` was passed.
The file sits next to `config/.env` at mode 0600 (the same trust level), so
strict `$VAR`-only bought friction without a real boundary; `$VAR`/`${VAR}`
expansion is still supported and the `mcp add` wizard nudges secrets toward it.

## Considered Options

- **Strict `$VAR`-only** (the old policy): refuses pasted upstream configs and
  adds an escape-hatch flag, while the secret still lands on the same host in
  `config/.env`. Rejected as security theater at this trust boundary.
- **Literals allowed + `$VAR` supported + nudge** (chosen): paste-compatible
  with the tools users copy from; the real boundary is *which process reads
  the file* (see ADR-0002), not the file's syntax.

## Consequences

- Expansion happens at load time in the server process only; an unset
  reference fails that one server's boot loudly (and since the simplify pass,
  a sibling entry's unset secret cannot fail an unrelated server —
  `load_one_server` expands only the selected entry).
- Entry key sets are closed (a typo like `"evn"` fails loud), and `"type":
  "sse"` is rejected with a pointer to `"type": "http"` — strictness moved
  from secrets policy to shape validation, where it prevents real failures.
