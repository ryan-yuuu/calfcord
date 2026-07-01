"""Unit tests for :mod:`calfcord.mcp.config_write` — mcp.json mutation.

The writer backs ``disco mcp add`` / ``mcp remove``. Pinned contracts:

* validate-before-write: a rejected entry leaves the file byte-identical;
* unrelated top-level keys and sibling servers survive a mutation verbatim;
* a brand-new file is created with the ``mcpServers`` wrapper at mode 0600;
* adding an existing name needs ``force``; removing a missing name is an
  error naming what IS configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from calfcord.mcp.config import McpConfigError
from calfcord.mcp.config_write import add_server, remove_server


def _seed(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


class TestAddServer:
    def test_adds_stdio_entry(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, {"mcpServers": {}})
        add_server(path, "github", {"command": "npx", "args": ["-y", "srv"]})
        data = json.loads(path.read_text())
        assert data["mcpServers"]["github"] == {"command": "npx", "args": ["-y", "srv"]}

    def test_creates_file_with_wrapper_at_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        add_server(path, "github", {"command": "npx"})
        data = json.loads(path.read_text())
        assert data == {"mcpServers": {"github": {"command": "npx"}}}
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_preserves_unrelated_keys_and_siblings(self, tmp_path: Path) -> None:
        path = _seed(
            tmp_path,
            {
                "$schema": "https://example/schema.json",
                "mcpServers": {"docs": {"type": "http", "url": "https://d"}},
            },
        )
        add_server(path, "github", {"command": "npx"})
        data = json.loads(path.read_text())
        assert data["$schema"] == "https://example/schema.json"
        assert data["mcpServers"]["docs"] == {"type": "http", "url": "https://d"}
        # Insertion order: existing first, new appended.
        assert list(data["mcpServers"]) == ["docs", "github"]

    def test_existing_name_refused_without_force(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, {"mcpServers": {"github": {"command": "old"}}})
        original = path.read_text()
        with pytest.raises(McpConfigError, match="github"):
            add_server(path, "github", {"command": "new"})
        assert path.read_text() == original

    def test_existing_name_overwritten_with_force(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, {"mcpServers": {"github": {"command": "old"}}})
        add_server(path, "github", {"command": "new"}, force=True)
        assert json.loads(path.read_text())["mcpServers"]["github"] == {"command": "new"}

    def test_invalid_entry_rejected_file_untouched(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, {"mcpServers": {}})
        original = path.read_text()
        with pytest.raises(McpConfigError, match="evn"):
            add_server(path, "github", {"command": "npx", "evn": {}})
        assert path.read_text() == original

    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, {"mcpServers": {}})
        with pytest.raises(McpConfigError, match="Bad-Name"):
            add_server(path, "Bad-Name", {"command": "npx"})

    def test_corrupt_file_rejected_not_clobbered(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        path.write_text("{not json")
        with pytest.raises(McpConfigError, match="JSON"):
            add_server(path, "github", {"command": "npx"})
        assert path.read_text() == "{not json"


class TestRemoveServer:
    def test_removes_entry_keeping_siblings(self, tmp_path: Path) -> None:
        path = _seed(
            tmp_path,
            {"mcpServers": {"github": {"command": "x"}, "docs": {"type": "http", "url": "https://d"}}},
        )
        remove_server(path, "github")
        assert json.loads(path.read_text())["mcpServers"] == {
            "docs": {"type": "http", "url": "https://d"}
        }

    def test_missing_name_errors_listing_configured(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, {"mcpServers": {"docs": {"type": "http", "url": "https://d"}}})
        with pytest.raises(McpConfigError, match=r"(?s)nope.*docs"):
            remove_server(path, "nope")

    def test_missing_file_errors(self, tmp_path: Path) -> None:
        with pytest.raises(McpConfigError):
            remove_server(tmp_path / "mcp.json", "github")


class TestReadRawShapes:
    def test_existing_file_without_wrapper_rejected(self, tmp_path: Path) -> None:
        """A non-empty document lacking ``mcpServers`` is the loader's error
        to report — the writer must not silently graft the wrapper onto it."""
        path = tmp_path / "mcp.json"
        path.write_text('{"something": "else"}')
        with pytest.raises(McpConfigError, match="mcpServers"):
            add_server(path, "github", {"command": "x"})
        assert path.read_text() == '{"something": "else"}'

    def test_mcpservers_non_object_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        path.write_text('{"mcpServers": []}')
        with pytest.raises(McpConfigError, match="object"):
            add_server(path, "github", {"command": "x"})
