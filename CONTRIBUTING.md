# Contributing to Agent Disco

Thanks for the interest. This doc covers the workflow for sending changes
that the maintainers can merge with minimum back-and-forth. It assumes
you've read the top-level `README.md` once — the deployment modes and
process layout there are prerequisites for everything below.

## Setup

Agent Disco is a Python 3.12+ project. Dependencies are managed with `uv`
(see `pyproject.toml`); do not hand-edit `[project.dependencies]`. Use
`uv add <pkg>` so the lockfile stays canonical.

```bash
uv sync --frozen          # install pinned deps into .venv (matches CI)
uv run pytest -q          # the test suite should pass on a clean checkout
```

Your branch should not regress the test count vs. `main`. The latest
CI run on `main` is the canonical baseline — check the CI badge at the
top of the README. If your branch lands below `main` without an
explicit "this test is removed because X" note in the PR, something
regressed.

For running Agent Disco end-to-end, start with the **Quick start** in
`README.md` (Docker Compose). Two more deployment modes — native
(`uv run` each process) and the mix-and-match hybrid — are documented in
[`docs/architecture.md`](docs/architecture.md#running-modes). New
contributors should start with Docker Compose; native mode is faster to
iterate on once you know the layout.

## Running locally

The README **Quick start** and
[`docs/architecture.md`](docs/architecture.md#running-modes) are the
single source of truth for how to start the four processes. Don't
duplicate them here. The only addition
for development: when iterating on a single agent, run that one agent
natively (`uv run calfkit-agent <name>`) and leave bridge / router /
tools in compose. The wire protocol is Kafka, so the split-mode shape
just works.

## Adding an agent

An agent is a single Markdown file under `agents/`. The frontmatter is
identity (name, persona) plus runtime hints (provider, model, tool
list, thinking effort); the body is the LLM's system prompt. The
Discord slash command is always `/<name>`.
The bridge auto-discovers every file in that directory at boot.

The full reference — every frontmatter field, the provider-default
fallback chain, the per-agent runtime state model — lives in
[`docs/authoring-agents.md`](docs/authoring-agents.md). Read that
before adding a new agent.

## Adding a tool

A tool is an `async` function decorated with `@agent_tool` and dropped
into `src/calfcord/tools/builtin/`. Discovery is automatic
on the next `calfkit-tools` boot — no registry edits, no entry points.
Agents opt in by listing the tool name in their `.md` frontmatter
`tools:` array.

The full reference — signature contract, error-handling convention,
discovery rules, security model, testing pattern, lazy-init for heavy
resources — lives in
[`docs/authoring-tools.md`](docs/authoring-tools.md). Read that before
shipping a tool, especially section 4 (error handling) and section 6
(security).

## Commit conventions

All commits landing on `main` start with a conventional-commit prefix:

- `feat:` — a new feature
- `fix:` — a bug fix
- `chore:` — maintenance or tooling changes
- `docs:` — documentation only changes
- `refactor:` — code changes that neither fix a bug nor add a feature
- `test:` — adding or updating tests
- `perf:` — performance improvements
- `style:` — formatting, whitespace, etc. (no code change)

Why: the prefix is what makes release-notes generation legible without
hand-curation, and it's the same signal a future semantic-version tool
would read off the log. Pick the narrowest accurate prefix — `feat:` for
behavior visible to an operator or an agent's LLM, `refactor:` for
moves that change no observable behavior, `chore:` for build / CI /
tooling. When in doubt between `feat:` and `fix:`, ask whether someone
running the previous commit would notice the change as new capability
(`feat:`) or as the absence of a bug (`fix:`).

This convention applies to commits going onto `main`. Inside a feature
branch, commit however helps you think; the squash-or-rebase before
merge is where the convention lands.

## PR expectations

- **Tests for new code.** New behavior ships with at least one test
  that exercises it. Run `uv run pytest -q` locally; your branch
  should not regress the count vs. the latest `main` CI run.
- **Ruff clean for new files.** Run `uv run ruff check <your-files>`
  and fix anything it flags. The repo has a small number of
  pre-existing ruff errors in older modules; don't add to that count
  and don't try to fix unrelated ones in the same PR. The CI lint job
  is advisory (`continue-on-error: true`) while the baseline is being
  cleared, but new errors should still be cleaned up before merge.
- **One logical change per PR.** A refactor and a feature are two PRs.
  Reviewers can absorb 200 lines of focused diff; 2,000 lines of mixed
  intent get rubber-stamped or sit unreviewed.
- **PR title follows the same conventional-prefix style as commits.**
  `feat: add pypi_info tool`, not `Add pypi_info tool`. Squash-merged
  commits inherit the PR title, so the prefix has to be there for the
  history on `main` to stay consistent.

## Code review

What reviewers look for, in order:

1. **Correctness.** Does the change do what the description says? Are
   edge cases (empty input, network failure, missing config) handled
   the way the rest of the codebase handles them?
2. **No silent failures.** This codebase has a hard convention for
   tools: LLM-recoverable problems return `"error: ..."` strings;
   infrastructure bugs raise `RuntimeError` with caller context. See
   `docs/authoring-tools.md` section 4. The same principle applies
   outside tools — prefer a loud `RuntimeError` over a swallowed
   exception or a logged-and-continued warning.
3. **Tests prove the claim.** A test that passes whether or not the
   production change is present is not a test for that change.
4. **Tone matches.** Comments and docstrings explain *why*, not *what*.
   The code already says what.

How to respond to comments: address every thread, either with a code
change or a reply explaining why you're not changing. "Done" is fine
when paired with a force-push that shows the fix; a comment without
either a fix or a justification stalls the review.

## Reporting bugs / requesting features

- **Bugs and feature requests:** open an issue. Templates live at
  [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/) and prompt for
  the information that's almost always the first reviewer follow-up
  (repro steps, deployment mode, commit SHA).
- **Security vulnerabilities:** do *not* open a public issue. See
  [`SECURITY.md`](SECURITY.md) for the private disclosure path via
  GitHub Security Advisories.
