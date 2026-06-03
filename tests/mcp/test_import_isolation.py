"""Guard the agent/bridge import boundary for the MCP package.

The credentialed :mod:`calfcord.mcp.servers` registry expands ``$VAR`` MCP
secrets at :class:`~calfkit.mcp.McpServer` construction time, so importing
it requires the bridge's secrets in the environment — a requirement that
belongs to the bridge, not the agent. The agent path must therefore reach
the MCP tool surface (selector parsing, the schema-only catalog, the
factory) WITHOUT ever importing ``servers``.

This must run in a *subprocess*: other tests in this suite import
``calfcord.mcp.servers`` in-process (e.g. via ``McpServer`` parity checks),
which would pollute ``sys.modules`` and make an in-process assertion
flaky/false. A fresh interpreter gives a clean ``sys.modules`` to assert
against.
"""

from __future__ import annotations

import subprocess
import sys

# The script runs in a fresh interpreter. It imports the agent-safe MCP
# modules and builds an AgentFactory (the agent deployment's entry point to
# the MCP catalog) with MagicMock dependencies, then asserts the
# bridge-only ``servers`` module was never pulled in transitively.
_ISOLATION_SCRIPT = """
import sys
from unittest.mock import MagicMock

# Agent-safe MCP imports — none may pull in calfcord.mcp.servers.
import calfcord.mcp.selector  # noqa: F401
import calfcord.mcp.catalog  # noqa: F401

from calfcord.agents.factory import AgentFactory

# Constructing the factory triggers the lazy MCP_CATALOG import (and the
# lazy TOOL_REGISTRY import); neither may reach the credentialed registry.
factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
assert factory is not None

assert "calfcord.mcp.servers" not in sys.modules, (
    "agent path imported the bridge-only calfcord.mcp.servers module: "
    + repr([m for m in sys.modules if m.startswith("calfcord.mcp")])
)
"""


def test_agent_path_does_not_import_mcp_servers() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"isolation subprocess failed (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
