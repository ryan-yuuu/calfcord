# Agent Persistent Memory — Design & As-Built

**Status**: Implemented on `feat/agent-memory` (2026-05-31). Full suite green
(1520 passed, 2 skipped); `memory_prompt.md` verified present in the built wheel.
**Scope**: Per-agent file-based memory modeled on Claude Code. A memory-enabled
agent gets a "how memory works" block appended to its instructions **at runtime**;
the block text is read **only by the bridge** from an editable Markdown file,
shipped to agents in `deps`, and localized per-agent by a dynamic-instructions
hook. The agent then manages plain memory files with the **existing
general-purpose filesystem tools** — no dedicated memory tools.

> Design archive note (per `docs/design/README.md`): records the decisions and
> their rationale, and now the as-built shape.

## Trust & distribution premises

- **Single-owner cooperating fleet** — no multi-tenancy. Memory needs no isolation
  or enforcement; per-agent directories are for organization, not security.
- **Distributed in space** — the four processes can run on separate hosts and
  share **nothing but Kafka**. Nothing in this feature reads a file another
  process wrote: the **bridge is the single reader** of the prompt file; agents
  and the tools process receive or forward it over Kafka, never read it.

## Decision log (as built)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Opt-in via `memory: true` frontmatter**, default off. | Operator-explicit; existing agents unchanged; only memory agents pay any cost. |
| D2 | **General-purpose fs tools — no dedicated memory tools.** | Single trust domain removes the reason for a namespacing API; faithful to Claude Code. |
| D3 | **Memory lives at `memory/<agent_id>/` under the shared `./workspace` mount.** | The fs tools (in `calfkit-tools`) resolve relative paths against the workspace; no compose change, persistent bind mount. |
| D4 | **Per-agent subdir is a convention, not a boundary.** | Trust model; cross-agent reads are harmless. |
| D5 | **Recall is tool-driven** (agent reads its `MEMORY.md` at task start), and the agent **hand-maintains** the index. | calfkit `Agent` is static + the mount topology blocks passive injection; "read your index" is the natural, distribution-safe path. |
| D6 | **The explanation text is injected by the BRIDGE via `deps`, localized by a per-agent runtime hook — agents never read the prompt file.** | Single source of truth at the bridge: one change propagates to all agents without rebuilding them. Preserves distribution (bridge is the sole reader). |
| D7 | **`private_chat` projects `deps` forward (spread + override)** so the template reaches A2A targets. | `deps` is reconstructed at the A2A hop; spreading carries ambient context (the template) to every memory agent regardless of caller, with no per-key plumbing. |
| D8 | **`memory: true` with no `read_file`/`write_file` is a hard build error.** | A memory agent that can't touch files is a silent no-op; fail loud at agent build. |

## 1. Architecture (as built)

```
        memory_prompt.md  (bundled package data; editable; CALFCORD_MEMORY_PROMPT_PATH override)
                 │  read by the BRIDGE only
                 ▼
┌──── calfkit-bridge ────────────────────────────────────────────────┐
│  ingress.handle (slash/@-mention/fan-out):                          │
│    deps = {discord, phonebook, **_memory_prompt_deps()}             │
│    _memory_prompt_deps(): {memory_prompt: <raw template>}           │
│       only when the registry has ≥1 memory-enabled agent            │
└────────────────────────────────────────┬───────────────────────────┘
                                          │ deps over Kafka
              ┌───────────────────────────┼───────────────────────────┐
              ▼                                                         ▼
┌──── calfkit-agent ────────────────┐         ┌──── calfkit-tools (private_chat) ────┐
│  factory.build_node:              │         │  execute_node(deps={                  │
│   if definition.memory:           │         │     **caller_deps,   # projects the   │
│     agent.instructions(           │  A2A    │     # bridge-seeded memory_prompt fwd │
│       memory_instructions(id))    │◀────────│     discord, caller_agent_id,         │
│  hook reads deps[memory_prompt],  │  invoke │     phonebook})                       │
│  localizes memory/<id>/, appends  │         └───────────────────────────────────────┘
│  → instructions (calfkit joins    │
│    system_prompt + temp_instr +   │         Memory FILES live in calfkit-tools'
│    this) at runtime               │         /workspace, reached via read_file/
└───────────────────────────────────┘         write_file over Kafka (D3).
```

So an agent learns it has memory from the runtime-appended block, then operates on
`memory/<id>/` through the ordinary fs tools. The template's text exists in exactly
one readable place (the bridge); everything downstream receives or forwards it.

## 2. Components (file-by-file, as built)

| File | Change |
|---|---|
| `agents/memory_prompt.md` | **NEW** — the editable explanation template, with a `{{MEMORY_DIR}}` placeholder. Ships as package data (verified in the wheel). |
| `agents/memory.py` | **NEW** — `load_memory_prompt()` (bridge reads it; cached; raises on missing/empty), `render_memory_block(template, agent_id)` (`str.replace` localization), `memory_instructions(agent_id)` (builds the runtime hook), `MEMORY_PROMPT_DEPS_KEY`. No runtime `calfkit_organization` import → no cycle. |
| `agents/definition.py` | `memory: bool = False` field. |
| `agents/factory.py` | `build_node`: `_require_memory_tools` guard (D8); registers `memory_instructions(agent_id)` via `agent.instructions(...)` when `definition.memory`. System prompt left raw (no baking). |
| `bridge/ingress.py` | `_memory_prompt_deps()` helper (gated on any-memory-agent; loads template; logs-once + degrades on load failure); splatted into the slash-branch `deps`. |
| `tools/builtin/private_chat.py` | Both `execute_node` sites spread caller `deps` then override the A2A-owned keys; `caller_deps` threaded through `_post_response_with_feedback_retries` → `_execute_retry_with_feedback` so the retry path carries it too. |
| `.env.example` | `CALFCORD_MEMORY_PROMPT_PATH` documented (read on the **bridge**). |
| `docs/authoring-agents.md` | `memory:` field + the bridge-injection mechanics. |
| `docker-compose.yml` / `.gitignore` | Removed the vestigial `agent-memory` mount + ignore line (memory lives under `./workspace`). |
| `pyproject.toml` | **No change** — hatchling ships the package-dir `.md` by default (verified). |
| Tests | `tests/agents/test_memory.py` (loader/render/hook); `+` memory cases in `test_definition.py`, `test_factory.py` (guard); `tests/bridge/test_ingress.py::TestMemoryPromptInjection` (gated injection, raw template); `tests/tools/builtin/test_private_chat.py` (deps projection across A2A). |

No new tools, no tools-process change, no scope/enforcement helper, no agent-side file read.

## 3. The mechanism in detail

### 3.1 Bridge injection (single reader, gated)
`BridgeIngress._memory_prompt_deps()` returns `{MEMORY_PROMPT_DEPS_KEY: load_memory_prompt()}`
only when `any(spec.memory for spec in registry.all())`, else `{}`. Consequences:
zero memory agents → byte-identical to today (no read, no wire cost); ≥1 memory agent
→ the **raw** template rides in every assistant invocation's `deps`, guaranteeing it
reaches any memory agent through any path (including A2A from a non-memory caller). A
bad `CALFCORD_MEMORY_PROMPT_PATH` is logged once and skipped — memory agents degrade
to no block rather than the bridge failing every message.

### 3.2 A2A propagation (`private_chat`)
`deps` is reconstructed at the A2A hop, so `private_chat` spreads the caller's
`deps` first, then overrides the keys it owns (`discord` → the A2A-forwarded wire,
`caller_agent_id` → this hop's caller, `phonebook` → refreshed). Ambient keys like
`memory_prompt` thus propagate automatically; the retry-with-feedback path threads
the same `caller_deps`.

### 3.3 Per-agent runtime hook
The factory registers `memory_instructions(agent_id)` on memory agents via pydantic-ai's
dynamic-instructions decorator (`Agent.instructions`). At each invocation calfkit calls it
with a `RunContext` whose `.deps` is the deps dict; it reads `deps[MEMORY_PROMPT_DEPS_KEY]`,
localizes `{{MEMORY_DIR}}` → `memory/<agent_id>/`, and returns the block (or `None` if
absent). calfkit joins it with the system prompt + per-call peer roster
(`get_instructions` in vendored pydantic-ai). The path is workspace-relative because the
LLM passes it to `read_file`/`write_file`, resolved against the tools workspace.

### 3.4 The explanation text (`memory_prompt.md`)
The complete `# Memory` block (header included, so operators own all of it) with one
`{{MEMORY_DIR}}` placeholder; tells the agent to keep one-fact-per-file memories plus a
`MEMORY.md` index under `{{MEMORY_DIR}}`, managed with `read_file`/`write_file`/`edit_file`,
and to read the index at task start (treating a missing index as "no memories yet").

## 4. Storage layout

```
workspace/                         # existing ./workspace bind mount (persistent), gitignored
└── memory/
    ├── scribe/{MEMORY.md, user_boss.md, …}
    └── conan/{MEMORY.md, …}
```

## 5. Distribution invariant (verified)

- **Prompt text**: read only by the bridge (bundled `memory_prompt.md`, or an operator
  file via `CALFCORD_MEMORY_PROMPT_PATH` on the bridge), shipped over Kafka in `deps`.
  Agents and tools never read it.
- **Memory files**: written/read only by the tools process in its own `/workspace`,
  reached by the LLM via `read_file`/`write_file` Kafka tool calls.
- **Bridge & router** otherwise uninvolved. No shared filesystem anywhere.
- The only inherited caveat is the pre-existing one: a multi-tools-host deploy splits the
  workspace; memory inherits it, single-tools-host (default) is fully correct.

## 6. Open items / follow-ups

1. **Pre-existing lint** in files touched (`RUF100` in `test_factory.py`, `I001` inline
   import in `test_private_chat.py`) — baseline, not introduced here; CI lint is advisory.
2. **Recall reliability** — the agent must remember to read its index; the prompt says so.
   Passive parity (a read-only mount + bridge-side index read) remains a deferred option if
   needed.
3. **Manual smoke test** (not yet run — needs a live Discord+Kafka deployment): enable
   `memory: true` on one agent with a tools worker, confirm save/restart/recall and that a
   custom `CALFCORD_MEMORY_PROMPT_PATH` on the bridge takes effect.
4. **Index growth** uncapped (v1); revisit if memories balloon.

## 7. Verification done

- `uv run pytest -q` → **1520 passed, 2 skipped**.
- `uvx ruff check` on all changed source files → clean (pre-existing baseline lint aside).
- `uv build --wheel` → `memory_prompt.md` present at `calfkit_organization/agents/memory_prompt.md`.
- Import-cycle check across the four touched modules → clean.
