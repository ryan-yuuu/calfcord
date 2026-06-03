"""Resolve ``mcp/...`` selectors into schema-only calfkit tool nodes.

This is the agent-side workhorse. Given the ``mcp/...`` selectors an agent
declared in its frontmatter and the transport-free
:data:`~calfcord.mcp.catalog.MCP_CATALOG`, it produces the
:class:`~calfkit.models.node_schema.BaseToolNodeSchema` objects the agent
needs to (a) advertise the MCP tools to its LLM under flattened
``<server>_<tool>`` names and (b) publish ``Call`` messages onto the
correct ``mcp.<server>.<tool>.input`` topics.

Why "schema-only"
-----------------

The agent deployment must never construct a *credentialed*
:class:`~calfkit.mcp.McpServer`: calfkit expands ``$VAR`` references at
``McpServer`` construction time, so importing a real (credentialed) server
would require the bridge's MCP secrets to be present in the agent's env
just to build the tool surface. Instead we build a server with a
**placeholder command that is never executed** and only *iterate* it for
schemas (``McpServer.__iter__`` is pure/synchronous, reading the inline
tool list). See :func:`schema_only_server`.

The rename / topic split
------------------------

For a server ``gmail`` with selected tool ``search`` we:

* :meth:`~calfkit.mcp.McpServer.only` ``("search")`` — filter by the
  *original* tool name, then
* :meth:`~calfkit.mcp.McpServer.rename` ``({"search": "gmail_search"})`` —
  map the original name to the LLM-facing flattened name.

calfkit applies the rename to the LLM-facing ``tool_schema.name`` while
keeping the *original* tool name in the wire topics, so the resulting node
advertises ``gmail_search`` to the model but subscribes/publishes on
``mcp.gmail.search.{input,output}``. We deliberately do **not** use
:meth:`~calfkit.mcp.McpServer.prefix`, which joins with a ``.`` — illegal
in an LLM tool name.

Determinism
-----------

Servers are processed in sorted order and tools within a server in sorted
order, so the emitted node list (and therefore the LLM's tool ordering and
the boot logs) is reproducible across runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from calfkit.mcp import McpServer, McpToolDef
from calfkit.models.node_schema import BaseToolNodeSchema

from calfcord.mcp.selector import parse_mcp_selector

_SCHEMA_ONLY_PLACEHOLDER_COMMAND = "mcp-schema-only-not-executed"
"""Placeholder ``command`` for a schema-only :class:`~calfkit.mcp.McpServer`.

Construction is I/O-free and the agent path only *iterates* the server for
schemas, so this string is never resolved to an executable, never spawned,
and carries no transport secrets. Named conspicuously so it is obvious in
any stack trace that an attempt to actually *run* it is a bug (the agent
deployment must route execution to the bridge, not run MCP locally)."""


def schema_only_server(name: str, tools: list[McpToolDef]) -> McpServer:
    """Build a transport-free :class:`~calfkit.mcp.McpServer` for schemas.

    The returned server is constructed with a placeholder command that is
    **never opened**: callers must only *iterate* it (directly or via
    :meth:`~calfkit.mcp.McpServer.only` /
    :meth:`~calfkit.mcp.McpServer.rename`) to obtain
    :class:`~calfkit.models.node_schema.BaseToolNodeSchema` objects.
    Iteration reads the inline ``tools`` list synchronously and spawns no
    subprocess, opens no socket, and expands no ``$VAR`` — so this is safe
    to call in the agent deployment, which has none of the bridge's MCP
    credentials.

    Passing ``name=`` explicitly bypasses calfkit's name inference (which
    would otherwise derive a name from the command), so the wire topics use
    the catalog's server name rather than the placeholder string.

    Args:
        name: The MCP server name (a ``schemas/`` module name); becomes the
            ``<server>`` segment of every ``mcp.<server>.<tool>`` topic.
        tools: The server's :class:`~calfkit.mcp.McpToolDef` schemas.

    Returns:
        An un-connected :class:`~calfkit.mcp.McpServer` suitable for
        schema iteration only.
    """
    return McpServer.stdio(_SCHEMA_ONLY_PLACEHOLDER_COMMAND, name=name, tools=tools)


def _group_selected_tools(
    selectors: Iterable[str],
    catalog: Mapping[str, list[McpToolDef]],
) -> dict[str, list[str]]:
    """Group selectors into ``{server: [original_tool_name, ...]}``, validated.

    Shared core of :func:`validate_mcp_references` and
    :func:`resolve_mcp_selectors` so the two cannot drift in what they
    consider valid. A bare ``mcp/<server>`` selector expands to *all* of
    that server's tools (sorted). An ``mcp/<server>/<tool>`` selector adds
    exactly that tool. Duplicate selections (e.g. a bare server plus an
    explicit tool of it, or the same explicit tool twice) are collapsed.

    The returned mapping is deterministic: servers are not ordered here
    (the dict preserves first-seen order), but each server's tool list is
    de-duplicated and sorted, and callers that need server ordering sort
    the keys themselves.

    Args:
        selectors: Raw ``mcp/...`` selector strings.
        catalog: The ``server -> [McpToolDef]`` catalog to validate
            against.

    Raises:
        ValueError: On a malformed selector (propagated from
            :func:`~calfcord.mcp.selector.parse_mcp_selector`), an unknown
            server, or an unknown tool of a known server. Unknown-server
            and unknown-tool messages list the valid alternatives so the
            operator can fix a typo without grepping the catalog.
    """
    # server -> set of original tool names (set dedupes bare+explicit overlap)
    selected: dict[str, set[str]] = {}
    for entry in selectors:
        sel = parse_mcp_selector(entry)
        if sel.server not in catalog:
            raise ValueError(
                f"MCP selector {entry!r} references unknown server "
                f"{sel.server!r}; known servers: {sorted(catalog)}"
            )
        available = {t.name for t in catalog[sel.server]}
        bucket = selected.setdefault(sel.server, set())
        if sel.selects_all_tools:
            # Bare server: select every tool the server publishes.
            bucket.update(available)
        else:
            if sel.tool not in available:
                raise ValueError(
                    f"MCP selector {entry!r} references unknown tool "
                    f"{sel.tool!r} on server {sel.server!r}; available tools: "
                    f"{sorted(available)}"
                )
            bucket.add(sel.tool)
    return {server: sorted(names) for server, names in selected.items()}


def validate_mcp_references(
    selectors: Iterable[str],
    catalog: Mapping[str, list[McpToolDef]],
) -> None:
    """Raise if any selector references an unknown server or tool.

    A validation-only pass (no node construction), intended as the
    bridge's boot-time check that every selector declared across all
    agents can actually be served by the catalog the bridge hosts. Shares
    the exact validation logic of :func:`resolve_mcp_selectors` via
    :func:`_group_selected_tools`, so "passes validation here" and
    "resolves there" cannot diverge.

    Args:
        selectors: Raw ``mcp/...`` selector strings.
        catalog: The ``server -> [McpToolDef]`` catalog to validate
            against.

    Raises:
        ValueError: On a malformed selector, unknown server, or unknown
            tool (see :func:`_group_selected_tools`).
    """
    _group_selected_tools(selectors, catalog)


def resolve_mcp_selectors(
    selectors: Iterable[str],
    catalog: Mapping[str, list[McpToolDef]],
) -> list[BaseToolNodeSchema]:
    """Resolve selectors into schema-only calfkit tool nodes.

    For each referenced server, builds a :func:`schema_only_server`,
    filters it to the selected original tool names with
    :meth:`~calfkit.mcp.McpServer.only`, renames each to its LLM-facing
    ``<server>_<tool>`` name with :meth:`~calfkit.mcp.McpServer.rename`,
    and iterates it to collect the resulting
    :class:`~calfkit.models.node_schema.BaseToolNodeSchema` nodes. A bare
    ``mcp/<server>`` selector resolves to all of that server's tools.

    The emitted node list is deterministic: servers are processed in
    sorted order and tools within each server in sorted order.

    Args:
        selectors: Raw ``mcp/...`` selector strings (e.g. from an agent's
            ``tools:`` frontmatter list).
        catalog: The ``server -> [McpToolDef]`` catalog to resolve
            against (typically :data:`calfcord.mcp.catalog.MCP_CATALOG`).

    Returns:
        Schema-only tool nodes, one per selected tool. Each advertises
        ``<server>_<tool>`` to the LLM and routes on
        ``mcp.<server>.<tool>.{input,output}``.

    Raises:
        ValueError: On a malformed selector, unknown server, or unknown
            tool (same contract as :func:`validate_mcp_references`).
    """
    grouped = _group_selected_tools(selectors, catalog)

    nodes: list[BaseToolNodeSchema] = []
    # Sort servers for deterministic node ordering (tool names within a
    # server are already sorted by ``_group_selected_tools``).
    for server in sorted(grouped):
        selected = grouped[server]
        server_obj = (
            schema_only_server(server, catalog[server])
            .only(*selected)
            .rename({tool: f"{server}_{tool}" for tool in selected})
        )
        nodes.extend(list(server_obj))
    return nodes
