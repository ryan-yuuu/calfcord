"""Guard the agent/bridge import boundary for the MCP package.

The credentialed MCP surface now lives in ``mcp.json`` + the
:mod:`calfcord.mcp.config` loader (called only by the ``calfkit-mcp`` bridge
runner). Loading it expands ``$VAR`` MCP secrets from the environment, so it
belongs to the bridge, not the agent. The agent path must reach the MCP tool
surface (selector parsing, the schema-only catalog, the factory) WITHOUT ever
importing the bridge-only loader or runner — guaranteeing an agent never needs
an MCP credential to boot.

This must run in a *subprocess*: other tests in this suite import
:mod:`calfcord.mcp.config` / :mod:`calfcord.mcp.runner` in-process, which would
pollute ``sys.modules`` and make an in-process assertion flaky/false. A fresh
interpreter gives a clean ``sys.modules`` to assert against.
"""

from __future__ import annotations

import subprocess
import sys

# The script runs in a fresh interpreter. It imports the agent-safe MCP modules
# and builds an AgentFactory (the agent deployment's entry point to the MCP
# catalog) with MagicMock dependencies, then asserts the bridge-only loader and
# runner were never pulled in transitively.
_ISOLATION_SCRIPT = """
import sys
from unittest.mock import MagicMock

# Agent-safe MCP imports — none may pull in the bridge-only config/runner.
import calfcord.mcp.selector  # noqa: F401
import calfcord.mcp.catalog  # noqa: F401

from calfcord.agents.factory import AgentFactory

# Constructing the factory triggers the lazy MCP_CATALOG import (and the lazy
# TOOL_REGISTRY import); neither may reach the credentialed loader/runner.
factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
assert factory is not None

leaked = [m for m in ("calfcord.mcp.config", "calfcord.mcp.runner") if m in sys.modules]
assert not leaked, (
    "agent path imported bridge-only MCP module(s): "
    + repr(leaked)
    + " (all calfcord.mcp.*: "
    + repr([m for m in sys.modules if m.startswith("calfcord.mcp")])
    + ")"
)

# Sentinel printed only after every assertion above passed, so the test can
# distinguish "ran to completion" from a vacuous exit-0 (e.g. if the script
# were ever truncated or short-circuited before the assertions ran).
print("ISOLATION_OK")
"""


def test_agent_path_does_not_import_mcp_bridge() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"isolation subprocess failed (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Guard against a vacuous pass: the script must have run to completion
    # (all assertions executed) and emitted the sentinel, not merely exited 0.
    assert "ISOLATION_OK" in result.stdout, (
        f"isolation subprocess exited 0 but did not run to completion "
        f"(no ISOLATION_OK sentinel)\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
