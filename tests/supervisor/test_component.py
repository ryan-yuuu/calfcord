"""Unit tests for the GENERIC component lifecycle (design §2 / §12.0).

``component_start`` / ``component_stop`` are the DRY base every named SINGLETON
roster process (``router`` / ``tools`` / ``mcp``) clocks in/out through — the same
workspace-check-then-REST shape as the agent roster ops, but deliberately WITHOUT
the agent-only broker-wide duplicate guard (a component is a single declared
slot, so a same-host duplicate is impossible and a broker probe would be dead
work). These exercise both functions with **no real process-compose binary and no
broker**: the REST client is injected.

The contracts pinned here:

* **Workspace check first.** With the supervisor unreachable there is nothing to
  start/stop — print the not-running hint and return ``1`` before any lifecycle
  call (no doomed REST round-trip).
* **No duplicate guard.** Unlike ``agent_start``, ``component_start`` never takes
  or queries a probe — a singleton component cannot duplicate on one host.
* **Per-home default client.** With no client injected, the default targets the
  port :func:`lifecycle.pc_port_for` derives from ``$CALFCORD_HOME`` (the port
  ``up -p`` pinned), so a second install on one host does not collide.
"""

from __future__ import annotations

from calfcord.supervisor import component


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient.

    ``workspace_up`` drives the ``project_state`` workspace check (False → the
    real client's RuntimeError-on-transport-failure, i.e. supervisor unreachable).
    Every lifecycle call records its name so a test can assert it was (or was NOT)
    issued.
    """

    def __init__(self, *, workspace_up: bool = True) -> None:
        self._workspace_up = workspace_up
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []

    async def project_state(self):
        if not self._workspace_up:
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}

    async def start_process(self, name: str):
        self.start_calls.append(name)
        return {}

    async def stop_process(self, name: str):
        self.stop_calls.append(name)
        return {}


def _home(tmp_path) -> str:
    return str(tmp_path)


# --- component_start --------------------------------------------------------


async def test_component_start_happy_path(tmp_path, capsys):
    """Workspace up → POST start the named slot, report online, exit 0."""
    client = _StubClient()

    rc = await component.component_start(_home(tmp_path), name="tools", client=client)

    assert rc == 0
    assert client.start_calls == ["tools"]
    out = capsys.readouterr().out
    assert "tools" in out
    assert "online" in out


async def test_component_start_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no start issued."""
    client = _StubClient(workspace_up=False)

    rc = await component.component_start(_home(tmp_path), name="tools", client=client)

    assert rc == 1
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
    assert "calfcord start" in out


# --- component_stop ---------------------------------------------------------


async def test_component_stop_happy_path(tmp_path, capsys):
    """Workspace up → PATCH stop, report stopped, exit 0."""
    client = _StubClient()

    rc = await component.component_stop(_home(tmp_path), name="tools", client=client)

    assert rc == 0
    assert client.stop_calls == ["tools"]
    out = capsys.readouterr().out
    assert "tools" in out
    assert "stopped" in out


async def test_component_stop_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no stop issued."""
    client = _StubClient(workspace_up=False)

    rc = await component.component_stop(_home(tmp_path), name="tools", client=client)

    assert rc == 1
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- default wiring (production seams, no real broker) -----------------------


def test_component_defaults_to_per_home_process_compose_client(tmp_path):
    """With no ``client`` injected, the per-home ``ProcessComposeClient`` is built.

    The default REST client must target the port :func:`lifecycle.pc_port_for`
    derives from ``$CALFCORD_HOME`` — the same port ``up -p`` pins — so a second
    install on one host does not collide. Asserting the resolver wiring (not a
    live call) keeps this unit-pure.
    """
    from calfcord.supervisor.client import ProcessComposeClient
    from calfcord.supervisor.lifecycle import pc_port_for

    home = _home(tmp_path)
    client = component._resolve_client(None, home)

    assert isinstance(client, ProcessComposeClient)
    expected = ProcessComposeClient(port=pc_port_for(home))
    assert client._base_url == expected._base_url


# --- import-lightness (decoupling invariant) --------------------------------

# Must run in a *subprocess* (the ``test_import_isolation.py`` pattern): other
# tests in the full suite import ``calfcord.mcp.config`` in-process, which would
# pollute ``sys.modules`` and make an in-process assertion vacuously false.
_COMPONENT_ISOLATION_SCRIPT = """
import sys

import calfcord.supervisor.component  # noqa: F401

leaked = [m for m in sys.modules if m == "calfcord.mcp.config"]
assert not leaked, (
    "component import pulled in the bridge-only MCP secrets loader: "
    + repr(leaked)
)
print("COMPONENT_ISOLATION_OK")
"""


def test_component_module_does_not_import_mcp_config():
    """component.py must stay off the bridge-only MCP-secrets path (CLAUDE.md, §12.3).

    It will be reused by the ``mcp start/stop`` veneer, which must remain
    importable on a host that holds no MCP credentials.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", _COMPONENT_ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"isolation subprocess failed (exit={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "COMPONENT_ISOLATION_OK" in result.stdout, (
        "isolation subprocess exited 0 but did not run to completion "
        f"(no sentinel)\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
