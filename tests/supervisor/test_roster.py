"""Unit tests for the agent-roster operations (design §3.4, §3.5, §13.1).

These exercise ``agent_start`` / ``agent_stop`` / ``agent_restart`` / ``agent_ps``
with **no real process-compose binary, no broker, and no network**: the
broker-wide live-roster probe (a read of calfkit's native mesh) and the REST
client are both injected. A stub probe scripts the broker-wide live roster (agent
NAMES, the presence the mesh carries); a stub client scripts the supervisor REST surface
(``project_state`` for the workspace check, ``start_process`` /
``stop_process`` / ``restart_process`` for lifecycle, ``list_processes`` for the
physical half of the ps union).

The contracts pinned here are the distributed-correct duplicate guard (§3.5 —
refuse to start a name already live anywhere, CLI-side, without a bridge change),
the not-declared-agent reload path (§13.1 — a brand-new agent authored after
``disco start`` needs a workspace reload, never an ``update_project`` that
would bounce the substrate), and the three-way ps union (§3.4 — physical∩logical,
physical-only "not yet registered", logical-only "running on another host").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from calfkit.exceptions import MeshUnavailableError

from calfcord.supervisor import roster
from calfcord.supervisor.client import ProcessComposeError

# --- fakes ------------------------------------------------------------------


class _StubProbe:
    """A scriptable stand-in for the broker-wide live-roster probe.

    Records the ``server_urls`` it was called with so a test can assert the probe
    fired (duplicate guard / ps) or did NOT (workspace-down short-circuit). The
    calfkit 0.12 mesh carries agent NAMES (presence), not full definitions, so it
    returns a fixed list of live agent names.
    """

    def __init__(self, roster_result: list[str] | None = None) -> None:
        self._roster = list(roster_result or [])
        self.calls: list[str] = []

    async def __call__(self, server_urls: str):
        self.calls.append(server_urls)
        return list(self._roster)


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient.

    ``workspace_up`` drives the ``project_state`` workspace check (False → the
    real client's RuntimeError-on-transport-failure, i.e. supervisor unreachable).
    ``start_raises`` models the not-declared-process error the PC server returns
    for an agent authored after ``disco start``. Every lifecycle call records
    its name so tests can assert it was (or was NOT) issued. ``update_project`` is
    present only so a test can assert §13.1: the not-declared path NEVER calls it.
    """

    def __init__(
        self,
        *,
        workspace_up: bool = True,
        start_raises: Exception | None = None,
        list_processes_result: object = None,
        fail_start: dict[str, Exception] | None = None,
        fail_stop: dict[str, Exception] | None = None,
        fail_restart: dict[str, Exception] | None = None,
    ) -> None:
        self._workspace_up = workspace_up
        self._start_raises = start_raises
        self._list_processes_result = list_processes_result or []
        # Per-name failure scripts let the bulk best-effort tests fail ONE item and
        # assert the sweep continues; absent (the default), no per-name op fails, so
        # the single-op tests above are unaffected.
        self._fail_start = fail_start or {}
        self._fail_stop = fail_stop or {}
        self._fail_restart = fail_restart or {}
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []
        self.update_project_calls: list[str] = []

    async def project_state(self):
        if not self._workspace_up:
            # Mirrors ProcessComposeClient: a transport failure (supervisor not
            # up) surfaces as RuntimeError, which the workspace check reads as
            # "not running".
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}

    async def start_process(self, name: str):
        self.start_calls.append(name)
        if name in self._fail_start:
            raise self._fail_start[name]
        if self._start_raises is not None:
            raise self._start_raises
        return {}

    async def stop_process(self, name: str):
        self.stop_calls.append(name)
        if name in self._fail_stop:
            raise self._fail_stop[name]
        return {}

    async def restart_process(self, name: str):
        self.restart_calls.append(name)
        if name in self._fail_restart:
            raise self._fail_restart[name]
        return {}

    async def list_processes(self):
        return self._list_processes_result

    async def update_project(self, yaml_text: str):
        self.update_project_calls.append(yaml_text)
        return {}


_SERVERS = "localhost:9092"


def _home(tmp_path) -> str:
    return str(tmp_path)


# --- agent_start: duplicate guard (§3.5) ------------------------------------


async def test_agent_start_refuses_duplicate_when_probe_shows_live(tmp_path, capsys):
    """Name already live anywhere → no duplicate, clear message, exit 0 (§3.5).

    The guard is broker-wide (the probe), so a name running on ANY host is caught
    CLI-side without a bridge change. It is a benign no-op, not a failure: return
    0, and crucially do NOT call ``start_process`` (no second instance).
    """
    client = _StubClient()
    probe = _StubProbe(["assistant"])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == []  # the whole point: no duplicate started
    assert probe.calls == [_SERVERS]  # the guard actually queried the org
    out = capsys.readouterr().out
    assert "already running in the organization" in out
    assert "assistant" in out


# --- agent_start: already-running-here is a restart (behavior #2) ------------


async def test_agent_start_local_running_restarts_not_duplicate_refusal(tmp_path, capsys):
    """`start` on a name that is Running on THIS host → restart, not the refusal.

    Behavior #2: a re-`start` of a locally-running instance is the useful
    idempotency — reload it in place (the same effect as `restart`). This must take
    precedence over the org-wide duplicate guard: the guard refuses (and tells you
    it is running elsewhere), but a *local* running instance is ours to restart, so
    we never reach the probe and never print the refusal.
    """
    client = _StubClient(list_processes_result=[{"name": "assistant", "status": "Running"}])
    # The probe would also report it live; the local-running branch must win BEFORE
    # the guard, so the probe is never consulted.
    probe = _StubProbe(["assistant"])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.restart_calls == ["assistant"]  # restarted in place
    assert client.start_calls == []  # NOT a fresh start
    assert probe.calls == []  # local-running short-circuits before the org probe
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "restarted" in out
    assert "already running in the organization" not in out


async def test_agent_start_remote_running_keeps_duplicate_refusal(tmp_path, capsys):
    """`start` on a name live on ANOTHER host (not local) → keep the §3.5 refusal.

    Behavior #2 only restarts a *local* running instance; a name answering on a
    different host is the duplicate-guard case (starting a second would
    double-reply / split-brain), so refuse with the org-wide message and return 0.
    """
    client = _StubClient(
        list_processes_result=[]  # not running on THIS host
    )
    probe = _StubProbe(["assistant"])  # answering on another host

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.restart_calls == []  # not ours to restart
    assert client.start_calls == []  # no duplicate started
    assert probe.calls == [_SERVERS]  # the guard did query the org
    out = capsys.readouterr().out
    assert "already running in the organization" in out


# --- agent_start: happy path ------------------------------------------------


async def test_agent_start_happy_path_starts_when_roster_empty(tmp_path, capsys):
    """Not live anywhere → start the local disabled slot, report online, exit 0."""
    client = _StubClient()
    probe = _StubProbe([])  # nobody live → free to start

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == ["assistant"]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "online" in out


async def test_agent_start_ignores_other_live_agents(tmp_path):
    """A different agent being live must not block starting this one."""
    client = _StubClient()
    probe = _StubProbe(["scheduler"])  # someone else is live

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == ["assistant"]


# --- agent_start: workspace down --------------------------------------------


async def test_agent_start_workspace_down_short_circuits(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, probe/start NOT called.

    The workspace check is first: with no supervisor there is nothing to start,
    so we must not waste a broker probe or attempt a start that would only raise.
    """
    client = _StubClient(workspace_up=False)
    probe = _StubProbe([])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1
    assert probe.calls == []  # cheap-out: no broker probe when nothing can start
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
    assert "disco start" in out


class _RaisingProbe:
    """A probe that raises (the broker being unreachable) to test degradation."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, server_urls: str):
        self.calls.append(server_urls)
        raise RuntimeError("broker unreachable")


async def test_agent_ps_physical_excludes_all_non_agent_processes(tmp_path, capsys):
    """The physical half of `ps` lists only AGENTS — not the substrate
    (broker/bridge) and not the other non-agent processes (tools)."""
    client = _StubClient(
        list_processes_result=[
            {"name": "broker", "status": "Running"},
            {"name": "bridge", "status": "Running"},
            {"name": "tools", "status": "Running"},
            {"name": "assistant", "status": "Running"},
        ]
    )
    probe = _StubProbe([])  # nothing logical → the board shows only physical agents

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=client, probe=probe)

    assert rc == 0
    out = capsys.readouterr().out
    assert "assistant" in out
    for non_agent in ("broker", "bridge", "tools"):
        assert non_agent not in out


async def test_agent_ps_tolerates_probe_failure(tmp_path, capsys):
    """A broker hiccup must not crash read-only `ps`: degrade to physical-only."""
    client = _StubClient(list_processes_result=[{"name": "assistant", "status": "Running"}])
    probe = _RaisingProbe()

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=client, probe=probe)

    assert rc == 0
    assert probe.calls == [_SERVERS]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "broker unreachable" in out.lower()


async def test_agent_start_tolerates_probe_failure_and_proceeds(tmp_path, capsys):
    """If the org-wide duplicate probe fails (broker unreachable), warn and proceed
    with the local start rather than crashing."""
    client = _StubClient()
    probe = _RaisingProbe()

    rc = await roster.agent_start(
        _home(tmp_path),
        name="assistant",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == ["assistant"]
    out = capsys.readouterr().out.lower()
    assert "could not verify" in out or "broker unreachable" in out


# --- agent_start: not declared in the running project (§13.1) ----------------


async def test_agent_start_not_declared_asks_for_reload_no_update_project(tmp_path, capsys):
    """start_process raises (brand-new agent) → reload message, exit 1, no update.

    A new agent authored after ``disco start`` is not a declared slot, so the
    PC server errors on start with a 4xx. §13.1: bringing it online needs a
    workspace reload (stop && start) because an in-place ``update_project`` bounces
    the substrate — so we must NOT call ``update_project``. The reload hint is
    reserved for this STRUCTURAL 4xx not-declared case (Fix #9).
    """
    client = _StubClient(
        start_raises=ProcessComposeError(
            "start_process: ... HTTP 404: process newbie is not defined",
            status_code=404,
        )
    )
    probe = _StubProbe([])  # not live → guard passes → we reach start_process

    rc = await roster.agent_start(
        _home(tmp_path),
        name="newbie",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1
    assert client.start_calls == ["newbie"]  # we did attempt the start
    assert client.update_project_calls == []  # §13.1: never bounce the substrate
    out = capsys.readouterr().out
    assert "newbie" in out
    # The message must steer to a reload, not leave the operator stranded.
    assert "disco stop" in out
    assert "disco start" in out


async def test_agent_start_server_error_raises_loudly_not_reload_hint(tmp_path):
    """A 5xx on start is a genuine infra fault → raise loudly (Fix #9).

    §13.1's reload hint is reserved for the STRUCTURAL 4xx not-declared case. A 5xx
    (a wedged supervisor) is not "a brand-new agent"; mistranslating it into the
    benign reload hint would mask a real fault. Per the error convention it must
    raise with caller/target/correlation context, carrying the PC body.
    """
    pc_error = ProcessComposeError(
        "start_process: process-compose POST /process/start/assistant failed with HTTP 500: internal supervisor error",
        status_code=500,
    )
    client = _StubClient(start_raises=pc_error)
    probe = _StubProbe([])  # not live → guard passes → we reach start_process

    with pytest.raises(RuntimeError) as excinfo:
        await roster.agent_start(
            _home(tmp_path),
            name="assistant",
            server_urls=_SERVERS,
            client=client,
            probe=probe,
        )

    message = str(excinfo.value)
    # The target agent is named (correlation), and PC's body survives.
    assert "assistant" in message
    assert "500" in message
    assert client.start_calls == ["assistant"]  # we did attempt the start
    assert client.update_project_calls == []  # still never bounce the substrate


async def test_agent_start_unknown_error_raises_loudly(tmp_path):
    """A raise with no structural status is a genuine fault → raise loudly (Fix #9).

    A bare ``RuntimeError`` (status_code absent → ``None``) is NOT the 4xx
    not-declared case, so it must not be mistranslated into the benign reload hint;
    it propagates as the infra fault it is.
    """
    client = _StubClient(start_raises=RuntimeError("unexpected transport blowup"))
    probe = _StubProbe([])

    with pytest.raises(RuntimeError):
        await roster.agent_start(
            _home(tmp_path),
            name="assistant",
            server_urls=_SERVERS,
            client=client,
            probe=probe,
        )


# --- agent_stop -------------------------------------------------------------


async def test_agent_stop_happy_path(tmp_path, capsys):
    """Workspace up → PATCH stop, report stopped, exit 0."""
    client = _StubClient()

    rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=client)

    assert rc == 0
    assert client.stop_calls == ["assistant"]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "stopped" in out


async def test_agent_stop_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no stop issued."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_stop(_home(tmp_path), name="assistant", client=client)

    assert rc == 1
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
    assert "disco start" in out


# --- agent_restart ----------------------------------------------------------


async def test_agent_restart_happy_path(tmp_path, capsys):
    """Workspace up → POST restart (reload after an edited .md), exit 0."""
    client = _StubClient()

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=client)

    assert rc == 0
    assert client.restart_calls == ["assistant"]
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "restarted" in out


async def test_agent_restart_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 1, no restart issued."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_restart(_home(tmp_path), name="assistant", client=client)

    assert rc == 1
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_start_all: sweep every DEFINED agent (behavior #1) ---------------


async def test_agent_start_all_mixes_running_stopped_and_remote(tmp_path, capsys):
    """`start --all` runs the single-start logic per DEFINED id (decision B).

    The caller passes the defined ids; each runs through `agent_start`'s body, so a
    locally-running one restarts (behavior #2), a stopped one starts, and one only
    answering on another host hits the duplicate-refusal. All three are honored in
    one sweep; every id gets a per-item line; an all-success sweep returns 0.
    """
    client = _StubClient(list_processes_result=[{"name": "local_up", "status": "Running"}])
    probe = _StubProbe(["remote"])  # answering elsewhere, not here

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["local_up", "stopped", "remote"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.restart_calls == ["local_up"]  # running here → restarted
    assert client.start_calls == ["stopped"]  # not anywhere → started
    out = capsys.readouterr().out
    assert "restarted" in out  # local_up
    assert "online" in out  # stopped
    assert "already running in the organization" in out  # remote
    # The closing summary pins the count math + wording for an all-success sweep.
    assert "start --all: 3 agent(s) processed, 0 failed." in out


async def test_agent_start_all_never_starts_reserved_processes(tmp_path, capsys):
    """`start --all` must NEVER touch a reserved (substrate/singleton) process.

    The caller (main.py) passes the RAW ``.md`` stems from ``detect_agents``,
    unfiltered — and a creatable ``tools.md`` / ``router.md`` /
    ``broker.md`` / ``bridge.md`` is not rejected by the id pattern (only
    ``disco start``'s ``build_compose_project`` rejects them). So if a reserved
    name leaks into ``agent_ids`` the sweep must drop it rather than ``start`` /
    ``restart`` the live singleton — the "--all never touches another component
    type" invariant. Note the leak would be a mis-START, not a mis-restart: even
    with ``tools`` Running locally, the restart branch keys off
    ``_running_agent_names``, which already filters reserved names out of the
    locally-Running set — so an unfiltered sweep would fall through to
    ``start_process('tools')``. The assertions below pin BOTH paths regardless.
    """
    client = _StubClient(list_processes_result=[{"name": "tools", "status": "Running"}])
    probe = _StubProbe([])  # nobody live → a leaked agent id would reach start

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["assistant", "tools"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    # The reserved singleton is neither started nor restarted by `start --all`.
    assert "tools" not in client.start_calls
    assert "tools" not in client.restart_calls
    # The real agent is still swept normally.
    assert client.start_calls == ["assistant"]


async def test_agent_start_all_all_reserved_is_clean_no_op(tmp_path, capsys):
    """An ``agent_ids`` of only reserved names drops to the empty no-op, not a sweep.

    After the reserved filter the defined set is empty, so it must take the same
    clean ``no agents defined`` / exit-0 path as a genuinely empty set — never
    fall through to start a singleton.
    """
    client = _StubClient()
    probe = _StubProbe([])

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["tools", "broker", "bridge"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == []
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "no agents defined" in out


async def test_agent_start_single_op_refuses_reserved_name(tmp_path, capsys):
    """A single ``agent start tools`` is refused at the chokepoint: error + exit 1.

    Reserved names (substrate + the tools/router singletons) are owned by
    ``disco start`` and their own component verbs, never the agent roster. The
    single-op guard at the top of ``agent_start`` closes the ``agent start tools``
    exposure before any workspace check / probe / start runs.
    """
    client = _StubClient()
    probe = _StubProbe([])

    rc = await roster.agent_start(
        _home(tmp_path),
        name="tools",
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1
    assert client.start_calls == []
    assert client.restart_calls == []
    assert probe.calls == []  # refused before any world-touching work
    out = capsys.readouterr().out
    assert "error:" in out
    assert "tools" in out


async def test_agent_start_all_empty_defined_set(tmp_path, capsys):
    """No defined agents → an explicit "none" line and a clean 0."""
    client = _StubClient()
    probe = _StubProbe([])

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=[],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "no agents defined" in out


async def test_agent_start_all_continues_past_a_hard_failure_returns_1(tmp_path, capsys):
    """A hard failure on one id must not abort the sweep; return 1 if any failed.

    Best-effort: a 5xx (genuine infra fault) on one id raises inside the single
    start, but the sweep catches it, keeps going for the rest, and reports a
    non-zero summary so the operator knows something needs attention.
    """
    boom = ProcessComposeError("HTTP 500: wedged supervisor", status_code=500)
    client = _StubClient(fail_start={"bad": boom})
    probe = _StubProbe([])  # nobody live → each id reaches start_process

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["good", "bad", "later"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1  # at least one hard-failed
    # The sweep continued past the failure: every id was attempted, including the
    # one AFTER the failing one.
    assert client.start_calls == ["good", "bad", "later"]
    # The summary pins the count math (3 processed, exactly 1 failed) + wording.
    assert "start --all: 3 agent(s) processed, 1 failed." in capsys.readouterr().out


async def test_agent_start_all_non_raising_failure_returns_1_and_keeps_sweeping(tmp_path, capsys):
    """A NON-raising per-id failure (agent_start returns 1) still fails the sweep.

    ``agent_start_all`` has two failure paths: ``except Exception`` (a raised 5xx)
    and ``if rc != 0`` (a NON-raising failure — agent_start's 4xx not-declared case
    *returns* 1 without raising). This pins the second: a brand-new ``newbie``
    triggers agent_start's 4xx reload steer (return 1, no raise), and the sweep
    must (a) count it as a failure → return 1, (b) keep going to the later id, and
    (c) print the reload steer for it.
    """
    client = _StubClient(
        # empty list_processes → nothing Running locally → no restart branch; the
        # per-id starts reach start_process, where `newbie` raises a 4xx that
        # agent_start translates to a RETURN 1 (not a re-raise).
        list_processes_result=[],
        fail_start={"newbie": ProcessComposeError("HTTP 404", status_code=404)},
    )
    probe = _StubProbe([])  # nobody live → each id reaches start_process

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["newbie", "later"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1  # the non-raising rc!=0 still fails the sweep
    # The sweep continued past the non-raising failure to the later id.
    assert client.start_calls == ["newbie", "later"]
    out = capsys.readouterr().out
    # agent_start's 4xx steer fired for newbie (the reload guidance).
    assert "disco stop" in out
    assert "disco start" in out


async def test_agent_start_all_probes_org_once_and_threads_live(tmp_path, capsys):
    """`start --all` probes the broker-wide roster ONCE, not once per id (FIX 5).

    The §3.5 duplicate guard reads the same org-wide roster for every id, so the
    sweep resolves it up front and threads it into each per-id start; a per-id
    re-probe would be N round-trips for one operator action. With a `remote`
    agent answering elsewhere, the single probe still feeds the refusal for it
    while the local `stopped` starts — all off ONE probe call.
    """
    client = _StubClient(list_processes_result=[])
    probe = _StubProbe(["remote"])  # answering on another host

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["stopped", "remote"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    # Exactly ONE org probe for the whole sweep, not one per id.
    assert probe.calls == [_SERVERS]
    assert client.start_calls == ["stopped"]  # the free one started
    out = capsys.readouterr().out
    assert "already running in the organization" in out  # remote refused off the shared probe


async def test_agent_start_all_broker_down_warns_once_not_per_id(tmp_path, capsys):
    """A broker-down probe during `start --all` warns ONCE, not N times (FIX 5).

    Previously each per-id ``agent_start`` independently caught the probe failure
    and printed "could not verify org-wide duplicates ...; proceeding." — N
    warnings for one operator action, with the guard silently skipped fleet-wide
    and no aggregate signal. The sweep must probe once up front, emit a SINGLE
    aggregate warning, then proceed with an empty live roster (so the local starts
    still happen).
    """
    client = _StubClient(list_processes_result=[])
    probe = _RaisingProbe()  # broker unreachable

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["a", "b", "c"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 0
    # The up-front probe was attempted exactly once (not re-probed per id).
    assert probe.calls == [_SERVERS]
    # Each id still started despite the unverifiable guard.
    assert client.start_calls == ["a", "b", "c"]
    out = capsys.readouterr().out.lower()
    # Exactly ONE aggregate broker-down warning for the whole sweep.
    assert out.count("could not verify") == 1


async def test_agent_start_all_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → the shared not-running hint, exit 1, nothing started."""
    client = _StubClient(workspace_up=False)
    probe = _StubProbe([])

    rc = await roster.agent_start_all(
        _home(tmp_path),
        agent_ids=["assistant"],
        server_urls=_SERVERS,
        client=client,
        probe=probe,
    )

    assert rc == 1
    assert client.start_calls == []
    assert probe.calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_stop_all: every LOCAL Running agent (behavior #1) -----------------


async def test_agent_stop_all_targets_only_running_local_agents(tmp_path, capsys):
    """`stop --all` stops every Running local AGENT — never the substrate/singletons.

    The target set is the same physical filter `ps` uses: Running processes whose
    name is not a reserved (substrate/tools) process. A Stopped agent is
    not a target. An all-success sweep returns 0.
    """
    client = _StubClient(
        list_processes_result=[
            {"name": "broker", "status": "Running"},  # substrate — never
            {"name": "tools", "status": "Running"},  # singleton — never
            {"name": "assistant", "status": "Running"},  # agent → stop
            {"name": "scheduler", "status": "Running"},  # agent → stop
            # PC v1.110.0 reports an operator-stopped slot as "Completed" (never
            # "Stopped"); not Running → skip.
            {"name": "dormant", "status": "Completed"},
        ]
    )

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 0
    assert sorted(client.stop_calls) == ["assistant", "scheduler"]
    for never in ("broker", "tools", "dormant"):
        assert never not in client.stop_calls
    # The summary pins the count math (2 targets, 0 failed) + wording.
    assert "stop --all: 2 agent(s) processed, 0 failed." in capsys.readouterr().out


async def test_agent_stop_all_empty_running_set(tmp_path, capsys):
    """No agents running locally → an explicit "none" line and a clean 0."""
    client = _StubClient(list_processes_result=[{"name": "broker", "status": "Running"}])

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 0
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "no agents running locally" in out


async def test_agent_stop_all_continues_past_a_failure_returns_1(tmp_path, capsys):
    """One failing stop must not abort the sweep; return 1 if any failed."""
    client = _StubClient(
        list_processes_result=[
            {"name": "a", "status": "Running"},
            {"name": "b", "status": "Running"},
            {"name": "c", "status": "Running"},
        ],
        fail_stop={"b": RuntimeError("stop blew up")},
    )

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 1
    assert sorted(client.stop_calls) == ["a", "b", "c"]  # every one attempted
    # The summary pins the count math (3 targets, exactly 1 failed) + wording.
    assert "stop --all: 3 agent(s) processed, 1 failed." in capsys.readouterr().out


async def test_agent_stop_all_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → the shared not-running hint, exit 1, nothing stopped."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_stop_all(_home(tmp_path), client=client)

    assert rc == 1
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_restart_all: every LOCAL Running agent (behavior #1) --------------


async def test_agent_restart_all_targets_only_running_local_agents(tmp_path, capsys):
    """`restart --all` restarts every Running local AGENT (same target as stop)."""
    client = _StubClient(
        list_processes_result=[
            {"name": "bridge", "status": "Running"},  # substrate — never
            {"name": "assistant", "status": "Running"},  # agent → restart
            # PC v1.110.0 reports a never-started declared slot as "Disabled"
            # (never "Stopped"); not Running → skip.
            {"name": "dormant", "status": "Disabled"},
        ]
    )

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 0
    assert client.restart_calls == ["assistant"]
    assert "bridge" not in client.restart_calls
    assert "dormant" not in client.restart_calls
    # The summary pins the count math (1 target, 0 failed) + wording.
    assert "restart --all: 1 agent(s) processed, 0 failed." in capsys.readouterr().out


async def test_agent_restart_all_empty_running_set(tmp_path, capsys):
    """No agents running locally → an explicit "none" line and a clean 0."""
    client = _StubClient(list_processes_result=[])

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 0
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "no agents running locally" in out


async def test_agent_restart_all_continues_past_a_failure_returns_1(tmp_path, capsys):
    """One failing restart must not abort the sweep; return 1 if any failed."""
    client = _StubClient(
        list_processes_result=[
            {"name": "a", "status": "Running"},
            {"name": "b", "status": "Running"},
        ],
        fail_restart={"a": RuntimeError("restart blew up")},
    )

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 1
    assert sorted(client.restart_calls) == ["a", "b"]  # every one attempted
    # The summary pins the count math (2 targets, exactly 1 failed) + wording.
    assert "restart --all: 2 agent(s) processed, 1 failed." in capsys.readouterr().out


async def test_agent_restart_all_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → the shared not-running hint, exit 1, nothing restarted."""
    client = _StubClient(workspace_up=False)

    rc = await roster.agent_restart_all(_home(tmp_path), client=client)

    assert rc == 1
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- agent_ps: the three-way union (§3.4) -----------------------------------


def _pc_proc(name: str, status: str) -> dict:
    """A process row as ``list_processes`` returns it (name + status)."""
    return {"name": name, "status": status}


async def test_agent_ps_workspace_down(tmp_path, capsys):
    """Supervisor unreachable → not-running hint, exit 0, probe NOT called.

    ps with no workspace is an expected state (you haven't opened the office),
    not an error — so exit 0, and don't spend a broker probe.
    """
    client = _StubClient(workspace_up=False)
    probe = _StubProbe([])

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=client, probe=probe)

    assert rc == 0
    assert probe.calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


async def test_agent_ps_union_three_cases(tmp_path, capsys):
    """The union renders all three §3.4 states distinctly.

    * ``assistant`` — physical (Running here) AND logical (answering) → registered.
    * ``ghost`` — physical (Running here) but NOT logical → started, not yet
      registered (the wedged/just-starting case).
    * ``remote`` — logical (answering) but NOT physical here → running on another
      host (expected multi-host, NOT an error).

    Substrate processes (broker/bridge) are excluded from the roster board, and a
    non-Running physical process is not counted as physically up.
    """
    client = _StubClient(
        list_processes_result=[
            _pc_proc("broker", "Running"),  # substrate — excluded
            _pc_proc("bridge", "Running"),  # substrate — excluded
            _pc_proc("assistant", "Running"),  # physical + logical
            _pc_proc("ghost", "Running"),  # physical only
            # PC v1.110.0 reports a dormant slot as "Completed"/"Disabled", never
            # "Stopped"; not Running → not physically up.
            _pc_proc("dormant", "Completed"),
        ]
    )
    probe = _StubProbe(["assistant", "remote"])

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=client, probe=probe)

    assert rc == 0
    out = capsys.readouterr().out

    # assistant: both halves → registered/running.
    assert "assistant" in out
    # ghost: physical up, not answering → "not yet registered".
    assert "ghost" in out
    assert "not yet registered" in out
    # remote: answering elsewhere, not local → "running on another host".
    assert "remote" in out
    assert "another host" in out

    # Substrate must not appear on the roster board.
    for line in out.splitlines():
        # broker/bridge can legitimately appear inside a sentence; assert they
        # are not rendered as roster rows by checking no row leads with them.
        stripped = line.strip()
        assert not stripped.startswith("broker")
        assert not stripped.startswith("bridge")
    # A non-Running declared process is not "physically up", so it is not shown
    # as running here (it is neither logical nor physically-up).
    assert "dormant" not in out


async def test_agent_ps_empty(tmp_path, capsys):
    """Workspace up but nothing running anywhere → an explicit empty board, exit 0."""
    client = _StubClient(list_processes_result=[_pc_proc("broker", "Running")])
    probe = _StubProbe([])

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=client, probe=probe)

    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() != ""  # must render *something*, not a blank board


async def test_agent_ps_tolerates_data_wrapped_and_nondict_rows(tmp_path, capsys):
    """The physical view survives the ``{"data": [...]}`` shape and junk rows.

    Process Compose's process-list wire shape wobbles across versions (bare list
    vs. ``{"data": [...]}``); a non-dict entry must be skipped, not crash the
    board. Both robustness paths are pinned so a shape wobble never blanks ps.
    """
    client = _StubClient(
        list_processes_result={
            "data": [
                _pc_proc("assistant", "Running"),
                "not-a-dict",  # defensive skip, must not crash
            ]
        }
    )
    probe = _StubProbe(["assistant"])

    rc = await roster.agent_ps(_home(tmp_path), server_urls=_SERVERS, client=client, probe=probe)

    assert rc == 0
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "running" in out


# --- default wiring (production seams, no real broker) -----------------------


def test_agent_start_defaults_to_per_home_process_compose_client(tmp_path):
    """With no ``client`` injected, the per-home ``ProcessComposeClient`` is built.

    The default REST client must target the port :func:`lifecycle.pc_port_for`
    derives from ``$CALFCORD_HOME`` — the same port ``up -p`` pins — so a second
    install on one host does not collide. Asserting the resolver wiring (not a
    live call) keeps this unit-pure.
    """
    from calfcord.supervisor.client import ProcessComposeClient
    from calfcord.supervisor.lifecycle import pc_port_for

    home = _home(tmp_path)
    client = roster._resolve_client(None, home)

    assert isinstance(client, ProcessComposeClient)
    expected = ProcessComposeClient(port=pc_port_for(home))
    # The base URL bakes in the derived port; equal base URLs prove equal ports.
    assert client._base_url == expected._base_url


async def test_default_probe_delegates_to_probe_live_roster(monkeypatch):
    """The default probe adapts ``_probe_live_roster`` to the injectable shape.

    With no ``probe`` injected, the resolver returns a closure that calls
    :func:`_probe_live_roster` (the calfkit 0.12 native-mesh read) with the given
    ``server_urls``. Monkeypatching that function lets the closure body run with
    no real broker, pinning the production delegation (the seam tests otherwise
    bypass). The mesh carries NAMES, so the default probe returns agent names.
    """
    seen: dict[str, str] = {}

    async def _fake_probe_live_roster(server_urls: str):
        seen["server_urls"] = server_urls
        return ["assistant"]

    monkeypatch.setattr(roster, "_probe_live_roster", _fake_probe_live_roster)

    default_probe = roster._resolve_probe(None)
    result = await default_probe(_SERVERS)

    assert seen["server_urls"] == _SERVERS
    assert result == ["assistant"]


# --- _probe_live_roster body (the native-mesh read; previously monkeypatched away) ---


class _ProbeFakeClient:
    """Scriptable stand-in for the short-lived Client ``_probe_live_roster`` opens.

    Controls ``mesh.get_agents()`` (return a roster or raise) and records ``aclose()``
    so a test can assert the connection is always cleaned up — even on the error paths.
    """

    def __init__(self, *, agents=None, get_agents_error=None) -> None:
        self._agents = agents or {}
        self._get_agents_error = get_agents_error
        self.mesh = self  # client.mesh.get_agents() resolves back here
        self.aclosed = False

    async def get_agents(self):
        if self._get_agents_error is not None:
            raise self._get_agents_error
        return self._agents

    async def aclose(self) -> None:
        self.aclosed = True


def _patch_probe_client(monkeypatch, fake: _ProbeFakeClient) -> None:
    monkeypatch.setattr(roster.Client, "connect", lambda *a, **k: fake)


async def test_probe_returns_sorted_online_names(monkeypatch):
    """Success: the online agent NAMES from the mesh, sorted; connection closed."""
    fake = _ProbeFakeClient(agents={"n2": SimpleNamespace(name="scribe"), "n1": SimpleNamespace(name="assistant")})
    _patch_probe_client(monkeypatch, fake)
    assert await roster._probe_live_roster(_SERVERS) == ["assistant", "scribe"]
    assert fake.aclosed is True


async def test_probe_empty_readable_roster_returns_empty(monkeypatch):
    """A *readable* roster with nobody online returns [] — distinct from an
    unreadable roster (which raises). This is the only path that yields []."""
    fake = _ProbeFakeClient(agents={})
    _patch_probe_client(monkeypatch, fake)
    assert await roster._probe_live_roster(_SERVERS) == []
    assert fake.aclosed is True


@pytest.mark.parametrize(
    "error",
    [
        MeshUnavailableError("establishing", reason="establishing"),
        MeshUnavailableError("no topic yet", reason="open_failed"),
        MeshUnavailableError("reader died", reason="reader_dead"),
        ConnectionError("broker down"),
    ],
    ids=["establishing", "open_failed", "reader_dead", "broker_down"],
)
async def test_probe_propagates_when_roster_unreadable(monkeypatch, error):
    """No broker pre-flight: get_agents() raises at call time when the roster can't
    be read — a down broker, a not-yet-created topic, a still-establishing view, or a
    dead reader — and _probe_live_roster PROPAGATES it (never masks it as []) so the
    caller's ``except Exception`` degrades ("broker unreachable"). The connection is
    still closed on every raise."""
    fake = _ProbeFakeClient(get_agents_error=error)
    _patch_probe_client(monkeypatch, fake)
    with pytest.raises(type(error)):
        await roster._probe_live_roster(_SERVERS)
    assert fake.aclosed is True
