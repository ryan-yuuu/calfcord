"""Import-isolation guard for the control-plane modules the CLI/agent paths use.

``calfcord.mcp.config`` is the bridge-only MCP loader: it reads ``mcp.json`` and
expands ``$VAR`` transport secrets (design §12.3, and the project's hard
decoupling invariant). The control-plane probe / first-reply watcher are invoked
from host-agnostic, bridge-independent call sites — the ``calfcord`` CLI (doctor
deep-probe, ``agent ps``, the duplicate guard, the init wizard's live finish) and
the agent runner — none of which may transitively load that secrets loader.

So importing :mod:`calfcord.control_plane.probe`,
:mod:`calfcord.control_plane.first_reply`, and the shared
:mod:`calfcord.control_plane.builders` must NOT pull ``calfcord.mcp.config`` into
``sys.modules`` (schema-only ``calfcord.mcp.*`` siblings are fine — the invariant
bans the *config/secrets* loader, not the schemas). A fresh interpreter gives a
clean ``sys.modules`` to assert against; mirrors
``tests/health/test_check.py`` / ``tests/health/test_heartbeat.py``.
"""

from __future__ import annotations

import subprocess
import sys

_ISOLATION_SCRIPT = """
import sys

import calfcord.control_plane.probe  # noqa: F401
import calfcord.control_plane.first_reply  # noqa: F401
import calfcord.control_plane.builders  # noqa: F401

mcp_config_leaked = "calfcord.mcp.config" in sys.modules
assert not mcp_config_leaked, (
    "a control-plane module transitively imported the bridge-only MCP secrets "
    "loader calfcord.mcp.config (all calfcord.mcp.*: "
    + repr([m for m in sys.modules if m.startswith("calfcord.mcp")]) + ")"
)
print("ISOLATION_OK")
"""


def test_control_plane_modules_do_not_import_mcp_config() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"isolation subprocess failed (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ISOLATION_OK" in result.stdout, (
        "isolation subprocess exited 0 but did not run to completion\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
