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
    ---

    You are Aksel, the Scheduler. ...

The YAML key is ``name`` (Claude Code parity). Internally the field is
``agent_id`` via a Pydantic alias so existing ``spec.agent_id`` access
patterns are preserved across the codebase.

Channel subscriptions and other deployment-specific state are deliberately
**not** in the frontmatter; keeping the ``.md`` portable across deployments
is a core design property. Per-deployment runtime state lives in
``state/agents/<name>.json``; see :mod:`calfkit_organization.agents.state`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import frontmatter
from pydantic import BaseModel, ConfigDict, Field, field_validator

_NAME_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")

Provider = Literal["anthropic", "openai"]
"""Supported LLM provider tags for the ``provider`` frontmatter field.

The factory maps each provider to a concrete model-client class:
    - ``"anthropic"`` → :class:`calfkit.AnthropicModelClient`
    - ``"openai"`` → :class:`calfkit.OpenAIModelClient`
"""


class AgentDefinition(BaseModel):
    """One agent's declarative definition: identity, runtime hints, and system prompt.

    Validators mirror the constraints Discord imposes on slash commands and
    webhook usernames so misconfiguration fails at load time rather than at
    first invocation.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    agent_id: str = Field(..., alias="name")
    slash: str
    display_name: str
    description: str
    avatar_url: str | None = None
    provider: Provider | None = None
    model: str | None = None
    tools: tuple[str, ...] = ()
    system_prompt: str

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        if not _NAME_PATTERN.fullmatch(v):
            raise ValueError(f"name must match [a-z0-9_-]{{1,32}}, got {v!r}")
        return v

    @field_validator("slash")
    @classmethod
    def _validate_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"slash must start with '/', got {v!r}")
        if not _NAME_PATTERN.fullmatch(v[1:]):
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
    return AgentDefinition(**metadata)
