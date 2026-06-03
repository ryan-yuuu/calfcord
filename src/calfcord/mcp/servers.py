"""Real, credentialed MCP servers — **bridge process only**.

This module hosts the live :class:`~calfkit.mcp.McpServer` instances the
MCP bridge deployment (:mod:`calfcord.mcp.runner`, the ``calfkit-mcp``
entry point) exposes on a calfkit :class:`~calfkit.worker.Worker`. Each
server here carries real transport — a ``stdio`` command or an ``http``
endpoint — and real credentials supplied via ``$VAR`` references.

**The agent deployment must NEVER import this module.** calfkit expands
``$VAR`` references at :class:`~calfkit.mcp.McpServer` *construction* time
(``expand_env`` inside ``McpServer.stdio`` / ``McpServer.http``), raising
on any unset variable. So merely importing this module requires every
MCP server's secrets to be present in the environment — a requirement that
belongs to the bridge, not the agent. Importing it from the agent path
would both breach the deployment boundary and make the agent unbootable
without the bridge's secrets. The agent path stays on the transport-free
:data:`calfcord.mcp.catalog.MCP_CATALOG` and
:mod:`calfcord.mcp.schema_build` instead.

Adding a server:

* Commit its codegen schema as ``schemas/<server>.py`` (so the catalog and
  the agent-facing tool surface know about it), then
* Register the live server below, reusing the catalog's tool list and an
  explicit ``name=`` so the wire topics (``mcp.<server>.<tool>.*``) match
  the schema-only nodes the agent builds.
"""

from __future__ import annotations

from calfkit.mcp import McpServer

from calfcord.mcp.catalog import MCP_CATALOG

__all__ = ["MCP_CATALOG", "MCP_SERVERS"]

MCP_SERVERS: dict[str, McpServer] = {
    # Empty until at least one credentialed MCP server is registered.
    #
    # Example — host the reference "everything" server over stdio, reusing
    # its committed schema (schemas/everything.py) so the bridge's live
    # tool surface matches the agent's schema-only nodes exactly:
    #
    #     "everything": McpServer.stdio(
    #         "npx",
    #         "-y",
    #         "@modelcontextprotocol/server-everything",
    #         tools=MCP_CATALOG["everything"],
    #         name="everything",
    #     ),
}
"""MCP server name → live :class:`~calfkit.mcp.McpServer`.

Hosted by the ``calfkit-mcp`` bridge worker. Empty for now; see the
commented example above and the module docstring for the import-boundary
contract. ``MCP_CATALOG`` is re-exported (via ``__all__``) so registrations
can reuse a server's committed tool schemas as the ``tools=`` argument."""
