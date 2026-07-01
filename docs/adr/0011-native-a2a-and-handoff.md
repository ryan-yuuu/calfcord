# Native agent-to-agent messaging + handoff; delete `private_chat`

**Status:** accepted

Agents consult and hand off to peers via calfkit's built-in `message_agent`
tool and `Handoff` peer, declared per-agent with the `a2a` and `handoff`
frontmatter fields (both default `true`, so every agent can reach any peer; a
list restricts, `false` opts out). This replaces the first-party `private_chat`
tool, which is **deleted** (its `Client.execute` RPC transport, the
forwarded-wire `slash_target` gate, the `<thread_id>` return contract, and the
deps-propagated phonebook go with it). A native consult is **stateless** — the
peer answers on a fresh conversation with no replay of the prior A2A thread. The
Discord A2A audit projection (a thread per exchange in a unified channel) is
preserved, re-homed to the bridge and driven off the caller's run stream.

## Why

calfkit 0.12 injects `message_agent` and resolves the peer directory inside the
agent runtime, so the bespoke `private_chat` transport (and the phonebook
calfcord shipped on `deps`) is redundant. Declaring capability in frontmatter
keeps the public surface small and the directory live.

## Consequences

- Supersedes the `private_chat`-retention parts of ADR 0004 and 0005.
- A faulted consult faults the whole caller turn (calfkit maps `RunFailed` →
  `NodeFaultError`), where `private_chat` returned an LLM-readable error string.
- No multi-turn A2A continuation (stateless consults, accepted for v1).
- Handoff loops are unbounded in v1 (calfkit has no cross-agent handoff-hop cap);
  operators keep the handoff graph acyclic by design.
