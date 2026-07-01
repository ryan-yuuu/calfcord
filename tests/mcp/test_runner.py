"""Tests for the ``calfkit-mcp`` per-server runner's guards.

One ``disco run mcp <server>`` process hosts exactly one
:class:`MCPToolbox`. Selection/validation behaviors (unknown name, empty
registry, sibling-secret isolation) live on the loader and are pinned in
``test_config.py``'s ``TestLoadOneServer``; here we pin the runner's own
contract — config failures become a clean ``SystemExit`` before any broker
connection, and the CLI shape.
"""

from __future__ import annotations

import pytest

from calfcord.mcp.config import McpConfigError
from calfcord.mcp.runner import _amain, _parse_args


class TestAmainGuards:
    async def test_config_load_failure_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config-load failure becomes a clean SystemExit with an actionable
        message — never a raw traceback, and never a broker connection (the
        guard precedes Client.connect)."""

        def _raise(*_a: object, **_k: object):
            raise McpConfigError("boom")

        monkeypatch.setattr("calfcord.mcp.runner.load_one_server", _raise)
        with pytest.raises(SystemExit) as excinfo:
            await _amain("demo")
        message = str(excinfo.value)
        assert "boom" in message and "demo" in message


class TestParseArgs:
    def test_server_positional_required(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_server_positional_parsed(self) -> None:
        assert _parse_args(["github"]).server == "github"
