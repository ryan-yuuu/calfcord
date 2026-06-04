"""The built MCP tool catalog: ``server name -> [McpToolDef, ...]``.

Built once at import time by walking the committed
:mod:`calfcord.mcp.schemas` package (see
:func:`calfcord.mcp.discovery.discover_mcp_catalog`). This mirrors how
:mod:`calfcord.tools` builds its ``TOOL_REGISTRY`` at import.

The catalog is **transport-free and credential-free**: it holds only the
:class:`~calfkit.mcp.McpToolDef` schema objects produced by ``calfkit mcp
codegen``, never a connected :class:`~calfkit.mcp.McpServer`. That keeps
this module **agent-safe** — the agent deployment imports it to advertise
MCP tools to its LLM and to compute Kafka topics, without needing any MCP
server's ``$VAR`` secrets present at import.

Both deployments consume this catalog:

* the **agent** path feeds it to :mod:`calfcord.mcp.schema_build` to build
  schema-only tool nodes, and
* the **bridge** path feeds it to :mod:`calfcord.mcp.config` to build real
  :class:`~calfkit.mcp.McpServer` instances from ``mcp.json``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from calfkit.mcp import McpToolDef

from calfcord.mcp import discovery
from calfcord.mcp import schemas as _schemas_pkg

MCP_CATALOG: Mapping[str, list[McpToolDef]] = MappingProxyType(discovery.discover_mcp_catalog(_schemas_pkg))
"""Server name → list of that server's :class:`~calfkit.mcp.McpToolDef`.

Populated at import time by
:func:`calfcord.mcp.discovery.discover_mcp_catalog` walking
:mod:`calfcord.mcp.schemas`. Insertion order is deterministic
(alphabetical by module name, then by attribute name within a module) so
boot logs and resolved tool surfaces are reproducible. Empty until at
least one ``calfkit mcp codegen`` output module is committed under
``schemas/``.

**Read-only.** Wrapped in :class:`types.MappingProxyType` so consumers
cannot mutate the module global (a stray ``MCP_CATALOG["x"] = ...`` raises
``TypeError``). Tests that need a different catalog inject their own dict
into the factory; this only hardens the shared global."""
