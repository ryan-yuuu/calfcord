"""MCP integration for calfcord: selector parsing, schema catalog, bridge.

This package straddles a hard deployment boundary, and the import graph
is shaped deliberately around it:

* **Agent-safe** modules carry no transport and no credentials. Importing
  them never opens a subprocess, never touches the network, and never
  requires an MCP server's ``$VAR`` secrets to be present in the
  environment:

  - :mod:`calfcord.mcp.selector` — pure leaf; parse/validate ``mcp/...``
    selectors. (Re-exported here; see below.)
  - :mod:`calfcord.mcp.discovery` — walk the committed ``schemas``
    package and collect :class:`~calfkit.mcp.McpToolDef` instances.
  - :mod:`calfcord.mcp.catalog` — the built ``name -> [McpToolDef]``
    catalog (no transport).
  - :mod:`calfcord.mcp.schema_build` — resolve selectors to
    schema-only :class:`~calfkit.models.node_schema.BaseToolNodeSchema`
    nodes for the agent's LLM tool surface + Kafka routing.

* **Bridge-only** modules host the *real* MCP servers and therefore
  require their credentials at import time (calfkit expands ``$VAR`` at
  :class:`~calfkit.mcp.McpServer` construction):

  - :mod:`calfcord.mcp.servers` — the credentialed ``McpServer`` registry.
  - :mod:`calfcord.mcp.runner` — the ``calfkit-mcp`` bridge entry point.

  The agent deployment must **never** import these two.

This ``__init__`` is intentionally a *leaf*: it re-exports only the
:mod:`~calfcord.mcp.selector` API. It does **not** import ``catalog``,
``schema_build``, ``discovery``, ``servers``, or ``runner``. The reason is
concrete — ``calfcord.agents.definition`` imports the selector helpers to
validate frontmatter, and that import runs *this* module. If this module
pulled in ``catalog`` it would build the schema catalog (and import
:mod:`calfkit`) as a side effect of merely parsing a selector string, and
if it pulled in ``servers`` it would breach the agent/bridge boundary
entirely. Keeping the package import cheap preserves both properties.

Consumers therefore import the heavier modules *directly*::

    from calfcord.mcp.catalog import MCP_CATALOG
    from calfcord.mcp.schema_build import resolve_mcp_selectors
    from calfcord.mcp.servers import MCP_SERVERS  # bridge process only
"""

from __future__ import annotations

from calfcord.mcp.selector import (
    MCP_SELECTOR_PREFIX,
    McpSelector,
    is_mcp_selector,
    is_valid_server_name,
    parse_mcp_selector,
    validate_mcp_selector,
)

__all__ = [
    "MCP_SELECTOR_PREFIX",
    "McpSelector",
    "is_mcp_selector",
    "is_valid_server_name",
    "parse_mcp_selector",
    "validate_mcp_selector",
]
