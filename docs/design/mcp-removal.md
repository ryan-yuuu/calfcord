# Plan: remove MCP support & bump calfkit to 0.7.0

> **Superseded.** MCP support was **reintroduced** on calfkit 0.9.0 — see
> [`mcp-reintroduction.md`](./mcp-reintroduction.md). This document is kept as
> the history of the removal (and the rationale that informed the redesign);
> for current behavior read [`../mcp-tools.md`](../mcp-tools.md).

**Status:** shipped on `main` (commits `refactor!: remove deprecated MCP support`,
`chore(deps): bump calfkit 0.6.0 -> 0.7.0`, `docs: drop MCP references`); reviewed.
**Author:** scoped 2026-06-08
**Trigger:** calfkit **0.7.0** removes the MCP adaptor entirely. calfcord must delete its
use of the deprecated MCP API and bump the pin. calfkit will ship a *v2* MCP later;
when it does, MCP can be re-introduced against the new surface.

---

## 1. Why now / the forcing function

calfkit 0.7.0 (latest on PyPI) drops MCP. From the 0.7.0 release notes:

> The MCP adaptor is removed (landed in #197). `from calfkit import mcp`,
> `Agent(tools=[McpServer(...)])`, `Worker(idempotency_cache=...)`, the `calfkit mcp`
> CLI subcommand, and the `mcp` dependency are gone. **There is no replacement in this
> release; pin `calfkit <0.7` if you depend on the MCP adaptor.**

Confirmed facts:

- The repo currently pins `calfkit[mcp-codegen]~=0.6.0`. **0.6.0 still ships the full MCP
  API** — nothing is broken today; this is a deliberate, green-build removal.
- On 0.7.0 the **`mcp-codegen` extra no longer exists** (replaced by a `cli` extra), so
  `calfkit[mcp-codegen]` would fail to resolve — the dependency line *must* change in
  lockstep with the bump.
- 0.7.0's **only** breaking change is MCP removal (0.6.1 is non-breaking features).
  calfcord does **not** use `idempotency_cache`; every other calfkit import calfcord
  relies on is unaffected.
- **No committed agent uses `mcp/` selectors** (only `agent.template.md` references them);
  `mcp/schemas/` holds no generated modules. There are zero real configs to migrate.
- **Dependency nuance (corrected after review):** the *direct* `jsonschema` pin is dropped
  (its only direct user is `mcp/config_cli.py`), but `jsonschema` **remains installed
  transitively** via `litellm` (← openhands-sdk) and `mcp`. Likewise the `mcp` library does
  **not** leave calfcord's tree — it is still pulled by `browser-use ← openhands-tools`. So
  the bump removes calfcord's dependency on `mcp`/`mcp-codegen` *via calfkit*, not from the
  resolved graph. Action (drop the direct `jsonschema` pin) is correct; phrase the PR/CHANGELOG
  accordingly.

## 2. Decisions taken (from review)

1. **Bump calfkit to 0.7.0 in the same change** (not stay on 0.6.0). `calfkit[mcp-codegen]~=0.6.0`
   → `calfkit~=0.7.0` (plain — calfcord uses neither `typer` nor `watchfiles`, so no `[cli]` extra).
2. **Keep an explicit "MCP unsupported" error** rather than silently letting `mcp/...` fall
   through. A leftover/future `mcp/...` tool entry is rejected at parse time with a clear,
   actionable message pointing at the calfkit-v2 plan (see §5).
3. **Accept the no-replacement gap.** 0.7.0 ships no MCP replacement and there is no announced
   v2 timeline; that is acceptable. A tracking issue is filed to re-introduce MCP on calfkit v2.
4. **Hard break — no migration path.** No concern for existing deployments running an `mcp`
   process; no migration shim, compatibility alias, or deprecation window is provided. calfcord
   is pre-1.0 (`0.1.0`), so the version is left as-is; a CHANGELOG/PR-description note records
   the removed surface (§9).
5. **Single PR.** Source + tests + deps + docs land together (tests must land with source).

## 3. The two MCP subsystems (mental model)

There are two distinct things named "mcp"; the plan removes both.

| | **(A) Agent-side selector machinery** | **(B) The `calfkit-mcp` bridge component** |
|---|---|---|
| Role | Turns `mcp/<server>/<tool>` frontmatter into LLM tool schemas + Kafka routing | A singleton *process type* hosting the live MCP servers |
| Code | `mcp/{selector,catalog,discovery,schema_build}.py` + `schemas/` | `mcp/runner.py` (`calfkit-mcp`) + `mcp/config.py` (`$VAR` loader) |
| Consumers | `agents/{factory,definition,md_writer,phonebook}.py`, `bridge/ingress.py`, `cli/{_agents,agent_tools}.py` | roster / compose / deploy / lifecycle / health / logs / main + `install.sh` |
| calfkit API | `McpToolDef`, `McpServer` (`.stdio/.only/.rename`) | `McpServer`, `McpServers.from_file`, `McpConfigError` |

Two architectural consequences of removal:

- The **agent/bridge decoupling invariant** ("host-agnostic code must never import the
  bridge-only `calfcord.mcp.config`"), guarded by **12 isolation tests across 12 files**
  (enumerated in §7), becomes moot — you cannot import a deleted module. Ten are deleted;
  **two are dual-purpose and only *trimmed*** (`tests/health/test_check.py`,
  `tests/supervisor/test_lifecycle.py` — they also assert `aiokafka` stays lazy, a still-valid
  invariant). A `/simplify` follow-up should then ask whether the lazy-import scaffolding those
  guards protected (e.g. deferred `from calfcord.tools import TOOL_REGISTRY`) still earns its
  keep now the invariant is gone. **Note (post-review):** the deferred `TOOL_REGISTRY` imports
  guard a *real* bridge↔tools import cycle + a per-import perf cost, not the (now-gone) mcp.config
  boundary, so they stay. But the codebase now has **no** test guarding the host-agnostic/bridge
  decoupling invariant (CLAUDE.md §12.3) — **when calfkit v2 MCP returns with a credentialed
  bridge-only loader, restore an import-isolation guard for it.**
- calfcord returns to **four process types** (bridge / agent / tools / router), matching
  `CLAUDE.md`; `architecture.md`'s "five, not four" note is deleted.

## 4. calfkit MCP API surface being eliminated

- `calfkit.mcp.McpToolDef` — catalog, discovery, config, schema_build, factory
- `calfkit.mcp.McpServer` (`.stdio()`, `.only()`, `.rename()`, `__iter__`) — schema_build, runner
- `calfkit.mcp.McpServers.from_file(...)` — config (bridge-only)
- `calfkit.mcp.mcp_json_schema()` — config_cli
- `calfkit.mcp.exceptions.McpConfigError` — runner
- the `calfkit mcp codegen` CLI subprocess — codegen_cli
- the `calfkit[mcp-codegen]` install extra

## 5. The "MCP unsupported" placeholder (design detail)

Goal: a precise, friendly rejection that survives until calfkit v2 MCP lands, with **one
source of truth for both the `mcp/` prefix and the error message**, and no dependency on
the deleted `calfcord.mcp` package.

- **One tiny leaf — `agents/_mcp_guard.py`** (revised after review; *not* a constant split
  across two modules). It holds exactly: `MCP_TOOL_PREFIX = "mcp/"`, a one-line
  `is_mcp_tool(entry) -> bool` (`startswith`), and `mcp_unsupported_error(entry) -> ValueError`
  that builds the single canonical message:
  `"MCP tools are not currently supported — calfkit removed the MCP adaptor in 0.7.0 and v2
  MCP support is planned. Remove '<entry>' from this agent's tools."`
  Both call sites import from this leaf, so the user-facing string lives in one place (no
  drift). This replaces — and is strictly smaller than — the deleted `mcp/selector.py`.
- **Parse-time gate — `agents/definition.py._validate_tools`:** for any entry where
  `is_mcp_tool(entry)`, raise `mcp_unsupported_error(entry)`. Bare names fall through to the
  existing downstream builtin-existence checks.
- **Write-time gate — `agents/md_writer.py.update_tools`:** same rejection before write, so
  `calfcord agent tools` refuses an `mcp/...` token (keep the existing non-string + atomic
  "leaves file untouched" behavior).
- **Docs consistency — `agents/agent.template.md`:** replace the MCP tools content (note:
  *three* regions — L85, L103-130, L200, not one block) with a one-line note: "MCP tools are
  not currently supported (planned for calfkit v2)."

**Why the parse-time gate is sufficient for the bridge (corrected rationale).** The earlier
draft claimed "the bridge builds definitions through this validator" — that is *wrong*. In
production the bridge builds its registry from state events via
`control_plane/builders.state_event_to_definition`, which **stubs `tools=()`** (and
`build_state_event` excludes tools from the wire), so the bridge never carries real `tools:`
lists. The gate is nonetheless sufficient because an `mcp/...` entry is rejected in the
**agent process** at `parse_agent_md → AgentDefinition → _validate_tools`, so such an agent
never boots or announces state. `bridge/ingress.py`'s own MCP branch is therefore safe to
delete; its surviving builtin-unknown check is a defensive backstop.

Everything else in the agent path (factory `_resolve_tools`, bridge ingress, phonebook)
drops its MCP branch entirely and treats `tools:` as builtin names only.

## 6. Work breakdown (phased — but ONE atomic commit)

> **Atomicity (critical, from review).** The phases are a logical decomposition, **not a
> sequence of independently-green checkpoints.** Between Phase 1 and Phase 6 the tree is
> import-broken at every boundary (e.g. after Phase 2 the package is gone but `pyproject`
> still pins `calfkit[mcp-codegen]` and points a console script at the deleted runner;
> after Phase 4's `uv sync` to 0.7.0 any remaining `from calfkit.mcp import …` fails
> *collection*). Do all of Phases 1–5 in **one commit** and only then run Phase 6. Do not
> `git commit` at a phase boundary expecting green.

**Phase 0 — branch.** Worktree off `main` (e.g. `chore/remove-mcp`).

**Phase 1 — leaf-out source edits** (so nothing imports `calfcord.mcp` *or* `calfkit.mcp`):
- **NEW: `agents/_mcp_guard.py`** — the single-leaf prefix + predicate + error builder (§5).
- `agents/factory.py` — drop `McpToolDef` import (L85), `mcp_catalog` ctor param (L264) +
  lazy-load + docstring (L298-329), and the MCP branch / `resolve_mcp_selectors` call /
  combined-surface collision check in `_resolve_tools`; keep all builtin resolution +
  `_require_memory_tools`. **Typing: narrow BOTH `_resolve_tools` and `_require_memory_tools`
  to `list[ToolNodeDef]` together (or keep both `BaseToolNodeSchema`) — do not mix** (invariant
  `list`). Then the now-dead imports `Counter` (L80), `Mapping` (L81), `BaseToolNodeSchema`
  (L86) are removed (confirmed: each is used only by deleted code).
- `agents/definition.py` — replace MCP-selector validation with the parse-time gate (§5);
  drop the `calfcord.mcp.selector` import (L50).
- `agents/md_writer.py` — drop MCP branch; add the write-time gate (§5); fix error text.
- `agents/phonebook.py` — delete `_builtin_tools_only`; drop its `is_mcp_selector` import
  (L27); pass `spec.tools` through.
- `bridge/ingress.py` — drop the 3 `calfcord.mcp` imports (L257-259) + the MCP boot block;
  keep builtin-unknown check.
- `cli/agent_tools.py` + `cli/_agents.py` — **delete** (not trim) the `discover_mcp_catalog`
  import, the `try/except discover_mcp_catalog` scaffold, and the MCP Choice-row loops;
  `_build_choices` returns just `choices` (drop the `mcp_empty` flag and the L205-207
  `calfcord-mcp-codegen` hint).
- **NEW: `cli/explain.py`** *(BLOCKER from review — was missing)* — delete the `mcp` roster
  bullet (L107-109) from `_TOPOLOGY`; scrub the L13 invariant docstring. (The "four process
  types" block already excludes mcp; this realigns the screen and the §6 smoke test.)
- `cli/deploy.py` — remove `"mcp": "calfkit-mcp"` from `_PROCESS_COMMANDS` (L126); scrub
  prose L23/L37/L240.
- `cli/main.py` — un-pair `mcp` from `tools` in the register loop (L176) and dispatch (L700);
  **collapse the now single-element `("tools",)` loop / `== "tools"` check to plain handling**;
  scrub docstrings L10-13/L163-171/L569-595/L698-699.
- `cli/logs.py` — drop `"mcp"` from the reserved-name list (L72, lockstep with compose);
  scrub docstring L24-25.
- `supervisor/compose.py` — drop `"mcp"` from `_RESERVED_PROCESS_NAMES` (L93) and the
  declaration loop (L214); scrub prose L14/L27/L201.
  (`roster.py`/`lifecycle.py`/`health/check.py`/`_provisioning.py` auto-follow.)
- `scripts/install.sh` — drop `mcp` from `run` dispatch (L515), the `mcp)` arm (L519-534),
  and usage text (L440-453).
- `Dockerfile` — delete the `COPY mcp.json ./` line **and** its L56-57 comment (build-breaking if left).
- `.env.example` — delete the whole `── MCP servers ──` block (L94-102, incl. the header).
- Prose-only docstring scrubs (non-load-bearing, accuracy only): `cli/{agent_inspect,agent_lifecycle,_fields,router_config,agent_create,agent_edit}.py`,
  `supervisor/{__init__,_workspace,roster,component,lifecycle}.py`, `health/{__init__,check,refresher}.py`,
  `agents/runner.py`, `_provisioning.py`, `_worker_runtime.py`. (May be batched/de-prioritized.)

**Phase 2 — delete:**
- `src/calfcord/mcp/` (whole package, incl. `schemas/` + its README)
- `mcp.json`
- `docs/mcp-tools.md`

**Phase 3 — tests** (full map + the 12 isolation guards in §7):
- delete `tests/mcp/` (9) + `tests/control_plane/test_import_isolation.py`
- **scrub the two test-file `from calfkit.mcp import McpToolDef` lines** —
  `tests/agents/test_factory.py:16` (module top-level → breaks *collection* under 0.7.0) and
  `tests/bridge/test_ingress.py:511`. These are independent of the `calfcord.mcp` deletions.
- **delete** `TestMcpBootValidation` + `_patch_catalog` (`tests/bridge/test_ingress.py:501-618`),
  the `mcp_catalog` fixture + `TestMcpToolsWiring` (`tests/agents/test_factory.py:727-936`).
- **FLIP, don't just delete, the acceptance tests** so the new gate has coverage:
  `tests/agents/test_definition.py` acceptance cases (L95/L101) → assert rejection;
  `tests/agents/test_md_writer.py:307` → assert rejection + file untouched. Delete the old
  malformed-grammar tests (L111/L125/L134) — the gate rejects all `mcp/` uniformly.
- **remove `mcp` from inputs that would otherwise actively flip an assertion** (not just
  dead sets): `tests/supervisor/test_roster.py` injected-process lists (L321, L828) and the
  agent-id list (L609) — else a process named `mcp` is no longer reserved and the
  `assert "mcp" not in out` / "no agents" assertions FAIL.
- edit the remaining cross-cutting files (drop MCP cases, `["tools","mcp"]`→`["tools"]`, fix
  the reply-topic distinctness 4→3, trim the 2 dual-purpose isolation tests, `tests/test_install_sh.py`).
- **add** the new TDD tests for the gate (see §7 "New tests to add").

**Phase 4 — dependency:**
- `pyproject.toml`: `calfkit[mcp-codegen]~=0.6.0` → `calfkit~=0.7.0` (NOT `~=0.7` — that
  would admit 0.8); remove the `calfkit-mcp`, `calfcord-mcp-codegen`, `calfcord-mcp-add`
  scripts; `uv remove jsonschema` (drops the *direct* pin; it stays transitively).
- `uv lock` and `uv sync` — run **last**, after all source + test edits, so the 0.7.0 install
  never lands while a `from calfkit.mcp import` still exists.

**Phase 5 — docs** (§7) + fix all dangling links to `docs/mcp-tools.md`.

**Phase 6 — verify:** `uv run pytest` green (`/pytest-coverage` for the new gate), `ruff`
clean, smoke `calfcord --help` / `calfcord explain topology` / a deploy render. File the
calfkit-v2 MCP tracking issue.

## 7. File-by-file checklist

### Delete entirely
- `src/calfcord/mcp/**` (10 `.py` + `schemas/__init__.py` + `schemas/README.md`)
- `mcp.json`
- `docs/mcp-tools.md`
- `tests/mcp/**` (9 files)
- `tests/control_plane/test_import_isolation.py`

### `pyproject.toml`
- L10 dep → `calfkit~=0.7.0`; L29/34/35 remove 3 scripts; L14 remove `jsonschema`.

### Source — add
- **`agents/_mcp_guard.py`** (new leaf: prefix + `is_mcp_tool` + `mcp_unsupported_error`).

### Source — functional edits
`agents/factory.py`, `agents/definition.py`, `agents/md_writer.py`, `agents/phonebook.py`,
`bridge/ingress.py`, `cli/agent_tools.py`, `cli/_agents.py`, **`cli/explain.py`**, `cli/deploy.py`,
`cli/main.py`, `cli/logs.py`, `supervisor/compose.py`, `scripts/install.sh`, `Dockerfile`,
`.env.example`. (Prose for `main.py`/`deploy.py`/`compose.py`/`logs.py` is scrubbed in the
same edit — see Phase 1.)

### Source — prose-only docstring scrubs (non-load-bearing; accuracy only)
`cli/{agent_create,agent_edit,agent_inspect,agent_lifecycle,_fields,router_config}.py`,
`agents/runner.py`, `_provisioning.py`, `_worker_runtime.py`,
`supervisor/{__init__,_workspace,roster,component,lifecycle}.py`,
`health/{__init__,check,refresher}.py`.

### Tests — edit
`tests/agents/{test_definition,test_factory,test_md_writer,test_phonebook}.py`,
`tests/bridge/test_ingress.py`, `tests/cli/{test_agent_tools,test_deploy,test_explain,test_init,test_logs,test_main}.py`,
`tests/health/{test_check,test_heartbeat,test_refresher}.py`,
`tests/integration/test_component_ops.py`,
`tests/supervisor/{test_component,test_compose,test_lifecycle,test_roster}.py`,
`tests/test_provisioning_wiring.py`, `tests/test_install_sh.py`, **`scripts/tests/test_installer.sh`**.

**The 12 `calfcord.mcp.config` isolation guards** — delete all except the two dual-purpose ones:
| action | file:line | test |
|---|---|---|
| delete w/ file | `tests/mcp/test_import_isolation.py:57` | agent-path-no-bridge |
| delete w/ file | `tests/control_plane/test_import_isolation.py:41` | control-plane-no-mcp-config |
| **TRIM (keep aiokafka)** | `tests/health/test_check.py:209` | check-no-mcp-or-aiokafka |
| **TRIM (keep aiokafka)** | `tests/supervisor/test_lifecycle.py:887` | lifecycle-no-mcp-or-aiokafka |
| delete guard | `tests/health/test_heartbeat.py:219` | heartbeat-no-mcp-config |
| delete guard | `tests/health/test_refresher.py:335` | refresher-no-mcp-config |
| delete guard | `tests/cli/test_logs.py:382` | logs-no-mcp-config |
| delete guard | `tests/cli/test_explain.py:130` | explain-no-mcp-config |
| delete guard | `tests/cli/test_deploy.py:508` | deploy-no-mcp-config |
| delete guard | `tests/supervisor/test_compose.py:241` | compose-no-mcp-config |
| delete guard | `tests/supervisor/test_component.py:282` | component-no-mcp-config |
| delete guard | `tests/supervisor/test_roster.py:1144` | roster-no-mcp-config |

### Tests — New tests to ADD (TDD for the §5 gate)
- `test_definition.py`: `mcp/<server>` and `mcp/<server>/<tool>` rejected at parse time with
  the canonical message naming the offending entry; bare/unknown builtin names still pass the
  validator; end-to-end `parse_agent_md` rejects `tools: [mcp/gmail]` from a real `.md`.
- `test_md_writer.py`: `update_tools([... , "mcp/gmail"])` raises with the same message AND
  leaves the file byte-identical; the rejection uses the shared `_mcp_guard` (no second copy).
- `test_ingress.py` (or `test_definition.py`): one assertion that `AgentDefinition(tools=("mcp/x",))`
  raises — documents that the bridge needs no MCP-specific runtime check.

### Docs — edit
`README.md`, `agents/agent.template.md` (3 regions: L85, L103-130, L200), `docs/architecture.md`,
`docs/authoring-agents.md` (**added** — L174 "builtin + MCP tool universe"), `docs/configuration.md`,
`docs/troubleshooting.md`, `docs/using-calfcord.md` (delete the "Give your agents more tools (MCP)"
section), `docs/installation.md`, `docs/distributed-deployment.md`, **`roadmap/onboarding-cli.md`**
(L4/L37/L39 — describes removed `calfcord run mcp` / `mcp add|codegen` as shipped).

### Docs — leave (historical record)
`docs/design/{onboarding-redesign,end-user-onboarding-plan,calfkit-worker-lifecycle-gaps,README}.md`,
`docs/reviews/builtin-tools-audit.md`, `docs/tools-research/**`. (Dangling `mcp-tools.md`
links inside these are acceptable under the historical-record policy.)

## 8. Resolved decisions (was: open questions)

1. **No-replacement gap — accepted.** 0.7.0 has no MCP replacement and no announced v2
   timeline; proceeding anyway. → file a tracking issue to re-add MCP on calfkit v2.
2. **Comms — hard break.** No concern for deployments running an `mcp` process; no migration
   path/alias/deprecation window. Version stays `0.1.0` (pre-1.0); record the removed surface
   in the PR description / CHANGELOG (§9).
3. **Placeholder — confirmed.** Parse-time gate in `definition.py` + write-time mirror in
   `md_writer.py` + the `agent.template.md` note, with the §5 message text.
4. **PR shape — single PR.** Source + tests + deps + docs land together.

## 9. User-facing breaking changes (for comms)

Gone: `calfcord mcp start|stop|restart`, `calfcord mcp add`, `calfcord mcp codegen`,
`calfcord run mcp`; the `mcp/...` options in the create/edit/tools wizards; the `mcp`
bullet in `calfcord explain topology`; the `mcp` k8s Deployment; the `calfkit-mcp`,
`calfcord-mcp-add`, `calfcord-mcp-codegen` console scripts.

Behavioral changes to note:
- An agent may now legally be **named `mcp`** (it is no longer a reserved process name).
- An `mcp/...` tool entry now raises a clear "not currently supported" error at parse time.
  A pre-existing `.md` still carrying one **cannot be opened via `calfcord agent tools` /
  `agent rename`** (it fails to parse) — the operator must hand-edit the YAML to remove it.
  Acceptable given zero committed configs use MCP; the error message says exactly what to do.
- Wording: say "calfcord no longer depends on `mcp`/`jsonschema` **via calfkit**" — both
  still arrive transitively through the openhands/litellm chains (§1).

## 10. Risks

- **Collection break under 0.7.0 (highest-consequence).** `tests/agents/test_factory.py:16`
  has a top-level `from calfkit.mcp import McpToolDef`; once 0.7.0 installs, pytest can't even
  *collect* that module. Must be scrubbed in the same commit (Phase 3). The one live
  `calfkit.mcp` import in non-test src is `agents/factory.py:85` — the only place the bump can
  break a *non-mcp* process.
- **No green checkpoint between phases.** The tree is import-broken at every Phase 1-5
  boundary; commit once, verify once (see §6 atomicity note).
- **Test assertions that *flip*, not just go dead.** `tests/supervisor/test_roster.py`
  (L321/L609/L828) and the acceptance tests in `test_definition.py`/`test_md_writer.py` will
  silently invert/fail unless handled per §6/§7 — not mere case removal.
- **Lockstep edits.** `compose._RESERVED_PROCESS_NAMES` ↔ `cli/logs._known_names`; and
  `scripts/install.sh` ↔ `scripts/tests/test_installer.sh` ↔ `tests/test_install_sh.py` —
  all three installer harnesses assert `mcp` routing.
- **`Dockerfile` COPY.** Deleting `mcp.json` without removing `COPY mcp.json ./` breaks the build.
- **`explain.py` (was missing).** Source bullet must be removed or the §6 smoke
  (`calfcord explain topology`) and the edited `test_explain.py` fail.
- **calfkit 0.7.0 bump.** Verified at the `v0.7.0` tag: every non-MCP symbol calfcord uses
  (incl. the vendored `pydantic_ai` — same slim 1.47.0) is byte-stable; MCP is the only break.
  Still, the full `uv run pytest` + a deploy/compose smoke is the real gate before merge.
  Pin `calfkit~=0.7.0` exactly (not `~=0.7`).
