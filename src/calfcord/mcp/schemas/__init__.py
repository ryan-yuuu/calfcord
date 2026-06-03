"""Committed, codegen-produced MCP tool schemas — one module per server.

This package is the discovery target walked by
:func:`calfcord.mcp.discovery.discover_mcp_catalog`. Each module here is
generated output (``calfkit mcp codegen <server>``); the module name is
the MCP server name and each module declares one top-level
:class:`~calfkit.mcp.McpToolDef` constant per tool. Do not hand-edit the
generated modules — see this package's ``README.md`` for the regenerate
workflow.
"""

from __future__ import annotations
