"""Agent identity, runtime hints, and system prompt parsed from ``agents/*.md``.

Each agent is declared as a single Markdown file with YAML frontmatter and
a system-prompt body. The frontmatter declares identity and intrinsic
runtime hints; the body is the system prompt fed to the LLM.

Format (matches Claude Code's ``.claude/agents/*.md`` convention)::

    ---
    name: scheduler
    description: "Calendar mechanics; book and prep meetings"
    provider: anthropic
    model: claude-sonnet-4-5
    tools: [calendar, email]
    thinking_effort: high
    ---

    You are Aksel, the Scheduler. ...

The YAML key is ``name`` (Claude Code parity). Internally the field is
``agent_id`` via a Pydantic alias so existing ``spec.agent_id`` access
patterns are preserved across the codebase. The Discord slash command for
each agent is always ``/<name>``; there is no separate ``slash``
frontmatter field.

``thinking_effort`` is the one frontmatter field that is operator-tunable
at runtime — the ``/thinking-effort`` Discord slash command rewrites it
via :mod:`calfcord.agents.md_writer`. Channel subscriptions
and other strictly deployment-specific state still live outside the .md
(see :mod:`calfcord.agents.state`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import frontmatter
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from calfcord.agents.identifier import AGENT_ID_PATTERN
from calfcord.mcp.selector import is_mcp_selector, validate_mcp_selector

Provider = Literal["anthropic", "openai", "openai-codex"]
"""Supported LLM provider tags for the ``provider`` frontmatter field.

The factory maps each provider to a concrete model-client class:
    - ``"anthropic"`` → :class:`calfkit.AnthropicModelClient`
    - ``"openai"`` → :class:`calfkit.OpenAIModelClient`
    - ``"openai-codex"`` →
      :class:`calfcord.providers.codex.CodexSubscriptionModelClient`
      (routes through ChatGPT Plus/Pro subscription billing rather than
      OpenAI API credits; requires ``uv run calfkit-auth codex login``).
"""

ThinkingEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
"""Operator-facing thinking-effort tiers.

Seven abstract levels mapped to provider-specific reasoning/thinking
parameters in :mod:`calfcord.agents.thinking`. Tier names
parallel Claude Code's effort vocabulary; ``minimal`` is the lightest
non-zero step (a hair above ``none``); ``xhigh`` is a calfkit-specific
step between ``high`` and ``max``.
"""


class AgentDefinition(BaseModel):
    """One agent's declarative definition: identity, runtime hints, and system prompt.

    Validators mirror the constraints Discord imposes on slash commands and
    webhook usernames so misconfiguration fails at load time rather than at
    first invocation. The Discord slash command name is always derived as
    ``/<agent_id>`` — the ``agent_id`` validator already enforces the
    Discord-compatible ``[a-z0-9_-]{1,32}`` shape.
    """

    # ``extra="forbid"`` surfaces frontmatter typos (``provder: openai``,
    # ``thiking_effort: high``) at parse time rather than silently dropping
    # them — important now that a slash command can rewrite the same file.
    # It also catches stale ``slash:`` lines left behind from the removal
    # of the dedicated slash field.
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    agent_id: str = Field(..., alias="name")
    description: str
    provider: Provider | None = None
    model: str | None = None
    tools: tuple[str, ...] | None = None
    """Tools available to this agent's LLM, resolved against
    :data:`calfcord.tools.TOOL_REGISTRY`.

    Semantics:
        - ``tools:`` omitted from frontmatter (default ``None``) — agent
          gets every registered builtin tool. Convenient default for
          general-purpose assistant agents.
        - ``tools: []`` in frontmatter — agent gets NO tools (text-only).
          Explicit opt-out for read-only or routing-style agents.
        - ``tools: [a, b]`` — agent gets exactly those tools.

    Security note: the "all by default" behaviour means a new
    ``agents/<name>.md`` ships with ``terminal`` / ``execute_code`` /
    ``write_file`` / ``patch`` access to the workspace bind-mounted into
    the ``calfkit-tools`` container unless the operator narrows the list
    explicitly. If you need a restricted-tools agent, add the
    ``tools:`` line. See :doc:`docs/authoring-agents` for the security
    model."""
    thinking_effort: ThinkingEffort | None = None
    publish_topic: str | None = Field(default=None, min_length=1)
    """Reserved and unused: it must be left ``None``. Every agent emits its
    ``ReturnCall`` to the inbound frame's ``callback_topic`` (the caller's reply
    topic, e.g. the bridge's ``discord.outbox``) -- the standard calfkit dispatch
    pattern -- so there is no fixed published-output topic. A non-``None`` value
    is rejected by :meth:`_forbid_publish_topic` so a stale setting fails loudly
    rather than silently doing nothing."""
    memory: bool = False
    """Opt in to a persistent per-agent notepad. When ``True``, the factory
    registers a runtime instructions hook
    (:func:`calfcord.agents.memory.memory_instructions`) that
    appends the memory-explanation block to this agent's instructions on every
    invocation — the stored ``system_prompt`` is left unchanged. The block text
    is read only by the bridge (from the editable ``memory_prompt.md``,
    overridable via ``CALFCORD_MEMORY_PROMPT_PATH``) and shipped to the agent in
    ``deps``; the hook localizes it to ``memory/<agent_id>/`` in the shared
    workspace, where the agent keeps one-fact-per-file memories plus a
    ``MEMORY.md`` index, managed with the ordinary filesystem tools.

    Default ``False`` so existing agents are unchanged. A memory-enabled agent
    must have the ``read_file`` and ``write_file`` tools — the factory enforces
    this at build time. See :doc:`docs/authoring-agents`."""
    a2a: bool | tuple[str, ...] = True
    """Native agent-to-agent messaging (calfkit's auto-injected ``message_agent``
    tool + ``peers=[Messaging(...)]``).

    - ``True`` (default) — reach any agent (``Messaging(discover=True)``); the
      live peer directory is rendered into the agent's prompt.
    - ``False`` — no A2A (the agent gets no ``message_agent`` tool).
    - ``[name, ...]`` — restrict to those peers (``Messaging(*names)``).

    Replaces the old opt-in (listing the now-removed ``private_chat`` tool);
    default-on makes native A2A frictionless."""
    handoff: bool | tuple[str, ...] = True
    """Native handoff — transfer the caller's turn to a peer
    (``peers=[Handoff(...)]``; the model emits ``HandoffRequest``).

    - ``True`` (default) — hand off to any agent (``Handoff(discover=True)``).
    - ``False`` — no handoff.
    - ``[name, ...]`` — restrict to those targets (``Handoff(*names)``).

    Replaces the in-channel ``@<agent_id>`` handoff convention (C7)."""
    system_prompt: str
    source_path: Path | None = Field(default=None, exclude=True, repr=False)
    """Path to the ``.md`` file this definition was parsed from. Set by
    :func:`parse_agent_md`; ``None`` for in-memory test constructions
    and for the built-in router (which is constructed in code, not
    parsed from disk). ``exclude=True`` prevents accidental round-trip
    into a YAML dump; ``repr=False`` keeps logs tidy. Required for the
    ``/thinking-effort`` slash command's rewrite."""

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        if not AGENT_ID_PATTERN.fullmatch(v):
            raise ValueError(f"name must match [a-z0-9_-]{{1,32}}, got {v!r}")
        return v

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v: str) -> str:
        if not (1 <= len(v) <= 100):
            raise ValueError(f"description must be 1-100 chars (Discord slash limit), got {len(v)}")
        return v

    @field_validator("system_prompt")
    @classmethod
    def _validate_system_prompt(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("system_prompt (markdown body) must be non-empty")
        return v

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Syntax-check ``mcp/...`` tool entries; let bare builtin names through.

        An ``mcp/...`` entry must be a well-formed selector
        (``mcp/<server>`` or ``mcp/<server>/<tool>``); all malformed entries
        are aggregated into ONE :class:`ValueError` naming each offending
        string, so an operator fixes the whole ``tools:`` line in a single
        pass. Whether the named server is configured or running is a
        *runtime* concern (the capability view resolves selectors per turn),
        deliberately not checked here — this is the authoritative syntactic
        gate for every read path: the agent process parses each ``.md``
        through here before booting.

        Bare names (anything not starting with ``mcp/`` — ``terminal``,
        ``calendar``, …) pass through **untouched**. Whether a bare name
        actually resolves to a registered builtin is checked later, by
        :meth:`AgentFactory._resolve_tools` against the factory's tool registry
        (defaults to :data:`~calfcord.tools.TOOL_REGISTRY`); rejecting
        arbitrary bare names here would couple this leaf schema to the tool
        registry and break code-built definitions that name tools the local
        registry doesn't (yet) hold.
        """
        if v is None:
            return v
        bad: list[str] = []
        for entry in v:
            if not is_mcp_selector(entry):
                continue
            try:
                validate_mcp_selector(entry)
            except ValueError as exc:
                bad.append(f"{entry!r}: {exc}")
        if bad:
            raise ValueError("malformed MCP tool selector(s) in tools: " + "; ".join(bad))
        return v

    @model_validator(mode="after")
    def _forbid_publish_topic(self) -> AgentDefinition:
        """Reject a non-``None`` ``publish_topic`` -- it must be left unset.

        ``publish_topic`` was reserved for the built-in router, removed in the
        0.12 migration. With no router, every agent emits its ``ReturnCall`` to
        the inbound frame's ``callback_topic`` (the caller's reply topic), so a
        ``publish_topic`` would be a silent no-op an operator might mistake for
        working custom-output wiring. Reject it at validation so a stale setting
        fails loudly rather than doing nothing.
        """
        if self.publish_topic is not None:
            raise ValueError(
                f"agent {self.agent_id!r} declares publish_topic="
                f"{self.publish_topic!r}, which is not supported; agents emit "
                f"ReturnCall to the inbound frame's callback_topic (set by the "
                f"caller). Remove the publish_topic field."
            )
        return self


def parse_agent_md(path: Path) -> AgentDefinition:
    """Parse one ``agents/<name>.md`` file into an :class:`AgentDefinition`.

    Enforces ``path.stem == frontmatter["name"]`` so the on-disk identifier
    and the declared name cannot drift.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if the file has no frontmatter, the YAML is malformed,
            the name does not match the filename stem, or any field fails
            validation.
    """
    try:
        post = frontmatter.load(path)
    except yaml.YAMLError as e:
        # ``frontmatter.load`` lets ``yaml.YAMLError`` propagate unchanged,
        # which would escape callers that catch only ``ValueError``. Re-raise
        # as ``ValueError`` so the docstring's contract holds and the
        # malformed-YAML path is indistinguishable from a malformed-name
        # path at the caller's seam.
        raise ValueError(f"{path}: malformed YAML frontmatter: {e}") from e
    metadata = dict(post.metadata)
    body = post.content

    if not metadata:
        raise ValueError(f"{path}: missing YAML frontmatter")

    declared_name = metadata.get("name")
    expected_name = path.stem
    if declared_name != expected_name:
        raise ValueError(
            f"{path}: frontmatter name={declared_name!r} does not match filename stem {expected_name!r}"
        )

    metadata["system_prompt"] = body.strip()
    # Resolve so the path remains valid even if the process later
    # ``os.chdir``s — the bridge daemon doesn't today, but a future
    # plugin or signal handler could.
    metadata["source_path"] = path.resolve()
    return AgentDefinition(**metadata)
