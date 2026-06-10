"""Shared CLI-side mcp.json enumeration with the strict error policy.

Several verbs consult the configured MCP server names with the same policy —
an *invalid* config fails the verb with the actionable loader message, not
just the server's own boot (``calfcord start``, ``mcp start --all``,
``deploy k8s``). This is that one policy, in one place; the tolerant
surfaces (logs, the tools editor, init's live finish) own their distinct
degrade behaviors at their call sites.
"""

from __future__ import annotations

from calfcord.mcp.config import McpConfigError, list_server_names, resolve_config_path


def configured_mcp_servers_or_none() -> list[str] | None:
    """Server names from mcp.json, or ``None`` after printing the error."""
    try:
        return list_server_names(resolve_config_path())
    except McpConfigError as exc:
        print(f"error: {exc}")
        return None
