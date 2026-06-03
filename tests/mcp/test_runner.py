"""Tests for the ``calfkit-mcp`` bridge runner's node-resolution guards.

Only :func:`calfcord.mcp.runner._resolve_mcp_nodes` is unit-testable
without standing up Kafka — it is the registry guard extracted from
``_amain`` precisely so it can be exercised in isolation. It enforces two
invariants:

* an empty registry must fail fast (the worker would otherwise boot inert,
  subscribing to no topics while appearing healthy), and
* every registry key must equal its server's (normalized) ``name=``, since
  the wire topics derive from the name while agents derive them from the
  selector ``<server>`` segment (= the key); a mismatch silently hangs
  every call to that server.

``McpServer`` construction is I/O-free (no subprocess, no ``$VAR``
expansion until ``open()``), so real servers are safe to build in-process
for these guards.
"""

from __future__ import annotations

import pytest
from calfkit.mcp import McpServer, McpToolDef

from calfcord.mcp.runner import _resolve_mcp_nodes


def test_empty_registry_raises_system_exit() -> None:
    with pytest.raises(SystemExit, match="no MCP servers configured"):
        _resolve_mcp_nodes({})


def test_non_empty_registry_returns_values() -> None:
    """A non-empty ``{name: server}`` registry whose keys match each
    server's ``name=`` resolves to its values, in insertion order, suitable
    for passing to a calfkit ``Worker``."""
    server_a = McpServer.stdio("npx", "-y", "a", tools=[McpToolDef(name="t")], name="a")
    server_b = McpServer.stdio("npx", "-y", "b", tools=[McpToolDef(name="t")], name="b")
    servers = {"a": server_a, "b": server_b}

    nodes = _resolve_mcp_nodes(servers)

    assert nodes == [server_a, server_b]


def test_key_name_mismatch_raises_system_exit() -> None:
    """A registry key that does not equal the server's (normalized) ``name=``
    is a silent-hang misconfig: the bridge subscribes to ``mcp.wrong.*``
    while agents publish to ``mcp.right.*``. The guard fails fast at boot,
    naming the offending ``key (name=...)`` pair."""
    server = McpServer.stdio("npx", "-y", "x", tools=[McpToolDef(name="t")], name="wrong")
    with pytest.raises(SystemExit, match="do not match the server's name"):
        _resolve_mcp_nodes({"right": server})
