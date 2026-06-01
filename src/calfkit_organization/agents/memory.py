"""Memory-prompt loading and the per-agent runtime instructions hook.

calfcord agents opt into a persistent notepad with ``memory: true`` in their
frontmatter. The "how memory works" explanation is **not** bundled into agent
prompts. Instead:

* the **bridge** reads the template (:func:`load_memory_prompt`) and ships it
  in ``deps`` under :data:`MEMORY_PROMPT_DEPS_KEY` on every invocation (only
  when the deployment has at least one memory-enabled agent);
* ``private_chat`` projects ``deps`` forward, so the template survives the
  A2A hop without per-key plumbing;
* each memory-enabled agent carries a dynamic-instructions hook
  (:func:`memory_instructions`, registered by the factory) that, at runtime,
  reads the template from ``deps``, localizes it to that agent's
  ``memory/<agent_id>/`` directory, and appends it to the agent's
  instructions.

Keeping the template out of agent images preserves calfcord's
distributed-in-space property: the **bridge is the single reader** of the
prompt file; agents and the tools process never read it — they receive or
forward it over Kafka. The text lives in an editable Markdown file
(:file:`memory_prompt.md` beside this module, overridable via
``CALFCORD_MEMORY_PROMPT_PATH`` on the bridge host). Substitution is a plain
:meth:`str.replace` so the literal braces in the template's YAML-frontmatter
example survive untouched.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from calfkit._vendor.pydantic_ai import RunContext

from calfkit_organization.agents.definition import AgentDefinition

_PROMPT_PATH_ENV = "CALFCORD_MEMORY_PROMPT_PATH"
_DEFAULT_PROMPT_PATH = Path(__file__).with_name("memory_prompt.md")
_MEMORY_DIR_PLACEHOLDER = "{{MEMORY_DIR}}"

MEMORY_PROMPT_DEPS_KEY = "memory_prompt"
"""``deps`` key under which the bridge ships the raw (un-localized) template
and the agent hook reads it. ``private_chat`` forwards ``deps`` wholesale, so
this key propagates through A2A chains with no per-key handling."""

_cached_prompt: str | None = None


def load_memory_prompt() -> str:
    """Return the memory-explanation template, cached after the first read.

    Called by the **bridge** (the single reader). Reads
    ``CALFCORD_MEMORY_PROMPT_PATH`` when set, otherwise the bundled
    :file:`memory_prompt.md` beside this module. Cached on a module global;
    an operator editing the file takes effect on the next bridge restart.

    Raises:
        ValueError: if the resolved file cannot be read (e.g. a configured
            override path that doesn't exist) or is empty — an empty template
            would make ``memory: true`` a silent no-op. Callers in the bridge
            degrade gracefully (log + skip injection) rather than failing the
            whole invocation.
    """
    global _cached_prompt
    if _cached_prompt is not None:
        return _cached_prompt
    override = os.getenv(_PROMPT_PATH_ENV)
    path = Path(override).expanduser() if override else _DEFAULT_PROMPT_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        # ``UnicodeError`` (covers ``UnicodeDecodeError``) is a ``ValueError``
        # subclass, not an ``OSError`` — a readable-but-non-UTF-8 override file
        # would otherwise escape as a bare, context-less ``ValueError``.
        raise ValueError(
            f"cannot read memory prompt at {path} ({_PROMPT_PATH_ENV}={override!r}): {e}"
        ) from e
    if not text.strip():
        raise ValueError(f"memory prompt at {path} is empty")
    _cached_prompt = text
    return _cached_prompt


def render_memory_block(template: str, agent_id: str) -> str:
    """Localize ``template`` for one agent: substitute its memory dir, then strip.

    The agent dir is workspace-relative (``memory/<agent_id>/``) on purpose:
    the LLM passes it straight to ``read_file`` / ``write_file``, which resolve
    relative paths against the workspace root in the tools process.
    """
    return template.replace(_MEMORY_DIR_PLACEHOLDER, f"memory/{agent_id}/").strip()


def memory_instructions(agent_id: str) -> Callable[[RunContext[dict]], str | None]:
    """Build the dynamic-instructions hook for ``agent_id``.

    The factory registers the returned callable on memory-enabled agent nodes
    via ``Agent.instructions``. calfkit invokes it per-run with a
    :class:`RunContext` whose ``.deps`` is the deps dict the invoker passed
    (the bridge injects the template; ``private_chat`` forwards it). It returns
    the localized memory block, or ``None`` when no template is present (a
    non-memory deployment, or an invocation path that didn't carry it), so the
    agent degrades gracefully instead of erroring. calfkit appends the result
    to the agent's instructions alongside its system prompt and the per-call
    peer roster.
    """

    def _hook(ctx: RunContext[dict]) -> str | None:
        deps = ctx.deps
        template = deps.get(MEMORY_PROMPT_DEPS_KEY) if isinstance(deps, dict) else None
        if not template:
            return None
        return render_memory_block(template, agent_id)

    return _hook


def memory_prompt_deps_for_registry(specs: Iterable[AgentDefinition]) -> dict[str, str]:
    """Build the ``deps`` entry that ships the memory-prompt template, or ``{}``.

    The bridge is the single reader of the template (:func:`load_memory_prompt`).
    It ships the raw (un-localized) text under :data:`MEMORY_PROMPT_DEPS_KEY` on
    every agent invocation it originates **whenever the deployment has at least
    one memory-enabled agent** — so the template reaches every agent and, because
    ``private_chat`` forwards ``deps`` wholesale, propagates through A2A chains;
    each memory-enabled agent's instructions hook then localizes it.

    Returns ``{}`` when no agent in ``specs`` opted into memory (existing
    deployments stay byte-identical — no template read, no wire cost). Raises
    :class:`ValueError` (propagated from :func:`load_memory_prompt`) when a memory
    agent exists but the template can't be loaded; the **caller** decides how to
    log and degrade — the high-frequency bridge-ingress path dedups the error to a
    single log, while rarer call sites (e.g. the outbox retry) log per occurrence.

    Shared by every bridge path that originates an agent invocation
    (:meth:`~calfkit_organization.bridge.ingress.BridgeIngress._memory_prompt_deps`
    and the outbox retry-with-feedback publish) so the "is memory enabled +
    load the template" decision lives in exactly one place.
    """
    if not any(spec.memory for spec in specs):
        return {}
    return {MEMORY_PROMPT_DEPS_KEY: load_memory_prompt()}


def _reset_cache_for_tests() -> None:
    """Clear the cached template so the next :func:`load_memory_prompt` re-reads.

    Test-only — production treats the prompt file as boot-time configuration."""
    global _cached_prompt
    _cached_prompt = None
