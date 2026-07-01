"""Agent-path MCP tool selection — schema-free, ``mcp.json``-free.

calfkit resolves an agent's MCP tools per turn by calling each declared
``ToolSelector`` against the capability view (the ``KafkaTable`` projection
of the ``mcp.capabilities`` topic the ``Worker`` auto-registers whenever a
hosted agent declares selectors). The selector type is calfkit's public
:class:`~calfkit.mcp.MCPToolbox` — an identity-only handle constructible
with just the server name, so on a distributed deploy the agent host needs
neither ``mcp.json`` nor the secrets inside it. This module owns the
calfcord side only: collapsing an agent's ``mcp/...`` frontmatter entries
into one ref per server.

Policy: refs are **non-strict** (the upstream default, never overridden
here). An agent whose MCP server is down (or not yet started) boots and
answers normally; the affected tools drop out of that turn with calfkit
logging the degradation. This matches the roster's "nothing runs that you
didn't start" property — declaring ``mcp/github`` in an agent's frontmatter
must not hold the agent hostage to the github server's uptime.
"""

from __future__ import annotations

from collections.abc import Iterable

from calfkit.mcp import MCPToolbox

from calfcord.mcp.selector import is_mcp_selector, parse_mcp_selector


def selectors_from_entries(entries: Iterable[str]) -> list[MCPToolbox]:
    """Collapse an agent's ``mcp/...`` frontmatter entries into per-server refs.

    Merge semantics match the old schema-build resolution: a bare
    ``mcp/<server>`` subsumes that server's explicit ``mcp/<server>/<tool>``
    entries; explicit-only selections dedupe into a sorted ``include``
    tuple; servers come back sorted so the agent's tool surface is
    deterministic regardless of frontmatter order.

    Args:
        entries: ``mcp/...`` selector strings only — the factory partitions
            builtin names out first. A non-MCP entry here is a programming
            error and raises.

    Raises:
        ValueError: For a non-MCP or malformed entry (message names the
            entry verbatim, via :func:`parse_mcp_selector`).
    """
    wildcard: set[str] = set()
    explicit: dict[str, set[str]] = {}
    for entry in entries:
        if not is_mcp_selector(entry):
            raise ValueError(
                f"expected an mcp/... selector, got {entry!r} (builtin names are resolved separately)"
            )
        server, tool = parse_mcp_selector(entry)
        if tool is None:
            wildcard.add(server)
        else:
            explicit.setdefault(server, set()).add(tool)
    return [
        # A bare mcp/<server> wildcard subsumes that server's explicit picks.
        MCPToolbox(
            server,
            include=None if server in wildcard else tuple(sorted(explicit[server])),
        )
        for server in sorted(wildcard | set(explicit))
    ]
