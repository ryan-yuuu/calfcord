# Security Policy

Agent Disco ships agents with shell, filesystem, and web-fetch tools that
take LLM-supplied input. The deployment model assumes a trusted shared
workspace (see `README.md` → **Security**). Real-world deployments
will find ways to break those assumptions; this doc is how to report
what you find without making the problem worse.

For the long-form deployment / threat model — sandboxing options,
network egress posture, recommended `.env` handling — see
[`docs/security.md`](docs/security.md).

## How to report

**Primary path: GitHub Security Advisories.** Open a private advisory at
<https://github.com/ryan-yuuu/agent-disco/security/advisories/new>. This
keeps the report off the public issue tracker until there's a fix to
disclose.

**No email backup.** GHSA is the only private reporting path. If you
don't have a GitHub account, create one — it's free, and the private
advisory flow is far better than email for coordinated disclosure. If
that's impossible for you, open a regular issue saying "I have a
security concern; please open a private channel" and a maintainer
will respond there.

What to expect:

- **Initial acknowledgment within 7 days.** That's the maintainer
  confirming the report was received and is being triaged, not a fix
  ETA.
- **90-day responsible-disclosure window.** Maintainers will work
  toward a fix and coordinated disclosure inside that window. If the
  fix needs longer, we'll say so explicitly and propose a new date
  rather than letting the clock run silently. Public disclosure before
  day 90 is at maintainer + reporter mutual agreement; after day 90 is
  the reporter's call.
- **Credit on disclosure** unless you ask to remain anonymous.

Please include, when you can: the affected commit SHA or release tag,
the deployment mode (native / hybrid / all-in-Docker), a minimal repro,
and the impact you observed.

## What's in scope

The four Agent Disco processes:

- **`calfkit-bridge`** — Discord gateway, registry loader, normalizer,
  outbox / persona webhook posting.
- **`calfkit-agent`** — the LLM-driven Agent nodes that consume channel
  topics and emit replies.
- **`calfkit-router`** — the ambient-channel router that decides
  whether a non-mentioned message should reach an agent.
- **`calfkit-tools`** — the tool runner.

Plus the tool surface itself. Anything that takes LLM-supplied string
input is high-priority — in particular `shell` and `web_fetch`, where a
prompt-injection payload landing in a tool argument has obvious
implications. Path-handling in the `fs` tools (`read_file`,
`write_file`, `edit_file`) is in scope too; `_resolve_path` has
documented no-escape-protection behavior by design, but any way to
escape *beyond* that documented behavior (e.g. via symlink races) is a
bug.

The deployment plumbing around those processes is also in scope:

- The workspace bind-mount model (Docker Compose mounts the project
  root into the `tools` container read-write).
- `.env` handling — how secrets reach each process, whether anything
  logs `DISCORD_BOT_TOKEN` or an LLM API key, whether a tool can
  exfiltrate them.
- The Kafka wire format between processes.
- The A2A audit mechanism: the unified `a2a-audit` Discord channel,
  the per-conversation thread projection, and Discord category
  permission inheritance.

## What's NOT in scope

- **Bugs in upstream `openhands-tools`, `smolagents`, Tansu /
  Apache Kafka, or `calfkit` itself.** Report those to their respective
  trackers; we'll happily coordinate but the fix has to land upstream.
- **Operator misconfigurations.** A `.env` checked into a public repo
  because the gitignore was edited; a Discord bot token leaked in a
  tweet; a `compose.override.yml` that widens the workspace mount to
  `$HOME` without thinking through the blast radius. These are
  documentation / education issues, not vulnerabilities — file an
  issue if the docs misled you.
- **Social engineering of Discord bot tokens or LLM API keys.** Out of
  scope as a security report; in scope as a "the docs should warn
  about this" issue.
- **Denial-of-service via expensive LLM prompts or tool calls** where
  the operator controls the bot's exposure. If the issue requires
  exposing the bot to the open internet without rate-limiting, the
  hardening belongs in the operator's deployment, not in Agent Disco.

## License note

Agent Disco is released under Apache-2.0. Vulnerability reports and any
patches submitted with them are accepted under the same terms.
