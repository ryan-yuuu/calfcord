# Persona is the agent name; drop display_name/avatar_url/history_turns/role

**Status:** accepted

An agent's Discord persona is derived entirely from its `name` — the webhook
username *is* the name, and the avatar is a deterministic DiceBear image seeded
by it. The `display_name`, `avatar_url`, `history_turns`, and `role` frontmatter
fields are removed from `AgentDefinition`; with `extra="forbid"`, a `.md` that
still carries them fails to parse loudly.

## Why

The deleted agent registry was the only reader of `display_name`/`avatar_url`
(the bridge now derives the persona with a pure function, so it works across a
distributed deployment with no shared filesystem); the mesh carries only
name/description. `history_turns` is dead — there is no per-agent history window;
the bridge passes the recent channel window it fetches. `role` distinguished the
now-deleted built-in router.

## Consequences

- Existing deployments need a one-time `.md` migration (remove the four fields);
  the loud parse error is the intended migration signal.
- History author-stamping identifies an agent's own past turns by bot-owned
  `webhook_id` (the persona sender's id set), not by a name registry — so a
  renamed or offline agent's turns are never mis-attributed.
