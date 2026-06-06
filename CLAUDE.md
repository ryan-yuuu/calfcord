# calfcord development guide for dummies

This file provides guidance to Claude Code, Codex, and other AI coding assistants when working on the calfcord codebase.

## What this is

Calfcord is an assistant team that lives on Discord: a team of AI agents (each with its own responsibilities, tools, and memories) that talk to humans and to each other. It is built on the **calfkit** SDK (an event-driven and distributed microservices framework to build and deploy AI agents). The defining architectural property is that everything is **distributed and independently deployable** — agents and tools are microservices that can run on different hosts and still collaborate over a shared broker.

## Dependency management and development environment

Project dependencies are managed with **`uv`**. 
- Do not hand-edit `pyproject.toml` — use `uv add <pkg>` so `uv.lock` stays canonical.
- Use `uv run` to execute project files and tests.

## Architecture

Calfcord is **four independent process types that communicate through Kafka**. Each is safe to deploy on its own host; switching deployment styles needs no code changes. `docs/architecture.md` is the authoritative source. Calfkit nodes are distributed by design, so agents, tools, and other integrations cannot be assumed to share a host filesystem. Configuration, control plane concerns, and other shared data must be shared over the network by default.

## Git conventions

- **Commits/PRs landing on `main` use conventional-commit prefixes**: `feat:`, `fix:`, `chore:`, `docs:`,
  `refactor:`, `test:`, `perf:`, `style:`. Pick the narrowest accurate one. PR titles follow the same style
  (squash-merge inherits them).
- **Ruff clean for new/changed files.** CI's lint job is `continue-on-error` only while a small pre-existing
  baseline of errors is cleared — don't add to it and don't fix unrelated baseline errors in the same PR.
- Comments and docstrings explain *why*, not *what*.

## Sub-agents

- When planned work is large, you may spawn sub-agents to split up or parallelize the work where possible
- Always spawn sub-agents with the opus model and xhigh thinking effort
- Spawn intelligent sub-agents generously for any kind of review work, investigation, and intel gathering

## Test driven development

- When implementing any code, please follow test driven development principles using the skill `/test-driven-development`
- Use the skill `/pytest-coverage` to check your test completeness.

## Deep implementation reviews

- Use `/pr-review-toolkit:review-pr` to deeply review the code changes for:
    - functional bugs and issues, 
    - anti-patterns,
    - test coverage,
    - documentation correctness & coverage
- Review implementations using the `/simplify` skill to surface any potential design or implementation simplifications using more elegant, well-engineered solutions or designs.
- In certain cases, when prompted, you may have to go through multiple rounds of deep reviews for code changes. In these events, the review is not considered done until the findings from consecutive review rounds converge towards no critical or must-fix issues.
- Spawn intelligent sub-agents generously for any kind of review work, investigation, and intel gathering

## Development: calfkit agents SDK

- This project dogfoods the calfkit event-driven and distributed agents SDK.
- If you reach use cases that calfkit geniunely does not support, causing you to either reach into calfkit internals or implement a hacky workaround, please create a new issue in the calfkit repo, providing a clear explanation of what you were trying to achieve or design and how calfkit's API surface was insufficient: https://github.com/calf-ai/calfkit-sdk/issues
- If you run into any verifiable bugs or issues in the calfkit SDK, please create an issue explaining the bug clearly and how to reproduce: https://github.com/calf-ai/calfkit-sdk/issues