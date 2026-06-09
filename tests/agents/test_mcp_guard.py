"""Unit tests for the shared MCP-rejection guard leaf."""

from __future__ import annotations

import pytest

from calfcord.agents._mcp_guard import MCP_TOOL_PREFIX, is_mcp_tool, mcp_unsupported_error


def test_prefix_constant() -> None:
    """Both gates (definition + md_writer) key off this one constant."""
    assert MCP_TOOL_PREFIX == "mcp/"


@pytest.mark.parametrize("entry", ["mcp/gmail", "mcp/gmail/search", "mcp/"])
def test_is_mcp_tool_true(entry: str) -> None:
    assert is_mcp_tool(entry) is True


@pytest.mark.parametrize("entry", ["shell", "read_file", "calendar", "mcpserver", "mcp", ""])
def test_is_mcp_tool_false(entry: str) -> None:
    # ``mcp`` (no slash) and the empty string are NOT selectors — the guard is a
    # ``mcp/`` prefix check, not a substring/name match.
    assert is_mcp_tool(entry) is False


def test_error_is_valueerror_naming_the_entry() -> None:
    err = mcp_unsupported_error("mcp/gmail")
    assert isinstance(err, ValueError)
    msg = str(err)
    assert "MCP tools are not currently supported" in msg
    assert "0.7.0" in msg
    assert "mcp/gmail" in msg
