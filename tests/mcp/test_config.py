"""Tests for :mod:`calfcord.mcp.config` — the mcp.json server loader.

Exercise :func:`load_mcp_servers` (wrapping calfkit's ``McpServers.from_file``)
and :func:`resolve_config_path` without standing up Kafka. A fake catalog is
injected so these tests don't depend on which schemas happen to be committed
under ``mcp/schemas/``. ``McpServer`` construction is I/O-free (no subprocess,
no network) so the built servers are safe to assert on in-process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from calfkit.mcp import McpToolDef
from calfkit.mcp.exceptions import McpConfigError

from calfcord.mcp.config import load_mcp_servers, resolve_config_path

# Minimal one-tool catalog entry; the tool name is irrelevant to these tests.
_DEMO_TOOLS = [McpToolDef(name="t")]


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loads_stdio_server_and_sets_name(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "mcp.json",
        {"mcpServers": {"demo": {"command": "echo", "args": ["hi"]}}},
    )
    servers = load_mcp_servers(cfg, catalog={"demo": _DEMO_TOOLS})
    assert set(servers) == {"demo"}
    # calfkit sets name=<config key>; the runner relies on this for wire topics.
    assert servers["demo"].name == "demo"
    # the transport carries the command + args through verbatim
    assert servers["demo"].transport.command == "echo"
    assert servers["demo"].transport.args == ("hi",)


def test_accepts_bare_shape_without_wrapper(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "mcp.json", {"demo": {"command": "echo"}})
    servers = load_mcp_servers(cfg, catalog={"demo": _DEMO_TOOLS})
    assert set(servers) == {"demo"}


def test_loads_http_server_with_header_auth(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "mcp.json",
        {
            "mcpServers": {
                "web": {
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "Bearer tok"},
                }
            }
        },
    )
    servers = load_mcp_servers(cfg, catalog={"web": _DEMO_TOOLS})
    assert servers["web"].name == "web"
    assert servers["web"].transport.url == "https://example.test/mcp"
    assert servers["web"].transport.headers["Authorization"] == "Bearer tok"


def test_empty_config_yields_empty_registry(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "mcp.json", {"mcpServers": {}})
    servers = load_mcp_servers(cfg, catalog={})
    assert len(servers) == 0


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(McpConfigError):
        load_mcp_servers(tmp_path / "absent.json", catalog={})


def test_malformed_json_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{ this is not valid json", encoding="utf-8")
    with pytest.raises(McpConfigError):
        load_mcp_servers(cfg, catalog={})


def test_server_without_committed_schema_raises(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "mcp.json", {"mcpServers": {"ghost": {"command": "echo"}}})
    with pytest.raises(McpConfigError):
        load_mcp_servers(cfg, catalog={})


def test_unset_env_var_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALFCORD_TEST_MCP_TOKEN", raising=False)
    cfg = _write(
        tmp_path / "mcp.json",
        {"mcpServers": {"demo": {"command": "echo", "args": ["$CALFCORD_TEST_MCP_TOKEN"]}}},
    )
    with pytest.raises(McpConfigError):
        load_mcp_servers(cfg, catalog={"demo": _DEMO_TOOLS})


def test_set_env_var_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Expansion happens at parse; with the var set, the load succeeds (the
    # complement of test_unset_env_var_raises shows the env is consulted).
    monkeypatch.setenv("CALFCORD_TEST_MCP_TOKEN", "s3cret")
    cfg = _write(
        tmp_path / "mcp.json",
        {"mcpServers": {"demo": {"command": "echo", "args": ["$CALFCORD_TEST_MCP_TOKEN"]}}},
    )
    servers = load_mcp_servers(cfg, catalog={"demo": _DEMO_TOOLS})
    assert set(servers) == {"demo"}
    # the $VAR resolved to its env value in the built transport
    assert "s3cret" in servers["demo"].transport.args


def test_resolve_config_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    assert resolve_config_path() == Path("mcp.json")


def test_resolve_config_path_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALFCORD_MCP_CONFIG", "/etc/calfcord/mcp.json")
    assert resolve_config_path() == Path("/etc/calfcord/mcp.json")
