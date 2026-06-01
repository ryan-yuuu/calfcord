# Agent Persistent Memory — Implementation Plan

**Status**: Finalized, awaiting approval to implement (drafted 2026-05-31)
**Scope**: Per-agent file-based memory modeled on Claude Code. A memory-enabled agent gets a "how memory works" block appended to its system prompt; the block lives in an **editable Markdown file** (not Python). The agent then manages plain memory files with the **existing general-purpose filesystem tools** — no dedicated memory tools, no isolation machinery.
**Touches**: mostly `agents/` — new `agents/memory_prompt.md`, new `agents/memory.py` (loader + composer), a `memory: bool` field on `AgentDefinition`, one `factory.build_node` call, one env var, plus docs and tests. The only deploy-side change is a one-line `docker-compose.yml` cleanup (remove the vestigial `agent-memory` mount). **No Dockerfile, CI, release-workflow, or slim-image-builder code changes are required** — verified in §7.

> Design archive note (per `docs/design/README.md`): the finalized plan for an
> unbuilt feature, recording the decisions and their rationale.

## Trust model (the premise everything rests on)

calfcord agents are a **cooperating fleet owned by a single user** — no multi-tenancy,
no untrusting parties sharing a deployment. Memory therefore needs **no isolation or
enforcement**: per-agent directories are for *organization*, not security, and an agent
reading a peer's memory is harmless. This matches the existing "trusted shared
workspace" (README §Security model) and is faithful to the reference — Claude Code's own
memory has no dedicated tools and no sandbox; it writes files with the ordinary `Write`
tool and hand-maintains a `MEMORY.md` index. This plan does the same.

## Decision log

| # | Decision | Rationale |
|---|---|---|
| D1 | **Explanation → agent system prompt** at `factory.build_node` (`factory.py:378-386`), via `compose_system_prompt(definition)`. | One convergence point for every agent; static text; needs no filesystem; rides the prompt cache. |
| D2 | **Opt-in via `memory: true` frontmatter, default off.** | Operator-explicit; only agents that should keep memory pay the tokens and adopt the behavior; zero change to existing agents. |
| D3 | **Managed with existing general-purpose fs tools — no dedicated memory tools.** | Single trust domain removes the only reason for a bespoke namespace-enforcing API. Faithful to Claude Code; almost nothing new to build. |
| D4 | **Memory lives at `memory/<agent_id>/` under the shared `./workspace` mount.** | The fs tools are rooted at `CALFCORD_WORKSPACE_DIR=/workspace`, so this is reachable with natural relative paths and needs **no compose change**. `./workspace` is a persistent bind mount → survives restarts. |
| D5 | **Per-agent subdir is a convention, not a boundary; no path enforcement.** | Trust model. Organization without machinery; cross-agent reads allowed. |
| D6 | **Agent hand-maintains its `MEMORY.md` index; recall is tool-driven** (agent reads the index file at task start). | No dedicated tool to rewrite the index (like Claude Code). Passive cross-process injection is blocked by the mount topology (§5), so "read your index" is the natural recall path. |
| D7 | **The explanation text lives in an editable Markdown file** (`agents/memory_prompt.md`), read and appended at build time; overridable via `CALFCORD_MEMORY_PROMPT_PATH`. | Operators tune the memory prompt without touching Python; the file doubles as living documentation of the default. A `{{MEMORY_DIR}}` placeholder is interpolated per agent. |
| D8 | **`memory: true` with no fs tools is a hard build error.** | A memory agent with `tools: []` can't act on the block; a silent no-op flag is worse than a loud failure. (Relaxable to a warning if it proves annoying.) |

## 1. Goals

A durable per-agent notepad surviving process restarts, so an agent can:

- Remember stable facts about the human it serves and the peers it works with, without
  re-deriving them each turn.
- Record guidance it's been given ("always reply in this format") and apply it later.
- Carry project context across invocations that last-N Discord history
  ([`conversation-history-plan.md`](./conversation-history-plan.md)) can't hold.

Claude Code's two halves, mapped:

| Half | Claude Code | calfcord (this plan) |
|---|---|---|
| **Explanation** | `# Memory` section of the system prompt | Editable `memory_prompt.md`, appended to the system prompt when `memory: true` (D1, D7) |
| **Recall** | `MEMORY.md` auto-injected each session | Agent reads `memory/<id>/MEMORY.md` with `read_file` at task start (D6) |

## 2. Non-goals (deferred)

- Dedicated memory tools / a memory API (rejected, D3).
- Isolation / access enforcement (rejected by trust model, D5).
- Semantic / vector recall — recall is the plain `MEMORY.md` index.
- Automatic memory extraction — the agent decides what to save.
- Passive recall injection — tool-driven (D6); see §5.1 for the escape hatch.
- Memory in the router — stateless classifier, no memory.
- Hot-reload of the prompt file — read once and cached; operator edits take effect on
  the next process restart (consistent with all other boot-time config).

## 3. Mechanism

### 3.1 The composer (`agents/memory.py`)

```python
# agents/memory.py
import os
from pathlib import Path
from calfkit_organization.agents.definition import AgentDefinition

_ENV = "CALFCORD_MEMORY_PROMPT_PATH"
_DEFAULT_PATH = Path(__file__).parent / "memory_prompt.md"
_MEMORY_DIR_PLACEHOLDER = "{{MEMORY_DIR}}"
_cached_prompt: str | None = None

def load_memory_prompt() -> str:
    """Read the memory-explanation template (env override or bundled default), cached.

    Raises ValueError (→ BootstrapError at the factory seam) if an explicitly
    configured override path is missing/unreadable, or if the resolved file is
    empty — an empty explanation would make `memory: true` a silent no-op.
    """
    global _cached_prompt
    if _cached_prompt is not None:
        return _cached_prompt
    raw = os.getenv(_ENV)
    path = Path(raw).expanduser() if raw else _DEFAULT_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"cannot read memory prompt at {path} ({_ENV}={raw!r}): {e}") from e
    if not text.strip():
        raise ValueError(f"memory prompt at {path} is empty")
    _cached_prompt = text
    return _cached_prompt

def compose_system_prompt(definition: AgentDefinition) -> str:
    """Return the agent's system prompt, with the memory block appended iff opted in."""
    if not definition.memory:
        return definition.system_prompt
    mem_dir = f"memory/{definition.agent_id}/"
    block = load_memory_prompt().replace(_MEMORY_DIR_PLACEHOLDER, mem_dir)
    return f"{definition.system_prompt}\n\n{block.strip()}"
```

Notes:
- **`str.replace`, not `str.format`** — the Markdown contains literal braces (YAML
  frontmatter examples), which `.format` would choke on. A single explicit placeholder
  token avoids that entirely.
- The composer runs in the **agent process** at build time. It reads only the prompt
  template (pure text); it never touches `agent-memory`/`workspace`, so the storage
  living in the tools process is irrelevant here (D1).
- **The agent-specific path is purely a build-time string substitution** of
  `definition.agent_id` into the template, baked once into the static prompt — no
  per-invocation work and no tool-side namespacing. `mem_dir` is **workspace-relative**
  (`memory/<id>/`, not absolute) because the LLM passes it straight to `read_file` /
  `write_file`, which resolve relative paths against `/workspace` in the tools process
  (`fs.py:_resolve_path`). The interpolated path therefore lives in the LLM's addressing
  frame; an absolute path would wrongly couple the prompt to the tools container's mount
  layout. Consequently the per-agent dir is what the agent is *told* to use, not an
  enforced boundary (D5).
- Errors surface as `BootstrapError` via the runner's existing
  `_build_node_or_bootstrap_error` wrapper → clean stderr exit, no traceback.

### 3.2 The touch point (`factory.py:378-386`)

```python
from calfkit_organization.agents.memory import compose_system_prompt

agent = Agent(
    node_id=definition.agent_id,
    system_prompt=compose_system_prompt(definition),   # body, +memory block if opted in
    ...
)
```

The router path (`factory.py:457-465`) is untouched.

### 3.3 The fs-tools guard (D8)

In `build_node`, after `tools = self._resolve_tools(definition)` and only when
`definition.memory`: require the resolved tool set to include at least `read_file` and
`write_file`; otherwise raise `ValueError` (→ `BootstrapError`) naming the agent and the
missing tools. (Agents that omit `tools:` get all tools and pass automatically; only an
explicitly-restricted agent can trip this.)

### 3.4 The editable prompt (`agents/memory_prompt.md`)

The complete `# Memory` section — header included, so operators own all of it —
with one `{{MEMORY_DIR}}` placeholder:

```markdown
# Memory

You have a persistent memory that survives restarts. It lives in your
workspace at `{{MEMORY_DIR}}` and you manage it with your normal file tools
(read_file, write_file, edit_file, glob) — there are no special memory
commands.

Layout:
- `{{MEMORY_DIR}}MEMORY.md` — your index: one line per memory,
  `- [short description](slug.md)`. Read this first.
- `{{MEMORY_DIR}}<slug>.md` — one fact per file, with frontmatter:
    ---
    name: <short-kebab-slug>
    description: <one-line summary>
    type: user | feedback | project | reference
    ---
    <the fact>

Types: user (who you serve), feedback (guidance you've been given — include
the why), project (ongoing work or constraints), reference (URLs, IDs, docs).

How to use it:
- At the start of a task, read `{{MEMORY_DIR}}MEMORY.md` to see what you
  already know. If it doesn't exist yet, you simply have no saved memories
  — that's normal on a fresh start. Open individual files only when relevant.
- To save: write_file the fact to `{{MEMORY_DIR}}<slug>.md`, then add its
  one-line pointer to `{{MEMORY_DIR}}MEMORY.md` (create that index file if it
  doesn't exist yet). write_file makes the directory for you, and writing the
  same slug updates an existing memory.
- Before saving, check the index and update an existing memory rather than
  duplicating it. Rewrite or remove memories you find are wrong.
- Save only durable facts; convert relative dates to absolute. Don't save
  anything that only matters to the current message.
- This memory is yours; peers keep their own under memory/<their id>/.
```

## 4. Storage layout

```
workspace/                         # the existing ./workspace bind mount (persistent)
└── memory/
    ├── scribe/
    │   ├── MEMORY.md              # index: one "- [description](slug.md)" line/memory
    │   ├── user_boss.md           # frontmatter + body, one fact per file
    │   └── feedback_<slug>.md
    └── conan/
        └── MEMORY.md
```

`workspace/`'s contents are already gitignored (only `workspace/.gitkeep` is tracked),
so `memory/` is excluded from git automatically. No new mount, no new storage env var.

## 5. Why recall is tool-driven (the one surviving constraint)

The fs tools run in `calfkit-tools`; the memory files live there (under `/workspace`).
Neither the **bridge** (which authors `temp_instructions`, `bridge/ingress.py:474`) nor
the **agent** process (which builds the system prompt) has that mount, and calfkit's
`Agent` takes a *static* `system_prompt` with no per-call hook (the only dynamic channel,
`temp_instructions`, is set by the invoker — `calfkit/nodes/agent.py:137`). So no process
is positioned to read an agent's index and inject it at invocation time. Recall is
therefore "the explanation tells the agent to `read_file` its `MEMORY.md`" — exactly how
Claude Code would recall absent auto-injection, at the cost of one cheap tool call.

### 5.1 Optional passive-injection parity (deferred)

If "read your index" proves unreliable, add a read-only mount of the memory dir on the
bridge or agent process and read `MEMORY.md` at `temp_instructions` / system-prompt build
time. Cost: a second mount + boot-snapshot staleness. Out of v1.

## 6. File-by-file changes

| File | Change |
|---|---|
| `agents/memory_prompt.md` | **NEW** — the editable explanation template (§3.4), with `{{MEMORY_DIR}}`. Shipped as package data. |
| `agents/memory.py` | **NEW** — `load_memory_prompt()` (cached read, env override, non-empty validation) + `compose_system_prompt(definition)` (§3.1). |
| `agents/definition.py` | Add `memory: bool = False` with a docstring (`extra="forbid"` requires it be declared). |
| `agents/factory.py` | `build_node` calls `compose_system_prompt`; add the fs-tools guard (§3.3). Router path untouched. |
| `pyproject.toml` (build backend) | **Likely no change.** Hatchling includes non-`.py` files under the wheel package (`packages = ["src/calfkit_organization"]`) by default, and the file is git-tracked, so `memory_prompt.md` ships automatically. One-time `uv build` + inspect to confirm; add an explicit `[tool.hatch.build] include`/`force-include` only if it's somehow missing. See §7.2. |
| `.env.example` | Document `CALFCORD_MEMORY_PROMPT_PATH` (commented; default bundled file works). |
| `docker-compose.yml` | Remove the now-vestigial `./agent-memory:/app/agent-memory` mount from the **agent** service (it anticipated a different design; memory now lives under `./workspace`). Optional cleanup, not load-bearing. |
| `.gitignore` | Drop the `agent-memory/` line alongside the mount removal; `workspace/` contents already ignored. |
| `tests/agents/test_memory.py` | **NEW** — `compose` returns body unchanged when off; appends + interpolates `memory/<id>/` when on; `load_memory_prompt` caches, honors `CALFCORD_MEMORY_PROMPT_PATH`, raises on missing override + empty file; `{{MEMORY_DIR}}` fully substituted (no placeholder leaks). |
| `tests/agents/test_definition.py` | `memory` field default + opt-in parse. |
| `tests/agents/test_factory.py` | Composed vs unchanged prompt by flag; fs-tools guard raises for `memory: true` + `tools: []`; passes for default (all tools). |
| `docs/authoring-agents.md` | Document `memory:`, the `memory/<id>/` convention, fs-tool requirement, and `CALFCORD_MEMORY_PROMPT_PATH`. |

No new tools, no tools-process change, no scope/enforcement helper, no storage env var.

## 7. Build, packaging & deploy impact

Verified against `Dockerfile`, `pyproject.toml`, `packaging/dockerfile.py`, and
`.github/workflows/{ci,release}.yml`. Net: **one optional compose cleanup; nothing else
changes.**

### 7.1 The cross-service split (the key deploy fact)

Memory spans **two** processes, and neither needs what the other has:

- The **prompt** is read in the **agent** process (`compose_system_prompt` at
  `factory.build_node`). The file is baked into the image — no mount, no workspace.
- The **memory files** are written by the **tools** process (the general-purpose fs
  tools), into `workspace/memory/<id>/` on the existing `./workspace` mount.

So a working memory deployment needs **both** an agent worker *and* a tools worker that
hosts the fs tools and mounts a persistent workspace — which is the **default topology
already**. No agent process needs a workspace mount; no tools process needs the prompt.

### 7.2 Packaging — the `.md` ships for free

`memory_prompt.md` lives under `src/calfkit_organization/agents/`, inside the wheel
package (`[tool.hatch.build.targets.wheel] packages = ["src/calfkit_organization"]`).
Hatchling includes non-`.py` files under the package dir by default and the file is
git-tracked, so it ships in the wheel; in the Docker build it also arrives via
`COPY src ./src`. Both editable and built-wheel installs resolve
`Path(__file__).parent / "memory_prompt.md"`. **Action:** one-time `uv build` + inspect
to confirm; only add a hatch `include`/`force-include` if missing (not expected).

### 7.3 Dockerfile — no change

`COPY src ./src` already carries the new module and the `.md`. Editing the prompt busts
the `uv sync` layer (it's source) — expected. No new OS packages, no new ENV.

### 7.4 docker-compose.yml — one cleanup, no new mount

- Memory files land in the **existing** `./workspace` mount on the **tools** service —
  no new mount.
- **Remove** the now-vestigial `./agent-memory:/app/agent-memory` mount from the
  **agent** service (and the matching `.gitignore` line). Pure cleanup.
- **Optional prompt override:** on the **agent** service only, mount a custom file and
  set `CALFCORD_MEMORY_PROMPT_PATH` to it. Bridge/router/tools never read the prompt.
- **No pre-`mkdir` of `memory/`.** Unnecessary and not cleanly possible here:
  `write_file` creates parent dirs on first save (`fs.py:141`), and the tools service is
  registry-free (no `agents/*.md` access) so it can't enumerate agent IDs to create
  `memory/<id>/` anyway — at most it could make an empty parent, which buys nothing.
  First-run reads of a missing `MEMORY.md` are handled in the prompt (§3.4), so the dir
  not pre-existing is invisible to the agent. If you want the parent visible in the
  checkout, commit `workspace/memory/.gitkeep` — purely cosmetic.

### 7.5 CI / release — no change

- CI `test` runs the new unit tests (the `.md` is present in the checkout); CI `build`
  re-validates the image builds.
- `release.yml` publishes a **multi-arch Docker image** (not a PyPI wheel) built from the
  same Dockerfile, so the `.md` ships with no workflow edit.

### 7.6 Slim images (`calfcord-package-agents` / `-tools`) — no templater change

- `render_agents_dockerfile` already does `COPY src ./src`, so a packaged-agents image
  carries the prompt. A memory-enabled agent in such an image still needs a co-deployed
  tools worker that (a) includes the fs tools in `CALFCORD_TOOLS_INCLUDE` and (b) mounts a
  persistent workspace.
- A slim tools image built **without** the fs tools cannot serve memory writes — an
  operator note, not a code change.
- `tests/packaging/test_dockerfile.py` needs no new assertion (templater unchanged);
  confirm it still passes.

## 8. Implementation order (single phase)

1. `agents/memory_prompt.md` — the template (§3.4).
2. `agents/memory.py` — loader + composer (§3.1).
3. `agents/definition.py` — `memory: bool = False`.
4. `agents/factory.py` — call composer + fs-tools guard (§3.2, §3.3).
5. `uv build` + inspect to confirm `memory_prompt.md` is in the wheel (§7.2).
6. Unit tests (steps 2–4): gating, interpolation, caching, override/empty/missing,
   guard behavior.
7. `.env.example` + `docs/authoring-agents.md`; remove the vestigial `agent-memory`
   mount + `.gitignore` line (§7.4).
8. Manual smoke test (exercises the §7.1 cross-service split): set `memory: true` on one
   agent, run an agent worker **and** a tools worker with fs tools + `./workspace`; have
   the agent save a fact + index line to `workspace/memory/<id>/`, restart, confirm it
   reads them back via `read_file`. Also smoke-test `CALFCORD_MEMORY_PROMPT_PATH`.
9. `/code-review` over the diff. Per CLAUDE.md, any sub-agents use **opus + xhigh**.

The work is small and self-contained in `agents/`; parallelization isn't warranted.

## 9. Open questions / risks

1. **Package-data inclusion (low).** §7.2: hatchling ships package-dir `.md` by default
   and `COPY src ./src` carries it, so both install modes resolve the file. Confirmed by
   a one-time `uv build` inspect (step 5); `importlib.resources` is the fallback only if
   that surprises us.
2. **`memory: true` + no fs tools (D8).** Resolved to a hard build error; relaxable to a
   warning if operators find it heavy-handed.
3. **Recall reliability (D6).** The model must remember to read its index; the prompt says
   so explicitly, and §5.1 is the fallback if it proves flaky in practice.
4. **Prompt edits need a restart** (read-once cache). Acceptable; matches other config.
5. **Index drift.** The agent maintains `MEMORY.md` by hand and can forget a pointer (same
   risk Claude Code carries); the prompt calls it out. Not worth tooling in v1.
6. **Memory ≠ scratch coexistence.** Memory shares the `./workspace` tree with scratch
   work (D4); revisit a dedicated mount only if clutter bites.

---

## Self-review checklist

- [x] `memory: bool` flag kept (D2) — the deliberate, off-by-default opt-in.
- [x] Explanation text moved to an editable `.md` file read + appended at runtime (D7),
      with an env override and per-agent `{{MEMORY_DIR}}` interpolation.
- [x] General-purpose fs tools only; no dedicated tools, no isolation machinery (D3, D5).
- [x] Storage under the existing `./workspace` mount → no compose change required (D4).
- [x] Recall-is-tool-driven justified by the surviving mount-topology constraint (§5).
- [x] fs-tools-required guard resolves the prior open question (D8).
- [x] Interpolation uses `str.replace` (not `.format`) to survive literal braces in the md.
- [x] Build/deploy surface audited (§7) against Dockerfile, pyproject, the slim-image
      templater, and CI/release workflows: `.md` ships for free, no workflow/Dockerfile
      change, one optional compose cleanup, cross-service split made explicit.
- [x] Scope genuinely small: one md file, one ~25-line module, one field, one factory call.
