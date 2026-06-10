"""calfcord's MCP integration (calfkit 0.9.0 toolbox API).

Two strictly separated halves, mirroring the deployment boundary:

* **Agent path** (any host): :mod:`calfcord.mcp.selector` parses the
  ``mcp/...`` entries in agent frontmatter and
  :mod:`calfcord.mcp.agent_select` resolves them per turn against the
  capability view calfkit maintains from the ``mcp.capabilities`` topic.
  Schema-free and secret-free — agent hosts never read ``mcp.json``.

* **Server path** (the host running the MCP servers):
  :mod:`calfcord.mcp.config` loads ``mcp.json`` (commands, URLs,
  credentials) and :mod:`calfcord.mcp.runner` hosts one
  :class:`calfkit.mcp.mcp_toolbox.MCPToolbox` per ``calfcord run mcp
  <server>`` process.

This package deliberately re-exports nothing: importing a submodule states
which side of the boundary the importer is on, and the import-isolation
test (``tests/mcp/test_import_isolation.py``) holds the agent path to it.
"""
