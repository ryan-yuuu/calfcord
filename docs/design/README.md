# Design archive

Historical design notes from prior calfcord iterations. Kept around for
context on why specific architectural decisions were made; not authoritative
on the current behavior of the code.

For up-to-date documentation see:

- [README.md](../../README.md) — project overview and quick start.
- [docs/architecture.md](../architecture.md) — the four processes, deployment matrix, run modes.
- [docs/configuration.md](../configuration.md) — full environment-variable reference.
- [docs/a2a-threads.md](../a2a-threads.md) — agent-to-agent threading via `private_chat`.
- [docs/ambient-routing.md](../ambient-routing.md) — the router process and non-`@mention` channel routing.
- [docs/authoring-tools.md](../authoring-tools.md) — adding a builtin tool.
- [docs/authoring-agents.md](../authoring-agents.md) — adding an agent.

## Contents

In approximate chronological order (oldest first):

- [`discord-topic-bridge-plan.md`](./discord-topic-bridge-plan.md) — the original
  bridge design: per-channel Kafka topics, Discord-event normalization, and
  the persona-webhook outbox.
- [`calfkit-agent-factory-plan.md`](./calfkit-agent-factory-plan.md) — the
  Markdown-defined `agents/*.md` format, the `AgentDefinition` schema, and
  the factory that turns a definition into a runnable calfkit `Agent` node.
- [`conversation-history-plan.md`](./conversation-history-plan.md) — fetching
  recent channel messages from Discord and projecting them into the agent's
  `message_history` so the LLM sees context, not just the latest message.
- [`discord-retry-with-feedback-plan.md`](./discord-retry-with-feedback-plan.md) —
  the bridge outbox's retry policy: when a Discord post fails with an
  agent-fixable error (400 family), re-invoke the agent with a
  `<system-reminder>` explaining the failure so it can adapt.
- [`threaded-private-chat-plan.md`](./threaded-private-chat-plan.md) — the
  per-conversation Discord-thread projection of A2A `private_chat`
  exchanges, replacing the prior `a2a-<a>-<b>` per-pair channel.
