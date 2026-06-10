"""Unit tests for :mod:`calfcord.mcp.config` — the ``mcp.json`` loader.

The loader is the **server-path** half of the MCP boundary: it owns
transport config and credential expansion, and is only ever imported by the
``calfkit-mcp`` runner and the ``calfcord mcp`` CLI (never the agent path —
pinned by ``test_import_isolation.py``).

Pinned behaviors:

* the Cursor/Claude-Code ``{"mcpServers": {...}}`` schema, stdio + HTTP;
* ``$VAR`` / ``${VAR}`` expansion against the environment at load time
  (``$$`` escapes a literal dollar), failing loud on unset references;
* :func:`list_server_names` reads names WITHOUT expanding — compose
  generation and CLI rows must work with secrets unset;
* config-path resolution: ``$CALFCORD_MCP_CONFIG`` > ``$CALFCORD_HOME/config/mcp.json``
  > ``./mcp.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from calfkit.mcp.mcp_toolbox import MCPToolbox
from calfkit.mcp.mcp_transport import StdioServerParameters, StreamableHttpParameters

from calfcord.mcp.config import (
    McpConfigError,
    expand_vars,
    list_server_names,
    load_mcp_servers,
    resolve_config_path,
)


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return path


class TestExpandVars:
    def test_dollar_var_expanded(self) -> None:
        assert expand_vars("$TOKEN", {"TOKEN": "abc"}) == "abc"

    def test_braced_var_expanded(self) -> None:
        assert expand_vars("Bearer ${TOKEN}!", {"TOKEN": "abc"}) == "Bearer abc!"

    def test_double_dollar_escapes_literal(self) -> None:
        assert expand_vars("cost: $$5", {}) == "cost: $5"

    def test_unset_var_raises_naming_var(self) -> None:
        with pytest.raises(McpConfigError, match="TOKEN"):
            expand_vars("$TOKEN", {})

    def test_plain_string_passes_through(self) -> None:
        assert expand_vars("no refs here", {}) == "no refs here"

    def test_unbalanced_brace_raises(self) -> None:
        with pytest.raises(McpConfigError, match="unbalanced"):
            expand_vars("${TOKEN", {"TOKEN": "abc"})


class TestLoadStdio:
    def test_minimal_command_only(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"demo": {"command": "demo-server"}}})
        servers = load_mcp_servers(path)
        assert list(servers) == ["demo"]
        toolbox = servers["demo"]
        assert isinstance(toolbox, MCPToolbox)
        assert toolbox.node_id == "demo"
        params = toolbox._connection_params
        assert isinstance(params, StdioServerParameters)
        assert params.command == "demo-server"
        assert params.args == []

    def test_args_and_env_carried(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "s3cret")
        path = _write(
            tmp_path,
            {
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_TOKEN": "$GH_TOKEN"},
                    }
                }
            },
        )
        params = load_mcp_servers(path)["github"]._connection_params
        assert isinstance(params, StdioServerParameters)
        assert params.args == ["-y", "@modelcontextprotocol/server-github"]
        assert params.env is not None
        assert params.env["GITHUB_TOKEN"] == "s3cret"

    def test_env_passed_verbatim(self, tmp_path: Path) -> None:
        """The loader passes ``env`` through verbatim — the MCP SDK's
        ``stdio_client`` merges it over ``get_default_environment()`` at
        spawn time, so the child still gets ``PATH`` etc. without the
        loader duplicating that merge."""
        path = _write(
            tmp_path,
            {"mcpServers": {"demo": {"command": "x", "env": {"MY_VAR": "1"}}}},
        )
        params = load_mcp_servers(path)["demo"]._connection_params
        assert params.env == {"MY_VAR": "1"}

    def test_cwd_carried(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            {"mcpServers": {"demo": {"command": "x", "cwd": "/srv/demo"}}},
        )
        params = load_mcp_servers(path)["demo"]._connection_params
        assert str(params.cwd) == "/srv/demo"

    def test_explicit_stdio_type_accepted(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            {"mcpServers": {"demo": {"type": "stdio", "command": "x"}}},
        )
        params = load_mcp_servers(path)["demo"]._connection_params
        assert isinstance(params, StdioServerParameters)

    def test_command_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIN_DIR", "/opt/bin")
        path = _write(
            tmp_path,
            {"mcpServers": {"demo": {"command": "$BIN_DIR/server", "args": ["--token", "${BIN_DIR}"]}}},
        )
        params = load_mcp_servers(path)["demo"]._connection_params
        assert params.command == "/opt/bin/server"
        assert params.args == ["--token", "/opt/bin"]


class TestLoadHttp:
    def test_url_and_headers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCS_TOKEN", "tok")
        path = _write(
            tmp_path,
            {
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://docs.example.com/mcp",
                        "headers": {"Authorization": "Bearer $DOCS_TOKEN"},
                    }
                }
            },
        )
        params = load_mcp_servers(path)["docs"]._connection_params
        assert isinstance(params, StreamableHttpParameters)
        assert params.url == "https://docs.example.com/mcp"
        assert params.headers == {"Authorization": "Bearer tok"}

    def test_url_without_type_rejected(self, tmp_path: Path) -> None:
        """A ``url`` entry must say ``"type": "http"`` explicitly — matching
        the old calfcord schema and keeping stdio-vs-http unambiguous."""
        path = _write(tmp_path, {"mcpServers": {"docs": {"url": "https://x"}}})
        with pytest.raises(McpConfigError, match="docs"):
            load_mcp_servers(path)

    def test_sse_type_rejected_actionably(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, {"mcpServers": {"docs": {"type": "sse", "url": "https://x"}}}
        )
        with pytest.raises(McpConfigError, match="sse"):
            load_mcp_servers(path)


class TestLoadRejections:
    def test_missing_file_raises_naming_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        with pytest.raises(McpConfigError, match=r"nope\.json"):
            load_mcp_servers(missing)

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "{not json")
        with pytest.raises(McpConfigError, match="JSON"):
            load_mcp_servers(path)

    def test_missing_wrapper_key_raises_actionably(self, tmp_path: Path) -> None:
        """The bare-map shape (no ``mcpServers`` wrapper) is rejected with a
        pointer to the expected schema, not treated as a server map."""
        path = _write(tmp_path, {"demo": {"command": "x"}})
        with pytest.raises(McpConfigError, match="mcpServers"):
            load_mcp_servers(path)

    def test_empty_registry_is_ok(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {}})
        assert load_mcp_servers(path) == {}

    def test_invalid_server_name_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"Bad-Name": {"command": "x"}}})
        with pytest.raises(McpConfigError, match="Bad-Name"):
            load_mcp_servers(path)

    def test_unknown_entry_key_rejected_naming_key_and_server(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, {"mcpServers": {"demo": {"command": "x", "evn": {}}}}
        )
        with pytest.raises(McpConfigError, match=r"(?s)demo.*evn|evn.*demo"):
            load_mcp_servers(path)

    def test_command_and_url_together_rejected(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            {"mcpServers": {"demo": {"command": "x", "type": "http", "url": "https://x"}}},
        )
        with pytest.raises(McpConfigError, match="demo"):
            load_mcp_servers(path)

    def test_neither_command_nor_url_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"demo": {}}})
        with pytest.raises(McpConfigError, match="demo"):
            load_mcp_servers(path)

    def test_args_must_be_string_list(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, {"mcpServers": {"demo": {"command": "x", "args": "-y"}}}
        )
        with pytest.raises(McpConfigError, match="args"):
            load_mcp_servers(path)

    def test_env_values_must_be_strings(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, {"mcpServers": {"demo": {"command": "x", "env": {"N": 1}}}}
        )
        with pytest.raises(McpConfigError, match="env"):
            load_mcp_servers(path)

    def test_unset_var_raises_naming_var_and_server(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOPE_TOKEN", raising=False)
        path = _write(
            tmp_path,
            {"mcpServers": {"demo": {"command": "x", "env": {"T": "$NOPE_TOKEN"}}}},
        )
        with pytest.raises(McpConfigError, match=r"(?s)NOPE_TOKEN.*demo|demo.*NOPE_TOKEN"):
            load_mcp_servers(path)


class TestListServerNames:
    def test_names_in_declaration_order(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            {"mcpServers": {"zeta": {"command": "z"}, "alpha": {"command": "a"}}},
        )
        assert list_server_names(path) == ["zeta", "alpha"]

    def test_does_not_require_var_refs_to_be_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Compose generation and CLI rows enumerate servers on hosts where
        the secrets are NOT set — names must come back without expansion."""
        monkeypatch.delenv("NOPE_TOKEN", raising=False)
        path = _write(
            tmp_path,
            {"mcpServers": {"demo": {"command": "x", "env": {"T": "$NOPE_TOKEN"}}}},
        )
        assert list_server_names(path) == ["demo"]

    def test_still_validates_shape(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"Bad-Name": {"command": "x"}}})
        with pytest.raises(McpConfigError, match="Bad-Name"):
            list_server_names(path)

    def test_missing_file_yields_empty(self, tmp_path: Path) -> None:
        """No mcp.json simply means no MCP servers — enumeration callers
        (compose, CLI rows) treat absence as empty, not as an error."""
        assert list_server_names(tmp_path / "nope.json") == []


class TestResolveConfigPath:
    def test_env_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_MCP_CONFIG", str(tmp_path / "custom.json"))
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
        assert resolve_config_path() == tmp_path / "custom.json"

    def test_calfcord_home_config_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
        assert resolve_config_path() == tmp_path / "config" / "mcp.json"

    def test_cwd_fallback_for_dev_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
        monkeypatch.delenv("CALFCORD_HOME", raising=False)
        assert resolve_config_path() == Path("mcp.json")


class TestLoadShapeRejectionsExtra:
    """The remaining reachable shape-rejection branches (review round 1)."""

    def test_http_type_without_url_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"docs": {"type": "http"}}})
        with pytest.raises(McpConfigError, match="requires a 'url'"):
            load_mcp_servers(path)

    def test_headers_values_must_be_strings(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            {"mcpServers": {"docs": {"type": "http", "url": "https://d", "headers": {"N": 1}}}},
        )
        with pytest.raises(McpConfigError, match="headers"):
            load_mcp_servers(path)

    def test_empty_url_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"docs": {"type": "http", "url": ""}}})
        with pytest.raises(McpConfigError, match="url"):
            load_mcp_servers(path)

    def test_cwd_must_be_string(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"demo": {"command": "x", "cwd": 7}}})
        with pytest.raises(McpConfigError, match="cwd"):
            load_mcp_servers(path)

    def test_servers_map_not_object_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": ["demo"]})
        with pytest.raises(McpConfigError, match="object"):
            load_mcp_servers(path)

    def test_entry_not_object_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path, {"mcpServers": {"demo": "npx demo"}})
        with pytest.raises(McpConfigError, match="demo"):
            load_mcp_servers(path)


class TestExpandVarsEscapes:
    def test_double_dollar_before_brace_is_literal(self) -> None:
        """``$${`` is a literal ``${`` (the ``$$`` escape consumes the dollar),
        not an unbalanced reference — review round 1 regression pin."""
        assert expand_vars("$${", {}) == "${"

    def test_double_dollar_before_braced_var_ships_literal_ref(self) -> None:
        assert expand_vars("$${VAR}", {}) == "${VAR}"
