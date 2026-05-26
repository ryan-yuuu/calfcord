"""Registry of calfkit tool nodes available to agents.

Tools an agent declares in its ``.md`` frontmatter under ``tools:`` are
resolved against :data:`TOOL_REGISTRY` at factory build time. The runnable
tool node — the process that actually executes the tool's Python body —
lives in a separate ``calfkit-tools`` deployment; the agent process only
needs each tool's :class:`~calfkit.nodes.ToolNodeDef` for its schema (LLM
tool advertising) and its subscribe topic (where ``Agent`` publishes a
``Call`` when the LLM invokes the tool).

Adding a new tool:
    1. Drop a ``.py`` file in ``src/calfkit_organization/tools/builtin/``
       that declares an ``async def`` decorated by :func:`agent_tool` and
       assigns the resulting :class:`ToolNodeDef` to a module-level
       attribute (the convention is ``<name>_tool``).
    2. Restart ``calfkit-tools``. The auto-discovery loader at
       :func:`~calfkit_organization.tools.discovery.discover_tools` walks
       ``tools/builtin/`` at import time and registers every
       :class:`ToolNodeDef` it finds — no edits to this file required.
    3. The ``calfkit-tools`` runner picks up the same registry and hosts
       the tool's body. The agent process imports this module solely for
       the schema + subscribe-topic that the LLM dispatch needs.

See :mod:`calfkit_organization.tools.discovery` for the discovery rules
(file naming, collision handling, re-export dedup) and
``docs/authoring-tools.md`` for the contributor walkthrough.
"""

from __future__ import annotations

from calfkit.nodes.tool import ToolNodeDef

# ``TOOL_REGISTRY`` must be a defined name before the builtin package
# is imported below. Tool modules transitively import back into this
# package (via bridge/agent code), and Python resolves the cycle by
# binding to whatever ``TOOL_REGISTRY`` references at that point — an
# empty dict here, mutated by ``discover_tools`` once the cycle resolves.
TOOL_REGISTRY: dict[str, ToolNodeDef] = {}
"""Tool name → :class:`ToolNodeDef`. Populated at import time by
:func:`~calfkit_organization.tools.discovery.discover_tools` walking
``tools/builtin/``. Order of insertion is deterministic (alphabetical by
module then by attribute name) so boot logs are reproducible."""

from calfkit_organization.tools import builtin as _builtin  # noqa: E402
from calfkit_organization.tools.discovery import discover_tools  # noqa: E402

discover_tools(_builtin, TOOL_REGISTRY)
