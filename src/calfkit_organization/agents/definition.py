"""Agent identity, runtime hints, and system prompt parsed from ``agents/*.md``.

Each agent is declared as a single Markdown file with YAML frontmatter and
a system-prompt body. The frontmatter declares identity and intrinsic
runtime hints; the body is the system prompt fed to the LLM.

Format (matches Claude Code's ``.claude/agents/*.md`` convention)::

    ---
    name: scheduler
    display_name: "Aksel (Scheduler)"
    description: "Calendar mechanics; book and prep meetings"
    provider: anthropic
    model: claude-sonnet-4-5
    tools: [calendar, email]
    thinking_effort: high
    ---

    You are Aksel, the Scheduler. ...

When ``avatar_url`` is omitted, :func:`parse_agent_md` fills it with a
DiceBear "glass" URL seeded by the agent's name so every assistant gets
a stable, recognizable persona avatar without operators having to host
images. Set ``avatar_url`` explicitly in the frontmatter to override.

The YAML key is ``name`` (Claude Code parity). Internally the field is
``agent_id`` via a Pydantic alias so existing ``spec.agent_id`` access
patterns are preserved across the codebase. The Discord slash command for
each agent is always ``/<name>``; there is no separate ``slash``
frontmatter field.

``thinking_effort`` is the one frontmatter field that is operator-tunable
at runtime — the ``/thinking-effort`` Discord slash command rewrites it
via :mod:`calfkit_organization.agents.md_writer`. Channel subscriptions
and other strictly deployment-specific state still live outside the .md
(see :mod:`calfkit_organization.agents.state`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import frontmatter
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from calfkit_organization.agents.identifier import AGENT_ID_PATTERN
from calfkit_organization.discord.avatar import dicebear_avatar_url

Provider = Literal["anthropic", "openai", "openai-codex"]
"""Supported LLM provider tags for the ``provider`` frontmatter field.

The factory maps each provider to a concrete model-client class:
    - ``"anthropic"`` → :class:`calfkit.AnthropicModelClient`
    - ``"openai"`` → :class:`calfkit.OpenAIModelClient`
    - ``"openai-codex"`` →
      :class:`calfkit_organization.providers.codex.CodexSubscriptionModelClient`
      (routes through ChatGPT Plus/Pro subscription billing rather than
      OpenAI API credits; requires ``uv run calfkit-auth codex login``).
"""

ThinkingEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
"""Operator-facing thinking-effort tiers.

Seven abstract levels mapped to provider-specific reasoning/thinking
parameters in :mod:`calfkit_organization.agents.thinking`. Tier names
parallel Claude Code's effort vocabulary; ``minimal`` is the lightest
non-zero step (a hair above ``none``); ``xhigh`` is a calfkit-specific
step between ``high`` and ``max``.
"""

AgentRole = Literal["assistant", "router"]
"""Agent role — distinguishes ordinary assistant agents from the built-in
routing agent.

``"assistant"`` (default) is what every user-defined ``agents/*.md`` file
produces. ``"router"`` is reserved for the singleton built-in router
agent constructed by :func:`calfkit_organization.router.definition.build_router_definition`;
the factory wires routers differently (single-topic subscription, no
standard gates, ``ToolOutput`` final-output type, explicit
``publish_topic``).
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
    display_name: str
    description: str
    avatar_url: str | None = None
    """Webhook persona avatar URL. ``None`` here means "use the Discord
    webhook's default avatar"; for .md-loaded agents,
    :func:`parse_agent_md` substitutes the per-agent DiceBear default
    when the frontmatter omits or nulls the field, so live assistant
    definitions read from disk always carry a concrete URL."""
    provider: Provider | None = None
    model: str | None = None
    tools: tuple[str, ...] | None = None
    """Tools available to this agent's LLM, resolved against
    :data:`calfkit_organization.tools.TOOL_REGISTRY`.

    Semantics:
        - ``tools:`` omitted from frontmatter (default ``None``) — agent
          gets every registered builtin tool. Convenient default for
          general-purpose assistant agents.
        - ``tools: []`` in frontmatter — agent gets NO tools (text-only).
          Explicit opt-out for read-only or routing-style agents.
        - ``tools: [a, b]`` — agent gets exactly those tools.

    Security note: the "all by default" behaviour means a new
    ``agents/<name>.md`` ships with ``shell`` / ``write_file`` /
    ``edit_file`` access to the workspace bind-mounted into the
    ``calfkit-tools`` container unless the operator narrows the list
    explicitly. If you need a restricted-tools agent, add the
    ``tools:`` line. See :doc:`docs/authoring-agents` for the security
    model.

    Router agents must omit ``tools:`` entirely (or set ``tools: []``);
    the validator rejects routers that declare non-empty tools."""
    thinking_effort: ThinkingEffort | None = None
    role: AgentRole = "assistant"
    """Agent role. Defaults to ``"assistant"`` — ordinary user-defined
    agent. ``"router"`` is reserved for the singleton built-in routing
    agent; the factory uses a different wiring path for routers (no
    standard gates, single-topic subscription, ``ToolOutput`` final
    output type). User-authored ``agents/*.md`` should not set this
    field; the validator does not forbid it but operators wiring a
    second router will hit a registry boot error in :class:`AgentRegistry`."""
    publish_topic: str | None = Field(default=None, min_length=1)
    """Optional explicit Kafka publish topic for the agent's
    ``ReturnCall``. Used by routers to declare their structured-output
    destination (where the fan-out consumer subscribes). ``None`` for
    assistant agents — they emit ``ReturnCall`` to the inbound frame's
    ``callback_topic`` (i.e., the bridge's ``discord.outbox``), which
    is the standard calfkit dispatch pattern."""
    history_turns: int = Field(default=30, ge=0, le=100)
    """Number of recent channel messages the bridge fetches and projects
    into ``message_history`` on every invocation of this agent.

    - ``0`` disables history fetching for this agent entirely (no
      Discord REST call; agent runs with the system prompt + user
      prompt only).
    - The upper bound (100) is Discord's per-call REST cap for
      ``channel.history(limit=...)``; raising it would force
      pagination, which is out of scope for v1.
    - The default (30) is a reasonable balance between context quality
      and token cost: ~30 messages of ~100 tokens average is ~3K
      input tokens per invocation, which is trivial on small models
      and acceptable on larger ones.

    Set in ``.md`` frontmatter::

        ---
        name: scribe
        ...
        history_turns: 30
        ---

    The router's analogous knob is the ``CALFKIT_ROUTER_HISTORY_TURNS``
    env var (router is constructed in code, not from ``.md``)."""
    memory: bool = False
    """Opt in to a persistent per-agent notepad. When ``True``, the factory
    registers a runtime instructions hook
    (:func:`calfkit_organization.agents.memory.memory_instructions`) that
    appends the memory-explanation block to this agent's instructions on every
    invocation — the stored ``system_prompt`` is left unchanged. The block text
    is read only by the bridge (from the editable ``memory_prompt.md``,
    overridable via ``CALFCORD_MEMORY_PROMPT_PATH``) and shipped to the agent in
    ``deps``; the hook localizes it to ``memory/<agent_id>/`` in the shared
    workspace, where the agent keeps one-fact-per-file memories plus a
    ``MEMORY.md`` index, managed with the ordinary filesystem tools.

    Default ``False`` so existing agents are unchanged. A memory-enabled agent
    must have the ``read_file`` and ``write_file`` tools — the factory enforces
    this at build time. See :doc:`docs/authoring-agents` and
    ``docs/design/agent-memory-plan.md``."""
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

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, v: str) -> str:
        if not (1 <= len(v) <= 80):
            raise ValueError(f"display_name must be 1-80 chars, got {len(v)}")
        if v.lower() == "clyde":
            raise ValueError("display_name 'Clyde' is rejected by Discord webhooks")
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

    @model_validator(mode="after")
    def _validate_router_constraints(self) -> AgentDefinition:
        """Enforce role-specific invariants on ``tools`` and ``publish_topic``.

        Routers:
            - must declare no ``tools`` (the router uses pydantic-ai's
              ``ToolOutput`` pattern, where the "tool" is a
              schema-providing pseudo-tool whose args ARE the output —
              the body never runs; declaring real function tools
              alongside would muddy the LLM's tool list and the
              factory's wiring)
            - must declare a ``publish_topic`` (the fan-out consumer
              subscribes there; without it the router has no
              downstream consumer pathway)

        Assistants:
            - must NOT declare a ``publish_topic`` (assistants emit
              ``ReturnCall`` to the inbound frame's ``callback_topic``,
              not to a fixed published topic; setting one would be a
              silent no-op that an operator might mistake for working
              custom-output wiring).
        """
        if self.role == "router":
            if self.tools:
                raise ValueError(
                    f"router agent {self.agent_id!r} must declare no tools; "
                    f"got tools={list(self.tools)!r}"
                )
            if not self.publish_topic:
                raise ValueError(
                    f"router agent {self.agent_id!r} must declare a "
                    f"publish_topic; the fan-out consumer subscribes there"
                )
        else:  # role == "assistant"
            if self.publish_topic is not None:
                raise ValueError(
                    f"agent {self.agent_id!r} has role='assistant' but "
                    f"declares publish_topic={self.publish_topic!r}; "
                    f"publish_topic is reserved for routers — "
                    f"assistants emit ReturnCall to the inbound "
                    f"frame's callback_topic (set by the caller)"
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
        # which would escape callers that catch only ``ValueError`` (e.g.
        # ``calfcord-package-agents``'s narrowed ``except``). Re-raise as
        # ``ValueError`` so the docstring's contract holds and the
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
    # Fill the per-agent DiceBear default when the .md omits avatar_url
    # (or sets it to ``null``). Done here at the load boundary rather
    # than as an AgentDefinition validator so code-built definitions —
    # the router, test fixtures, state-event projections — keep their
    # explicit ``None`` semantics.
    if metadata.get("avatar_url") is None:
        metadata["avatar_url"] = dicebear_avatar_url(declared_name)
    return AgentDefinition(**metadata)
