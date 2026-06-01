"""Load the built-in routing agent's prompt and runtime config from ``router.md``.

The router's system prompt and its runtime config (``provider``, ``model``,
``thinking_effort``, ``history_turns``) live together in a single bundled
Markdown file, :file:`router.md`, beside this module — YAML front matter for
the config, Markdown body for the prompt. This mirrors how user-defined agents
are authored (``agents/*.md`` parsed by
:func:`calfkit_organization.agents.definition.parse_agent_md`), while keeping
the router file **bundled in the package** rather than user-managed: the router
is project infrastructure, and shipping the file inside the wheel/image (like
:file:`agents/memory_prompt.md`) means a deploy can never lose its router
config.

The prompt instructs the LLM to answer one question for every ambient
(``kind="message"``) Discord message: **"who is the user talking to?"** The
LLM picks one agent from the listed roster and emits the answer as a single
``<tool_name>(...)`` structured-output call (default tool name:
:data:`ROUTER_OUTPUT_TOOL_NAME`) carrying the agent id plus a short reasoning
string. The "exactly one agent" policy is enforced two ways: at the schema
level (:attr:`RoutingDecision.agent_id` is a single string, not a list) and at
the prompt level (the LLM is told to pick one addressee and that the chosen
agent can pull in peers out-of-band via its ``private_chat`` tool — the router
itself never fans out).

The per-call agent roster is injected via ``temp_instructions`` (built by
:func:`calfkit_organization.router.roster.build_router_temp_instructions`)
rather than baked into the file, so a newly-added agent becomes visible to the
router on the very next invocation without a restart.

Interpolation
-------------
The body references the structured-output tool name
(:data:`ROUTER_OUTPUT_TOOL_NAME`) and the :class:`RoutingDecision` field names
(``agent_id``, ``reasoning``). These are **not** hardcoded in the Markdown: the
body carries ``{{ROUTER_OUTPUT_TOOL}}`` / ``{{AGENT_ID_FIELD}}`` /
``{{REASONING_FIELD}}`` placeholders that :func:`load_router_md` substitutes
from the code constants at load time, so renaming the tool or a field stays a
one-edit change in code. The substitution is a plain :meth:`str.replace`
(mirrors :mod:`calfkit_organization.agents.memory`'s ``{{MEMORY_DIR}}``
handling), and a surviving ``{{...}}`` after substitution is treated as a
typo'd placeholder and fails fast. A coupling test
(``tests/router/test_prompt.py``) confirms the names referenced in the rendered
prompt match the schema, and the field-name constants below are asserted
against :class:`RoutingDecision` at import time.

Override
--------
The bundled file is read by default; ``CALFKIT_ROUTER_PROMPT_PATH`` points the
loader at a mounted file to override the whole thing (config front matter +
prompt body) without rebuilding the image. The parsed config and rendered
prompt are cached on a module global; an operator editing the file takes effect
on the next router restart.
"""

from __future__ import annotations

import os
from pathlib import Path

import frontmatter
import yaml
from pydantic import ValidationError

from calfkit_organization.agents.routing import ROUTER_OUTPUT_TOOL_NAME, RoutingDecision
from calfkit_organization.router.config import RouterConfig

_AGENT_ID_FIELD = "agent_id"
_REASONING_FIELD = "reasoning"
# Pin field names against the schema so a rename of ``RoutingDecision``
# fields without updating the placeholders fails at import time, not via a
# silently-malformed LLM tool call.
assert _AGENT_ID_FIELD in RoutingDecision.model_fields, (
    f"prompt references {_AGENT_ID_FIELD!r} but RoutingDecision has fields "
    f"{list(RoutingDecision.model_fields)}"
)
assert _REASONING_FIELD in RoutingDecision.model_fields, (
    f"prompt references {_REASONING_FIELD!r} but RoutingDecision has fields "
    f"{list(RoutingDecision.model_fields)}"
)

_PROMPT_PATH_ENV = "CALFKIT_ROUTER_PROMPT_PATH"
"""Env var an operator can set to point the loader at a mounted ``router.md``
(config + prompt) instead of the bundled file. When unset, the loader reads the
:file:`router.md` beside this module."""

_DEFAULT_PROMPT_PATH = Path(__file__).with_name("router.md")

# Placeholders that ``router.md``'s body carries; substituted with the code
# constants at load time so a rename of the tool / a schema field stays a
# single edit in code rather than a coordinated edit of the Markdown.
_TOOL_PLACEHOLDER = "{{ROUTER_OUTPUT_TOOL}}"
_AGENT_ID_PLACEHOLDER = "{{AGENT_ID_FIELD}}"
_REASONING_PLACEHOLDER = "{{REASONING_FIELD}}"

_cached: tuple[RouterConfig, str] | None = None


def load_router_md() -> tuple[RouterConfig, str]:
    """Return ``(config, system_prompt)`` parsed from ``router.md``, cached.

    Reads ``CALFKIT_ROUTER_PROMPT_PATH`` when set, otherwise the bundled
    :file:`router.md` beside this module. The YAML front matter is validated
    into a :class:`RouterConfig` (so ``extra="forbid"`` rejects any
    router-identity field), and the Markdown body has its
    ``{{ROUTER_OUTPUT_TOOL}}`` / ``{{AGENT_ID_FIELD}}`` / ``{{REASONING_FIELD}}``
    placeholders substituted from the code constants. Cached on a module
    global; an operator editing the file takes effect on the next restart.

    Raises:
        ValueError: if the file cannot be read (e.g. a configured override
            path that doesn't exist), has no YAML front matter (missing /
            mistyped ``---`` fences), its front matter is malformed or invalid
            (unknown / reserved key, bad enum, out-of-range ``history_turns``),
            the body is empty, or a ``{{...}}`` placeholder survives
            substitution (a typo'd placeholder that would otherwise reach the
            LLM verbatim). The path is included in the message so the error is
            self-describing in container logs.
    """
    global _cached
    if _cached is not None:
        return _cached

    override = os.getenv(_PROMPT_PATH_ENV)
    path = Path(override).expanduser() if override else _DEFAULT_PROMPT_PATH

    try:
        post = frontmatter.load(path)
    except (OSError, UnicodeError) as e:
        # ``UnicodeError`` (covers ``UnicodeDecodeError``) is a ``ValueError``
        # subclass, not an ``OSError`` — a readable-but-non-UTF-8 override file
        # would otherwise escape as a bare, context-less ``ValueError``.
        raise ValueError(
            f"cannot read router prompt at {path} ({_PROMPT_PATH_ENV}={override!r}): {e}"
        ) from e
    except yaml.YAMLError as e:
        # ``frontmatter.load`` lets ``yaml.YAMLError`` propagate unchanged;
        # re-raise as ``ValueError`` with the path so the malformed-front-matter
        # path is self-describing and callers that catch ``ValueError`` cover it.
        raise ValueError(f"{path}: malformed YAML frontmatter: {e}") from e

    metadata = dict(post.metadata)
    if not metadata:
        # No (or unclosed / BOM-prefixed / mistyped) ``---`` fences: frontmatter
        # returns empty metadata and folds the whole file — config lines
        # included — into the body. Fail loudly (mirrors ``parse_agent_md``)
        # rather than silently booting on all-default config with the raw config
        # text leaked into the prompt. Every bundled/override router.md must
        # carry front matter.
        raise ValueError(
            f"{path}: no YAML front matter found ({_PROMPT_PATH_ENV}={override!r}); "
            f"the router config (provider/model/thinking_effort/history_turns) must "
            f"appear between '---' fences at the top of the file"
        )

    try:
        config = RouterConfig(**metadata)
    except ValidationError as e:
        # Wrap the pydantic error with the file path so operators see which
        # file the validation failed against. Pydantic's own message lists
        # each invalid field with its location.
        raise ValueError(f"{path}: invalid router config: {e}") from e

    _cached = (config, _render_body(post.content, path))
    return _cached


def _render_body(raw: str, path: Path) -> str:
    """Substitute the body placeholders and validate the result.

    Raises:
        ValueError: if the body is empty after substitution, or a ``{{...}}``
            placeholder survives (only the three known placeholders are
            expected, so any remaining brace pair is a typo).
    """
    body = (
        raw.replace(_TOOL_PLACEHOLDER, ROUTER_OUTPUT_TOOL_NAME)
        .replace(_AGENT_ID_PLACEHOLDER, _AGENT_ID_FIELD)
        .replace(_REASONING_PLACEHOLDER, _REASONING_FIELD)
        .strip()
    )
    if not body:
        # An empty body makes the router prompt-less and useless; fail loudly
        # rather than ship an agent with no instructions.
        raise ValueError(f"{path}: router prompt body is empty")
    if "{{" in body or "}}" in body:
        raise ValueError(
            f"{path}: unsubstituted placeholder remains in router prompt body "
            f"(expected only {_TOOL_PLACEHOLDER}, {_AGENT_ID_PLACEHOLDER}, "
            f"{_REASONING_PLACEHOLDER})"
        )
    return body


# Eager binding so ``from ...router.prompt import SYSTEM_PROMPT`` keeps working
# and a malformed bundled ``router.md`` fails fast at import (mirrors the old
# import-time assertions). ``load_router_md`` caches, so this single read is
# reused by ``build_router_definition``.
SYSTEM_PROMPT = load_router_md()[1]


def _reset_cache_for_tests() -> None:
    """Clear the cached ``(config, prompt)`` so the next :func:`load_router_md`
    re-reads.

    Test-only — production treats ``router.md`` as boot-time configuration."""
    global _cached
    _cached = None
