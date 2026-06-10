"""Tests for the ``calfkit-mcp`` per-server runner's guards.

One ``calfcord run mcp <server>`` process hosts exactly one
:class:`MCPToolbox` (per-server isolation: a broken server config or an
unreachable upstream must never take down sibling MCP servers). These tests
exercise the selection/config guards without standing up Kafka or any MCP
session — ``MCPToolbox`` construction is I/O-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from calfkit.mcp.mcp_toolbox import MCPToolbox
from calfkit.mcp.mcp_transport import StdioServerParameters

from calfcord.mcp.config import McpConfigError
from calfcord.mcp.runner import _amain, _parse_args, _select_toolbox

_CONFIG_PATH = Path("mcp.json")


def _toolbox(name: str) -> MCPToolbox:
    return MCPToolbox(name, connection_params=StdioServerParameters(command="x"))


class TestSelectToolbox:
    def test_selects_named_server(self) -> None:
        boxes = {"a": _toolbox("a"), "b": _toolbox("b")}
        assert _select_toolbox(boxes, "b", _CONFIG_PATH) is boxes["b"]

    def test_unknown_name_exits_listing_configured(self) -> None:
        boxes = {"a": _toolbox("a"), "b": _toolbox("b")}
        with pytest.raises(SystemExit) as excinfo:
            _select_toolbox(boxes, "nope", _CONFIG_PATH)
        message = str(excinfo.value)
        assert "nope" in message
        assert "a" in message and "b" in message

    def test_empty_config_exits_with_add_hint(self) -> None:
        """An empty (but valid) registry fails fast — before any broker
        connection — and points at ``calfcord mcp add``."""
        with pytest.raises(SystemExit, match="calfcord mcp add"):
            _select_toolbox({}, "anything", _CONFIG_PATH)


class TestAmainGuards:
    async def test_config_load_failure_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config-load failure becomes a clean SystemExit with an actionable
        message — never a raw traceback, and never a broker connection (the
        guard precedes Client.connect)."""

        def _raise(*_a: object, **_k: object) -> dict[str, MCPToolbox]:
            raise McpConfigError("boom")

        monkeypatch.setattr("calfcord.mcp.runner.load_mcp_servers", _raise)
        with pytest.raises(SystemExit) as excinfo:
            await _amain("demo")
        message = str(excinfo.value)
        assert "boom" in message


class TestParseArgs:
    def test_server_positional_required(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_server_positional_parsed(self) -> None:
        assert _parse_args(["github"]).server == "github"
