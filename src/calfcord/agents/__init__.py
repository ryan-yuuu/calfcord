"""Agent definitions, factory, and process runner.

Public surface:
    AgentDefinition  — parsed agent identity + runtime hints + system prompt
    parse_agent_md   — parse one ``.md`` file into an AgentDefinition
    load_agents_dir  — parse all ``.md`` files in a directory
    ThinkingEffort   — Literal of operator-facing effort tiers
    AgentFactory     — constructs a calfkit Worker from a definition
    resolve_provider — shared provider-resolution fallback chain
"""

from calfcord.agents.definition import (
    AgentDefinition,
    ThinkingEffort,
    parse_agent_md,
)
from calfcord.agents.factory import AgentFactory, resolve_provider
from calfcord.agents.loader import load_agents_dir

__all__ = [
    "AgentDefinition",
    "AgentFactory",
    "ThinkingEffort",
    "load_agents_dir",
    "parse_agent_md",
    "resolve_provider",
]
