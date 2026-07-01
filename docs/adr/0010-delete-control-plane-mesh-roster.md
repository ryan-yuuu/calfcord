# Delete the control plane; roster and settings via the mesh + bridge-side overrides

**Status:** accepted

The `control_plane/` package is deleted (the `agent.state` projection, the
discovery ping, and the `SetThinkingEffort` control command). The bridge now
resolves `@mention`s and `calfcord agent ps` against calfkit's live **mesh**
(`calf.agents` — online-only, heartbeat-staleness-filtered), and the
`/thinking-effort <agent> <effort>` slash writes a **bridge-side persisted
per-agent override** (a SQLite `agent_overrides` row) applied as a per-call
`model_settings`, rather than a control command that reconfigured the agent
process.

## Why

The mesh is calfkit's native, first-class roster, so calfcord's hand-rolled
state projection was duplicative. A bridge-owned settings override is simpler
than an agent-side control channel: it needs no round trip, no agent-side apply,
and no `.md` rewrite, and it survives a bridge restart (hydrated from SQLite).

## Consequences

- Unknown and offline `@mention`s collapse to one case — "no agent named X is
  online right now" — because the mesh is online-only (fail-fast, not fail-open).
- The cross-host duplicate-`agent start` guard moves from an active discovery
  ping to passive mesh heartbeat-staleness.
- The override does not reach native A2A consults/handoffs (those use the agent's
  own default) — see ADR 0011.
