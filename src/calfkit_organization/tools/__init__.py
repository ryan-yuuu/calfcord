"""Registry of calfkit tool nodes available to agents.

Tools an agent declares in its ``.md`` frontmatter under ``tools:`` are
resolved against :data:`TOOL_REGISTRY` at factory build time. The runnable
tool node — the process that actually executes the tool's Python body —
lives in a separate ``calfkit-tools`` deployment; the agent process only
needs each tool's :class:`~calfkit.nodes.ToolNodeDef` for its schema (LLM
tool advertising) and its subscribe topic (where ``Agent`` publishes a
``Call`` when the LLM invokes the tool).

Adding a new tool:
    1. Define the tool function under ``calfkit_organization.tools`` and
       decorate with ``@agent_tool`` to produce a :class:`ToolNodeDef`.
    2. Register it in :data:`TOOL_REGISTRY` below by importing the
       module and inserting the node by its tool name.
    3. The ``calfkit-tools`` runner imports the same node and registers
       it on its :class:`Worker` so the body runs in that process.
"""

from __future__ import annotations

from calfkit.nodes.tool import ToolNodeDef

# The registry is intentionally defined BEFORE any tool module is imported.
# Tool modules transitively import bridge code, which imports agent code,
# which imports the agent factory, which imports back into this module —
# the registry name must already exist by that point so the cycle resolves
# to an empty dict (then mutated below once the imports complete).
TOOL_REGISTRY: dict[str, ToolNodeDef] = {}
"""Tool name → :class:`ToolNodeDef`. Keep entries in alphabetical order
for easy scanning."""

from calfkit_organization.tools.private_chat import private_chat_tool  # noqa: E402

TOOL_REGISTRY["private_chat"] = private_chat_tool
