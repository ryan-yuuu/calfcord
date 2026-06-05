# End-user onboarding + interactive tool editor

> **Status:** in progress on `feat/end-user-onboarding` (PR 1 landed first).
> **Baseline:** calfcord @ `eecc4e5`.
> **Audience:** calfcord maintainers. Captures the decisions behind making the
> native installer the primary onboarding path and adding two `calfcord`
> sub-commands (`init`, `agent tools`).

## Summary

The README quick start today is the **Docker Compose** path: it assumes a repo
clone (`cp .env.example .env`, drop `agents/scribe.md` into the tree,
`docker compose up --build`). That is a developer workflow, not an end-user one.
The native installer (`curl … | bash` → `~/.calfcord/`) is the genuinely
end-user-shaped path, but it had a gap: nothing gave the install a stable home
for agent definitions, and the "add an agent" / "pick a provider" steps were
undocumented.

This plan reorients the README around the installer and closes that gap with the
smallest, most reusable set of changes:

1. a stable agents/state home under `~/.calfcord/`, with the tools **workspace
   following the launch directory** (the Claude Code model);
2. a provider-agnostic **starter agent** (`agents/assistant.md`);
3. a guided **`calfcord init`** for first-run config; and
4. an interactive **`calfcord agent tools`** editor for an agent's tool list.

The defining constraint of the design is **reuse**: every new capability is
expressed through an existing seam rather than a parallel one. No new parsers,
validators, or provider plumbing are introduced.

## Findings that anchor the design (verified in code @ `eecc4e5`)

| Finding | Source | Consequence |
|---|---|---|
| Tools / thinking / model are **baked into the node at boot**; the `.md` is read once | `agents/factory.py:436` (`Agent(...)`), `agents/loader.py` | Tool edits need an **agent restart**; live mutation is out of scope |
| Provider resolves `frontmatter → CALFKIT_AGENT_DEFAULT_PROVIDER → "anthropic"` | `agents/factory.py:204` (`resolve_provider`) | Starter ships with **no `provider:`**; one env var drives it |
| `tools:` omitted → **all** builtins; `tools: []` → none | `agents/loader.py:32` (`_resolve_default_tools`), `agents/definition.py:113` | Starter uses `tools: []`; a tools editor must distinguish omitted vs empty |
| Per-agent `<id>.json` is **agent-managed** (channel subscriptions); the bridge never reads it | `agents/runner.py` (`store.load/save`); bridge has no refs | Safe to pin `CALFKIT_STATE_DIR` under `~/.calfcord` |
| Workspace defaults to **`Path.cwd()`-relative** | `tools/builtin/workspace.py:53` | Native default → the launch directory (not a hidden dir) |
| Builtins + MCP schemas enumerate **without transport/secrets** | `tools/__init__.py` (`TOOL_REGISTRY`), `mcp/discovery.py`, `mcp/selector.py` | A local CLI can list the full tool universe, honoring the decoupling invariant |
| `InquirerPy` / `prompt-toolkit` / `rich` already resolved | `uv.lock` | Interactive multi-select needs **no new heavy dependency** |

## Decisions

- Guided **`calfcord init`** for onboarding (not a hand-edited `.env`).
- **Docker-Redpanda** as the recommended local broker now; broker auth (SASL/TLS)
  for hosted/serverless Kafka is a **deferred fast-follow**.
- Provider-agnostic starter named **`assistant`** (general-purpose), `tools: []`.
- Agents + state **pinned** under `~/.calfcord/`; the tools **workspace follows
  the launch directory** (Claude Code model).
- An interactive **`calfcord agent tools`** editor; a live Discord `/tools`
  command is explicitly **out of scope** (needs a node hot-rebuild / lifecycle
  work that does not exist yet).

## Architecture

### Native directory model (intentional asymmetry)

| Var | Native default | Owner | Why |
|---|---|---|---|
| `CALFKIT_AGENTS_DIR` | `~/.calfcord/agents` | operator (edits) | stable home; survives `self update` GC |
| `CALFKIT_STATE_DIR` | `~/.calfcord/state/agents` | agent process | channel-subscription bookkeeping; must persist regardless of CWD |
| `CALFCORD_WORKSPACE_DIR` | **the launch `$PWD`** | tools process | agents act where you launched, like Claude Code |

`agents/` and `state/` are calfcord's own data and must outlive the GC'd
`versions/<sha>` tree, so they live under the install home. The tools workspace
is the *operator's* working files, so it follows the directory the tools process
was launched from. This matches the already-documented mode in
[`security.md`](../security.md) §3.3 ("same blast radius as Claude Code"); the
trust note there and §3.4 (don't expose to public Discord) apply.

### Shim wiring — one helper, `.env` always wins

The `calfcord` shim defaults the three vars before exec, but only when the
operator has not already set them (shell env **or** `config/.env`), so the
`.env` is authoritative and we never depend on `uv run --env-file` precedence:

```bash
_default_env() {  # name default
  [ -n "${!1:-}" ] && return 0
  [ -f "$ENVF" ] && grep -q "^$1=" "$ENVF" && return 0
  export "$1=$2"
}
_default_env CALFKIT_AGENTS_DIR     "$H/agents"
_default_env CALFKIT_STATE_DIR      "$H/state/agents"
_default_env CALFCORD_WORKSPACE_DIR "$PWD"
```

Dev (`uv run`) and Docker never see the shim and keep their current defaults.

### `calfcord init` (config only; never seeds)

Writes `~/.calfcord/config/.env` (dev: `./.env`), idempotent, secrets masked,
`chmod 600`. Seeding the starter is the installer's job — `init` only *detects
and reports* the agent, so there is no duplicated starter content.

```
1 Provider → select [anthropic | openai | openai-codex]; upsert CALFKIT_AGENT_DEFAULT_PROVIDER
             anthropic/openai → masked key → upsert ANTHROPIC_API_KEY / OPENAI_API_KEY
             openai-codex     → "run `calfcord calfkit-auth login`"
2 Discord  → DISCORD_BOT_TOKEN (masked), DISCORD_APPLICATION_ID; optional GUILD / CHANNEL
3 Broker   → [Local Redpanda (Docker) | I have a broker URL]
             docker → CALF_HOST_URL=localhost:19092 + print one-liner (offer to run if docker present)
             url    → CALF_HOST_URL=<prompt>
4 Detect   → list agents in CALFKIT_AGENTS_DIR; report or explain how to add one
5 Next     → start broker · run the 4 processes · @assistant hello
```

### `calfcord agent tools [<name>]` (interactive editor)

```
resolve agents_dir (CALFKIT_AGENTS_DIR | $H/agents | ./agents)
name = arg or InquirerPy.select(*.md)
defn = parse_agent_md(md_path)            # RAW tools: None=omitted, []=none, [..]=explicit
builtins = sorted(TOOL_REGISTRY)          # name + tool_schema.description
mcp = discover_mcp_catalog(schemas_pkg)   # {server:[McpToolDef]} (often {} until codegen)
selected = InquirerPy.checkbox(grouped builtins + mcp/<server>[/<tool>] selectors,
                               pre-checked from current; None ⇒ all builtins checked)
update_tools(md_path, selected)           # generalized md_writer (atomic + validated)
print "Restart `calfcord calfkit-agent` to apply."
```

Pre-selection converts the implicit "all" (`tools:` omitted) into explicit checks
and always writes an explicit list, so on-disk state stops being ambiguous after
the first save. MCP tools use the existing `mcp/<server>` / `mcp/<server>/<tool>`
selector grammar (`mcp/selector.py`), which imports schema-only — no transport.

### `md_writer` generalization (DRY)

Collapse the bespoke mutator into one validated-atomic path; existing
`/thinking-effort` behaviour is unchanged:

```python
def _update_fields(md_path, updates: dict) -> AgentDefinition: ...   # load → apply → validate synthetic def → atomic write
def update_thinking_effort(md_path, value): return _update_fields(md_path, {"thinking_effort": value})
def update_tools(md_path, tools):           return _update_fields(md_path, {"tools": list(tools)})
```

### CLI dispatch — one entry point, tiny shim delta

`calfcord self` stays in **bash** (install management must work even if the venv
is broken). A new Python package `calfcord.cli` (`calfcord-cli` entry point,
argparse subcommands) hosts `init` and `agent tools`. The shim adds one case
after `self` that dispatches `init|agent` to `calfcord-cli` (passing
`CALFCORD_HOME`). Future subcommands under those verbs are Python-only.

## Elegant simplifications (explicit)

1. **Reuse, don't rebuild**: enumeration via `TOOL_REGISTRY` + `discover_mcp_catalog`;
   selectors via `mcp/selector.py`; parsing via `parse_agent_md`; provider via the
   existing `CALFKIT_AGENT_DEFAULT_PROVIDER`. Zero new parsers/validators/provider plumbing.
2. **One CLI package** hosts both commands; one shim case dispatches them.
3. **One `md_writer` path** for every frontmatter mutation.
4. **One canonical starter file** (`agents/assistant.md`); the installer seeds it,
   `init` only verifies — no duplicated content.
5. **One `_default_env` helper** for all three dir vars, with `.env` precedence baked in.
6. **No new heavy dependency** — InquirerPy is already resolved.

## PR breakdown

Each PR is conventional-commit-prefixed and ships with a test (no CI test-count
regression). Sequence: **1 → 2 → 4 → 3** (PR 1 is independent).

- **PR 1 — `feat:` native home + starter agent + workspace-as-CWD.**
  `agents/assistant.md`; `scripts/install.sh` (`AGENTS_DIR`/`STATE_DIR` vars,
  `seed_agents()` [seed only when agents dir empty], `_default_env` shim block,
  source-guard for `main`, log lines); `tests/test_install_sh.py` (sources the
  installer in bash; asserts seeding + no-clobber and the shim's three env
  branches via a fake `uv`). *Closes the installer's prior zero-test gap.*
- **PR 2 — `feat:` `calfcord init` + CLI scaffolding.** `src/calfcord/cli/`
  (`main`, `init`, `_envfile`, `_prompts`); `[project.scripts] calfcord-cli`;
  `uv add inquirerpy`; shim `init|agent` dispatch. Tests: `.env` upsert
  (idempotent / never-clobber / 0600); init flow via injected prompt streams.
- **PR 4 — `feat:` `calfcord agent tools` editor** (depends on PR 2 scaffolding).
  `src/calfcord/cli/agent_tools.py` + `md_writer` generalization. Tests:
  `update_tools` atomic/validate/round-trip; choice pre-selection from
  `None`/`[]`/explicit; selector grammar; agent picker.
- **PR 3 — `docs:` installer-first README** (lands last, captures everything).
  README quick start rewrite (Docker demoted to a dev pointer);
  `installation.md` (agents-dir + `init` + Redpanda one-liner + shared-broker
  link); `architecture.md` running-modes; `security.md` native workspace note;
  `authoring-agents.md` / `authoring-tools.md` mention `calfcord agent tools`;
  `distributed-deployment.md` SASL-deferred note.

## Deferred (out of scope; noted in docs)

- **Broker SASL/TLS** plumbing (`CALF_SASL_*` → `Client.connect(security=…)`
  across the five runners). Unlocks zero-install hosted Kafka and cross-env
  distribution. `Client.connect` already forwards `**broker_kwargs`; calfcord
  just never passes them.
- **Live Discord `/tools`** (no-restart) — needs the calfkit node hot-rebuild /
  lifecycle work tracked in [`calfkit-worker-lifecycle-gaps.md`](./calfkit-worker-lifecycle-gaps.md).

## Risks & mitigations

- *`uv run --env-file` precedence* → neutralized by `_default_env`'s `.env` grep.
- *InquirerPy needs a TTY* → commands are interactive; tests target the pure
  choice-build + write functions, not the prompt; non-TTY surfaces a clear error.
- *Frontmatter rewrite drops comments / sorts keys* (existing `md_writer` caveat)
  → documented; `assistant.md` carries no load-bearing comments.
- *Importing `TOOL_REGISTRY` triggers builtin module imports* → one-time CLI
  startup cost, same env, acceptable.
