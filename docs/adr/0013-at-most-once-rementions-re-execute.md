# At-most-once delivery; a re-mention re-executes

**Status:** accepted

Each `@mention` is handled at-most-once and there is no invocation idempotency:
the bridge holds the run handle in memory for the run's lifetime (no
reattach-by-`correlation_id`), so a bridge restart mid-run drops that run's
reply, and re-`@mention`ing an agent **re-executes** it (re-billing LLM tokens,
re-running any side effects).

## Why

The caller-surface `InvocationHandle` is an in-memory, weak-referenceable,
acyclic object that self-GCs when the caller drops it (calfkit spec §5.2);
durable reattach and cross-restart reply replay are out of scope for v1. Adding
app-side idempotency/persistence would reintroduce the correlation bookkeeping
the pure-`Client` design (ADR 0008) deliberately removed.

## Consequences

A user (or operator) recovers a dropped reply by re-mentioning; downstream tools
must tolerate a repeated call. Acceptable because Discord interactions are
human-paced and agent side effects are already retry-tolerant by convention.
