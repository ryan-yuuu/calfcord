# Run the bridge on calfkit's caller surface (a pure `Client`)

**Status:** accepted

The Discord bridge is a pure calfkit `Client`: for each `@mention` it
`client.agent(name).start(...)`s the agent, drains the run's `stream()` (live
progress + the A2A audit projection consumed off that same stream via a
normalized `StepEvent` seam), and awaits `result()` to post the terminal reply
under the responding agent's persona. This replaces the embedded `Worker` with
its Kafka `outbox` / `steps` / `state` consumers, the `pending_wires`
correlation table, and the request/reply plumbing.

## Why

- calfkit 0.12 ships the agent mesh + intermediate streaming, so a run is one
  lossless, ordered, terminal-bearing channel — the bridge no longer needs a
  bespoke reply dispatcher (which deduped by `correlation_id` and so was
  inherently single-agent-per-turn).
- The terminal is `emitter_node_id`-stamped, so a handoff posts the peer's
  persona for free.

## Consequences

- Delivery is at-most-once and re-mentions re-execute (see ADR 0013).
- No app-side `result()` timeout (calfkit C5): a durable/paused run parks its
  handler task until the terminal arrives; shutdown cancels in-flight tasks.
