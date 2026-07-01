"""Guard the agent/MCP-server deployment boundary at import time.

The agent path (definition parsing, the factory, the agent runner) resolves
``mcp/...`` selectors from the broker's capability view and must run on
hosts that have no ``mcp.json`` and none of the secrets inside it. The only
modules allowed to read that file are the ``calfkit-mcp`` runner and the
``calfcord mcp`` CLI. A stray import of :mod:`calfcord.mcp.config` from the
agent path would silently re-couple agent hosts to the config file (and its
``$VAR`` environment), so this test imports the agent path in a clean
interpreter and asserts the loader never came along.

Subprocess (not in-process) on purpose: the test session itself imports
``calfcord.mcp.config`` for its own tests, so ``sys.modules`` here is
already polluted.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys

import calfcord.agents.definition
import calfcord.agents.factory
import calfcord.agents.runner
import calfcord.mcp.agent_select

forbidden = [m for m in sys.modules if m == "calfcord.mcp.config"]
sys.exit(f"agent path imported {forbidden}" if forbidden else 0)
"""


def test_agent_path_does_not_import_mcp_config() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
