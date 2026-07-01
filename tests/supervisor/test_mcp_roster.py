"""Unit tests for the per-server MCP roster lifecycle (``disco mcp ...``).

Each ``mcp.json`` server is its own Process Compose slot ``mcp-<server>`` —
per-server isolation, so the verbs here are the agent-roster shape (a slot
may be NOT declared when the server was added after ``disco start``)
minus the agent-only pieces (no broker-wide duplicate guard: two hosts
hosting the same toolbox id is a legitimate competing-consumer setup, not
the agent split-brain).

Contracts pinned:

* workspace check first (not-running hint, exit 1, no doomed REST call);
* ``start`` of a Running slot is a restart in place (behavior #2) — this is
  also the "re-pick up an edited mcp.json entry" command;
* a 4xx on start/restart is the not-declared case (server added after
  ``disco start``) → the workspace-reload hint, exit 1 — while 5xx /
  transport faults propagate loudly;
* ``--all`` sweeps: start over the *configured* names (mcp.json), stop and
  restart over the *running* ``mcp-`` slots on this host;
* server names are validated against the selector grammar before any REST
  call (a bad name could never match a declared slot).
"""

from __future__ import annotations

import pytest

from calfcord.supervisor import mcp_roster
from calfcord.supervisor.client import ProcessComposeError


class _StubClient:
    """Scriptable ProcessComposeClient stand-in (same shape as test_component's).

    ``fail_start`` maps a process name to the ``ProcessComposeError`` its
    ``start_process`` should raise, so tests can exercise the 4xx
    not-declared branch and the 5xx propagation branch.
    """

    def __init__(
        self,
        *,
        workspace_up: bool = True,
        running: set[str] | None = None,
        dormant: dict[str, str] | None = None,
        fail_start: dict[str, ProcessComposeError] | None = None,
        fail_restart: dict[str, ProcessComposeError] | None = None,
    ) -> None:
        self._workspace_up = workspace_up
        self._running = running or set()
        self._dormant = dormant or {}
        self._fail_start = fail_start or {}
        self._fail_restart = fail_restart or {}
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []

    async def project_state(self):
        if not self._workspace_up:
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}

    async def list_processes(self):
        rows = [{"name": name, "status": "Running"} for name in sorted(self._running)]
        rows += [
            {"name": name, "status": status}
            for name, status in sorted(self._dormant.items())
        ]
        return rows

    async def start_process(self, name: str):
        if name in self._fail_start:
            raise self._fail_start[name]
        self.start_calls.append(name)
        return {}

    async def stop_process(self, name: str):
        self.stop_calls.append(name)
        return {}

    async def restart_process(self, name: str):
        if name in self._fail_restart:
            raise self._fail_restart[name]
        self.restart_calls.append(name)
        return {}


def _home(tmp_path) -> str:
    return str(tmp_path)


# --- mcp_start ---------------------------------------------------------------


async def test_start_when_not_running_starts_slot(tmp_path, capsys):
    client = _StubClient()
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=client)
    assert rc == 0
    assert client.start_calls == ["mcp-github"]
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "github" in out and "online" in out


async def test_start_when_running_restarts_in_place(tmp_path, capsys):
    """Behavior #2 — and the documented way to re-apply an edited mcp.json
    entry to a live server."""
    client = _StubClient(running={"mcp-github"})
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=client)
    assert rc == 0
    assert client.start_calls == []
    assert client.restart_calls == ["mcp-github"]
    assert "restarted" in capsys.readouterr().out


async def test_start_workspace_down_prints_hint(tmp_path, capsys):
    client = _StubClient(workspace_up=False)
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=client)
    assert rc == 1
    assert client.start_calls == []
    assert "disco start" in capsys.readouterr().out


async def test_start_not_declared_4xx_prints_reload_hint(tmp_path, capsys):
    """A server added to mcp.json after ``disco start`` has no declared
    slot; PC answers 4xx. Steer to the workspace reload, exit 1."""
    err = ProcessComposeError("no such process", status_code=404)
    client = _StubClient(fail_start={"mcp-github": err})
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="github", client=client)
    assert rc == 1
    out = capsys.readouterr().out
    assert "disco stop" in out and "disco start" in out


async def test_start_5xx_propagates_loudly(tmp_path):
    err = ProcessComposeError("boom", status_code=500)
    client = _StubClient(fail_start={"mcp-github": err})
    try:
        await mcp_roster.mcp_start(_home(tmp_path), server="github", client=client)
    except RuntimeError as exc:
        assert "github" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


async def test_start_invalid_server_name_refused_before_rest(tmp_path, capsys):
    client = _StubClient(workspace_up=False)  # would hint if reached
    rc = await mcp_roster.mcp_start(_home(tmp_path), server="Bad-Name", client=client)
    assert rc == 1
    assert "Bad-Name" in capsys.readouterr().out


# --- mcp_stop / mcp_restart ---------------------------------------------------


async def test_stop_stops_slot(tmp_path, capsys):
    client = _StubClient(running={"mcp-github"})
    rc = await mcp_roster.mcp_stop(_home(tmp_path), server="github", client=client)
    assert rc == 0
    assert client.stop_calls == ["mcp-github"]
    assert "stopped" in capsys.readouterr().out


async def test_restart_restarts_slot(tmp_path, capsys):
    client = _StubClient()
    rc = await mcp_roster.mcp_restart(_home(tmp_path), server="github", client=client)
    assert rc == 0
    assert client.restart_calls == ["mcp-github"]
    assert "restarted" in capsys.readouterr().out


async def test_restart_not_declared_4xx_prints_reload_hint(tmp_path, capsys):
    err = ProcessComposeError("no such process", status_code=404)
    client = _StubClient(fail_restart={"mcp-github": err})
    rc = await mcp_roster.mcp_restart(_home(tmp_path), server="github", client=client)
    assert rc == 1
    out = capsys.readouterr().out
    assert "disco stop" in out and "disco start" in out


# --- sweeps -------------------------------------------------------------------


async def test_start_all_starts_each_configured_server(tmp_path, capsys):
    client = _StubClient(running={"mcp-alpha"})
    rc = await mcp_roster.mcp_start_all(
        _home(tmp_path), servers=["alpha", "beta"], client=client
    )
    assert rc == 0
    # alpha was running -> restarted (edited-entry pickup); beta started.
    assert client.restart_calls == ["mcp-alpha"]
    assert client.start_calls == ["mcp-beta"]


async def test_start_all_with_no_servers_says_so(tmp_path, capsys):
    client = _StubClient()
    rc = await mcp_roster.mcp_start_all(_home(tmp_path), servers=[], client=client)
    assert rc == 0
    assert client.start_calls == []
    assert "disco mcp add" in capsys.readouterr().out


async def test_start_all_aggregates_failures(tmp_path, capsys):
    err = ProcessComposeError("no such process", status_code=404)
    client = _StubClient(fail_start={"mcp-beta": err})
    rc = await mcp_roster.mcp_start_all(
        _home(tmp_path), servers=["alpha", "beta"], client=client
    )
    assert rc == 1
    assert client.start_calls == ["mcp-alpha"]  # alpha still started


async def test_stop_all_stops_only_running_mcp_slots(tmp_path, capsys):
    client = _StubClient(
        running={"mcp-alpha", "mcp-beta", "assistant", "tools"},
        dormant={"mcp-gamma": "Completed"},
    )
    rc = await mcp_roster.mcp_stop_all(_home(tmp_path), client=client)
    assert rc == 0
    assert sorted(client.stop_calls) == ["mcp-alpha", "mcp-beta"]


async def test_stop_all_none_running_is_benign(tmp_path, capsys):
    client = _StubClient(running={"assistant"})
    rc = await mcp_roster.mcp_stop_all(_home(tmp_path), client=client)
    assert rc == 0
    assert client.stop_calls == []


async def test_restart_all_restarts_only_running_mcp_slots(tmp_path, capsys):
    client = _StubClient(running={"mcp-alpha", "router"})
    rc = await mcp_roster.mcp_restart_all(_home(tmp_path), client=client)
    assert rc == 0
    assert client.restart_calls == ["mcp-alpha"]


# ----------------------------------------------------- workspace-down uniformity



@pytest.mark.parametrize(
    "call",
    [
        lambda c, h: mcp_roster.mcp_stop(h, server="x", client=c),
        lambda c, h: mcp_roster.mcp_restart(h, server="x", client=c),
        lambda c, h: mcp_roster.mcp_start_all(h, servers=["x"], client=c),
        lambda c, h: mcp_roster.mcp_stop_all(h, client=c),
        lambda c, h: mcp_roster.mcp_restart_all(h, client=c),
    ],
    ids=["stop", "restart", "start_all", "stop_all", "restart_all"],
)
async def test_workspace_down_hints_and_exits_1(tmp_path, capsys, call):
    """Every verb (not just start) checks the workspace before any REST call
    and prints the one shared not-running hint."""
    client = _StubClient(workspace_up=False)
    rc = await call(client, _home(tmp_path))
    assert rc == 1
    assert "disco start" in capsys.readouterr().out
    assert client.start_calls == client.stop_calls == client.restart_calls == []


async def test_restart_5xx_propagates_loudly(tmp_path):
    """mcp_restart mirrors mcp_start's not-declared-vs-genuine-fault split:
    a 5xx is infra, never mistranslated into the reload hint."""
    err = ProcessComposeError("boom", status_code=500)
    client = _StubClient(fail_restart={"mcp-github": err})
    with pytest.raises(RuntimeError, match="github"):
        await mcp_roster.mcp_restart(_home(tmp_path), server="github", client=client)


async def test_running_servers_returns_bare_names(tmp_path):
    """The public read strips the slot prefix so callers (mcp list) never
    learn the mcp- convention."""
    client = _StubClient(running={"mcp-github", "mcp-docs", "assistant"})
    assert await mcp_roster.running_servers(client) == {"github", "docs"}


async def test_start_all_reads_process_list_once(tmp_path):
    """The sweep reads the supervisor's process list once for N servers (no
    N+1 REST chatter) — pinned by counting list_processes calls."""
    client = _StubClient(running={"mcp-alpha"})
    calls = {"n": 0}
    original = client.list_processes

    async def _counting():
        calls["n"] += 1
        return await original()

    client.list_processes = _counting
    rc = await mcp_roster.mcp_start_all(
        _home(tmp_path), servers=["alpha", "beta", "gamma"], client=client
    )
    assert rc == 0
    assert calls["n"] == 1

