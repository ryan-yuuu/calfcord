"""Tests for the ``calfkit-mcp`` bridge runner's node-resolution guard.

Only :func:`calfcord.mcp.runner._resolve_mcp_nodes` is unit-testable
without standing up Kafka — it is the empty-registry guard extracted from
``_amain`` precisely so it can be exercised in isolation. An empty registry
must fail fast (the worker would otherwise boot inert, subscribing to no
topics while appearing healthy).
"""

from __future__ import annotations

import pytest

from calfcord.mcp.runner import _resolve_mcp_nodes


def test_empty_registry_raises_system_exit() -> None:
    with pytest.raises(SystemExit, match="no MCP servers configured"):
        _resolve_mcp_nodes({})


def test_non_empty_registry_returns_values() -> None:
    """A non-empty ``{name: server}`` registry resolves to its values, in
    insertion order, suitable for passing to a calfkit ``Worker``. We use
    sentinel objects (not real ``McpServer`` instances) because the guard
    only cares about the dict being non-empty — it never inspects the
    values."""
    sentinel_a = object()
    sentinel_b = object()
    servers = {"a": sentinel_a, "b": sentinel_b}

    nodes = _resolve_mcp_nodes(servers)

    assert nodes == [sentinel_a, sentinel_b]
