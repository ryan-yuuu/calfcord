"""Agent-path MCP tool selection — schema-free, ``mcp.json``-free.

calfkit resolves an agent's MCP tools per turn by calling each declared
``ToolSelector`` against the capability view (the ``KafkaTable`` projection
of the ``mcp.capabilities`` topic the ``Worker`` auto-registers whenever a
hosted agent declares selectors). calfkit's own selector type is produced
by ``MCPToolbox.select()`` — which requires constructing the toolbox, i.e.
having the server's connection params in hand. On a distributed deploy the
agent host has neither ``mcp.json`` nor the secrets inside it, so calfcord
implements the protocol directly: :class:`McpToolSelector` is just the
``(server, include)`` lookup key, resolved through calfkit's public
:func:`~calfkit.models.capability.resolve_capability`.

Policy: selectors are **non-strict**. An agent whose MCP server is down
(or not yet started) boots and answers normally; the affected tools drop
out of that turn with calfkit logging the degradation. This matches the
roster's "nothing runs that you didn't start" property — declaring
``mcp/github`` in an agent's frontmatter must not hold the agent hostage
to the github server's uptime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from calfkit.models.capability import resolve_capability
from calfkit.models.tool_dispatch import SelectorResult

from calfcord.mcp.selector import is_mcp_selector, parse_mcp_selector


@dataclass(frozen=True)
class McpToolSelector:
    """One server's deferred tool selection (calfkit ``ToolSelector``).

    ``include is None`` is the ``mcp/<server>`` wildcard — every tool the
    server currently advertises. A tuple pins the agent's surface to
    exactly those names (a server suddenly advertising new tools cannot
    enlarge it), mirroring ``MCPToolbox.select(include=...)``.
    """

    server: str
    include: tuple[str, ...] | None = None

    def resolve_tools(self, view: Mapping[str, Any]) -> SelectorResult:
        return resolve_capability(view, self.server, include=self.include)


def selectors_from_entries(entries: Iterable[str]) -> list[McpToolSelector]:
    """Collapse an agent's ``mcp/...`` frontmatter entries into per-server selectors.

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
    selected: dict[str, set[str] | None] = {}
    for entry in entries:
        if not is_mcp_selector(entry):
            raise ValueError(
                f"expected an mcp/... selector, got {entry!r} (builtin names are resolved separately)"
            )
        server, tool = parse_mcp_selector(entry)
        if tool is None:
            selected[server] = None  # wildcard subsumes any explicit picks
        elif server not in selected:
            selected[server] = {tool}
        elif selected[server] is not None:
            selected[server].add(tool)  # type: ignore[union-attr]
    return [
        McpToolSelector(server, include=None if tools is None else tuple(sorted(tools)))
        for server, tools in sorted(selected.items())
    ]
