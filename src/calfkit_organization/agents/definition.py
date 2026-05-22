"""Agent identity, runtime hints, and system prompt parsed from ``agents/*.md``.

Each agent is declared as a single Markdown file with YAML frontmatter and
a system-prompt body. The frontmatter declares identity and intrinsic
runtime hints; the body is the system prompt fed to the LLM.

Format (matches Claude Code's ``.claude/agents/*.md`` convention)::

    ---
    name: scheduler
    slash: /scheduler
    display_name: "Aksel (Scheduler)"
    description: "Calendar mechanics; book and prep meetings"
    avatar_url: null
    provider: anthropic
    model: claude-sonnet-4-5
    tools: [calendar, email]
    thinking_effort: high
    ---

    You are Aksel, the Scheduler. ...

The YAML key is ``name`` (Claude Code parity). Internally the field is
``agent_id`` via a Pydantic alias so existing ``spec.agent_id`` access
patterns are preserved across the codebase.

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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from calfkit_organization.agents.identifier import AGENT_ID_PATTERN

Provider = Literal["anthropic", "openai"]
"""Supported LLM provider tags for the ``provider`` frontmatter field.

The factory maps each provider to a concrete model-client class:
    - ``"anthropic"`` → :class:`calfkit.AnthropicModelClient`
    - ``"openai"`` → :class:`calfkit.OpenAIModelClient`
"""

ThinkingEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
"""Operator-facing thinking-effort tiers.

Six abstract levels mapped to provider-specific reasoning/thinking
parameters in :mod:`calfkit_organization.agents.thinking`. Tier names
parallel Claude Code's effort vocabulary; ``xhigh`` is a calfkit-specific
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
    first invocation.
    """

    # ``extra="forbid"`` surfaces frontmatter typos (``provder: openai``,
    # ``thiking_effort: high``) at parse time rather than silently dropping
    # them — important now that a slash command can rewrite the same file.
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    agent_id: str = Field(..., alias="name")
    slash: str
    display_name: str
    description: str
    avatar_url: str | None = None
    provider: Provider | None = None
    model: str | None = None
    tools: tuple[str, ...] = ()
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

    @field_validator("slash")
    @classmethod
    def _validate_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"slash must start with '/', got {v!r}")
        if not AGENT_ID_PATTERN.fullmatch(v[1:]):
            raise ValueError(f"slash name (after '/') must match [a-z0-9_-]{{1,32}}, got {v!r}")
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
    def _validate_router_constraints(self) -> "AgentDefinition":
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
    post = frontmatter.load(path)
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
