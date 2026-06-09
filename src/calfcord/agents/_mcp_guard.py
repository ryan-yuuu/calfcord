"""Reject MCP tool selectors with one canonical, actionable error.

calfkit removed the MCP adaptor in 0.7.0 and v2 MCP support is planned but not
yet available, so calfcord temporarily supports no MCP tools. Rather than let a
stale ``mcp/...`` entry fail later as a vague "unknown tool", the two tool-entry
gates (parse-time in :mod:`calfcord.agents.definition`, write-time in
:mod:`calfcord.agents.md_writer`) reject it here with a message that says exactly
what to do.

This is deliberately the *only* place the ``mcp/`` prefix and the rejection
message live, so the user-facing string cannot drift between the read and write
paths. It replaces — and is strictly smaller than — the deleted
``calfcord.mcp.selector`` module.
"""

from __future__ import annotations

MCP_TOOL_PREFIX = "mcp/"
"""Prefix that marked an MCP tool selector in agent frontmatter (no longer supported)."""


def is_mcp_tool(entry: str) -> bool:
    """True if ``entry`` is (or looks like) an MCP tool selector."""
    return entry.startswith(MCP_TOOL_PREFIX)


def mcp_unsupported_error(entry: str) -> ValueError:
    """The canonical rejection for an ``mcp/...`` tool entry."""
    return ValueError(
        "MCP tools are not currently supported — calfkit removed the MCP adaptor "
        "in 0.7.0 and v2 MCP support is planned. "
        f"Remove {entry!r} from this agent's tools."
    )
