"""Load all :class:`AgentDefinition`s from a directory of Markdown files.

Each ``<name>.md`` file in the directory is parsed via
:func:`parse_agent_md`. Hidden files (``.``-prefixed) and non-``.md``
files are ignored.

The loader also resolves the ``tools: omitted → all`` default at parse
time so downstream consumers (factory, phonebook, peer_roster) see a
concrete tuple of tool names rather than the ``None`` sentinel. See
:attr:`AgentDefinition.tools` for the explicit / implicit semantics.

Cross-agent uniqueness of ``slash`` and ``display_name`` is the
:class:`AgentRegistry`'s concern, not the loader's. The filesystem itself
prevents duplicate ``agent_id`` (one ``.md`` file per name).
"""

from __future__ import annotations

import logging
from pathlib import Path

from calfkit_organization.agents.definition import AgentDefinition, parse_agent_md

logger = logging.getLogger(__name__)


def _resolve_default_tools(definition: AgentDefinition) -> AgentDefinition:
    """Expand ``tools=None`` (frontmatter omitted) to every registered tool.

    Only applies to assistant agents. Routers keep ``tools=None``; their
    validator forbids declaring tools at all and the router-build path
    in :class:`AgentFactory` skips tool resolution.

    The TOOL_REGISTRY import is lazy so this module stays importable
    without dragging in the tools subpackage at agent-definition parse
    time (which loader.py used to be free of).
    """
    if definition.role == "router":
        return definition
    if definition.tools is not None:
        return definition  # explicit list (including explicit empty)
    from calfkit_organization.tools import TOOL_REGISTRY

    return definition.model_copy(update={"tools": tuple(sorted(TOOL_REGISTRY))})


def load_agents_dir(path: Path) -> list[AgentDefinition]:
    """Scan ``path`` for ``*.md`` files and parse each into an :class:`AgentDefinition`.

    Returns the definitions sorted by ``agent_id`` for deterministic ordering.
    Any agent whose frontmatter omits ``tools:`` is normalized to receive
    every registered builtin tool — see :func:`_resolve_default_tools`.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        NotADirectoryError: if ``path`` is not a directory.
        ValueError: if any individual file fails to parse or validate.
    """
    if not path.exists():
        raise FileNotFoundError(f"agents directory does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"agents path is not a directory: {path}")

    md_files = sorted(p for p in path.glob("*.md") if not p.name.startswith("."))
    definitions = [_resolve_default_tools(parse_agent_md(p)) for p in md_files]
    logger.info("loaded %d agent definition(s) from %s", len(definitions), path)
    return definitions
