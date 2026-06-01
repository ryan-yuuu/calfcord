"""Schema for the router's runtime config — the ``router.md`` front matter.

The router's prompt and its runtime knobs (``provider``, ``model``,
``thinking_effort``, ``history_turns``) are authored together in a single
bundled :file:`router.md` (see :mod:`calfkit_organization.router.prompt` for
the loader): YAML front matter for the config, Markdown body for the prompt.
This module defines the front-matter schema only.

The schema is intentionally narrow — model configuration only. The router's
identity (``agent_id``, ``display_name``, ``role``, ``publish_topic``,
``tools``, ``system_prompt``) is project infrastructure and not
operator-tunable; reserved fields appearing in the front matter are rejected
at load time via ``extra="forbid"`` so an operator cannot accidentally break
the singleton invariants enforced by :class:`AgentRegistry`.

Every field is optional: a field omitted from the front matter falls through
to the in-code default in :mod:`calfkit_organization.router.definition`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from calfkit_organization.agents.definition import Provider, ThinkingEffort


class RouterConfig(BaseModel):
    """Router runtime knobs parsed from the ``router.md`` front matter.

    All fields are optional — a missing field falls through to the in-code
    default in :mod:`calfkit_organization.router.definition`.

    ``extra="forbid"`` rejects any unknown key so a typo
    (``provder: openai``) or a reserved field (``system_prompt:``,
    ``display_name:``) surfaces at boot rather than silently dropping
    on the floor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider | None = None
    # ``min_length=1`` so an empty ``model: ""`` fails at the boundary rather
    # than being silently swallowed by the ``config.model or _DEFAULT_MODEL``
    # fallback in ``definition.py`` — matches the non-empty constraints on every
    # other operator-supplied string in the project (e.g. ``publish_topic``).
    model: str | None = Field(default=None, min_length=1)
    thinking_effort: ThinkingEffort | None = None
    history_turns: int | None = Field(default=None, ge=0, le=100)
