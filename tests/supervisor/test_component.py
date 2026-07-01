"""Unit tests for the GENERIC component lifecycle (design §2 / §12.0).

``component_start`` / ``component_stop`` / ``component_restart`` are the DRY base
every named SINGLETON roster process (``router`` / ``tools``) clocks
in/out through — the same workspace-check-then-REST shape as the agent roster ops,
but deliberately WITHOUT the agent-only broker-wide duplicate guard (a component
is a single declared slot, so a same-host duplicate is impossible and a broker
probe would be dead work). These exercise the functions with **no real
process-compose binary and no broker**: the REST client is injected.

The contracts pinned here:

* **Workspace check first.** With the supervisor unreachable there is nothing to
  start/stop/restart — print the not-running hint and return ``1`` before any
  lifecycle call (no doomed REST round-trip).
* **Start of an already-running component is a restart** (behavior #2 — the useful
  idempotency): a ``start`` on a slot already ``Running`` locally re-applies an
  edited config by restarting in place, rather than a no-op ``POST start``.
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
    ``running`` is the set of process names Process Compose reports in the
    ``Running`` state — it backs ``list_processes`` so a test can place the
    component in (or out of) the local Running set that ``component_start`` reads
    to decide start-vs-restart. ``dormant`` maps each present-but-NOT-Running slot
    name to its PC status string (the real v1.110.0 values: ``"Completed"`` for an
    operator-stopped slot, ``"Disabled"`` for a never-started one — never
    ``"Stopped"``), so a test can exercise the ``status == Running`` check with a
    name PRESENT but not Running (distinct from the name-ABSENT path). Every
    lifecycle call records its name so a test can assert it was (or was NOT) issued.
    """

    def __init__(
        self,
        *,
        workspace_up: bool = True,
        running: set[str] | None = None,
        dormant: dict[str, str] | None = None,
    ) -> None:
        self._workspace_up = workspace_up
        self._running = running or set()
        self._dormant = dormant or {}
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []

    async def project_state(self):
        if not self._workspace_up:
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}

    async def list_processes(self):
        # Mirror Process Compose's ``GET /processes`` shape just enough for the
        # Running filter: a list of {name, status} dicts. Running names come from
        # ``running``; ``dormant`` adds slots that ARE present but report a
        # non-Running status, so ``_is_running_locally``'s ``status == Running``
        # check is exercised on a present name (not only the name-absent path).
        rows = [{"name": name, "status": "Running"} for name in sorted(self._running)]
        rows += [
            {"name": name, "status": status}
            for name, status in sorted(self._dormant.items())
        ]
        return rows

    async def start_process(self, name: str):
        self.start_calls.append(name)
        return {}

    async def stop_process(self, name: str):
        self.stop_calls.append(name)
        return {}

    async def restart_process(self, name: str):
        self.restart_calls.append(name)
        return {}


def _home(tmp_path) -> str:
    return str(tmp_path)


# --- component_start --------------------------------------------------------


async def test_component_start_when_not_running_starts(tmp_path, capsys):
    """Workspace up, slot NOT running → POST start, report online, exit 0.

    The slot is a declared-but-idle component (absent from the Running set), so a
    ``start`` is a genuine clock-in: ``POST /process/start`` and ``<name> online``,
    with no restart issued.
    """
    client = _StubClient()

    rc = await component.component_start(_home(tmp_path), name="tools", client=client)

    assert rc == 0
    assert client.start_calls == ["tools"]
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "tools" in out
    assert "online" in out


async def test_component_start_when_already_running_restarts(tmp_path, capsys):
    """Workspace up, slot already Running → restart in place (behavior #2).

    Re-running ``start`` on a component that is already up is the useful
    idempotency: it re-applies an edited config by ``POST /process/restart`` and
    reports ``<name> restarted`` — NOT a no-op ``POST start`` (which Process
    Compose would reject / ignore for a running slot).
    """
    client = _StubClient(running={"router"})

    rc = await component.component_start(_home(tmp_path), name="router", client=client)

    assert rc == 0
    assert client.restart_calls == ["router"]
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "router" in out
    assert "restarted" in out


async def test_component_start_when_present_but_not_running_starts(tmp_path, capsys):
    """Workspace up, slot PRESENT but NOT Running → POST start, online, exit 0.

    Distinct from the name-ABSENT case: here the declared slot IS in the process
    list but reports a non-Running PC status (``"Completed"`` — an operator-stopped
    singleton). ``_is_running_locally`` must read the STATUS (not mere presence),
    so a ``start`` is a genuine clock-in (``POST start`` + ``online``), NOT a
    restart. This pins the ``status == Running`` check against a ``return True``
    mutant that the name-absent test cannot catch.
    """
    client = _StubClient(dormant={"router": "Completed"})

    rc = await component.component_start(_home(tmp_path), name="router", client=client)

    assert rc == 0
    assert client.start_calls == ["router"]  # not Running → a real start
    assert client.restart_calls == []  # NOT mistaken for a running slot
    out = capsys.readouterr().out
    assert "router" in out
    assert "online" in out


async def test_component_start_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no start issued."""
    client = _StubClient(workspace_up=False)

    rc = await component.component_start(_home(tmp_path), name="tools", client=client)

    assert rc == 1
    assert client.start_calls == []
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
    assert "disco start" in out


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


# --- component_restart ------------------------------------------------------


async def test_component_restart_happy_path(tmp_path, capsys):
    """Workspace up → POST restart, report restarted, exit 0.

    Restart is how a config edit (``router set`` etc.) takes effect on a running
    singleton — the node bakes its config at construction. Unlike ``start`` it
    issues ``POST /process/restart`` unconditionally (the verb's whole job), so it
    never consults the Running set.
    """
    client = _StubClient(running={"router"})

    rc = await component.component_restart(
        _home(tmp_path), name="router", client=client
    )

    assert rc == 0
    assert client.restart_calls == ["router"]
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "router" in out
    assert "restarted" in out


async def test_component_restart_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no restart issued."""
    client = _StubClient(workspace_up=False)

    rc = await component.component_restart(
        _home(tmp_path), name="router", client=client
    )

    assert rc == 1
    assert client.restart_calls == []
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
