"""Unit tests for the substrate lifecycle orchestration (design §13.1-§13.3).

These exercise ``start`` / ``stop`` / ``status`` and their building blocks with
**no real process-compose binary and no broker**: the REST client, the process
spawner, and the clock are all injected. The spawn seam records every argv so the
detached-launch contract (``up -f ... -D -t=false -p <port> -L <log>``) and the
teardown-on-failure (``down -p <port>``) are pinned by test; a stub client drives
the idempotency probe, the priming reconcile (#494), and the readiness gate down
each branch. No wall-clock waits: ``sleep`` advances a fake monotonic clock.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from calfcord.supervisor import lifecycle

# --- fakes ------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock whose only advance is driven by the injected sleep.

    ``start``'s readiness poll measures elapsed time with ``clock()`` and waits
    between polls with ``sleep()``; wiring ``sleep`` to advance this clock makes
    the timeout deterministic with zero real time elapsed.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _RecordingSpawn:
    """Records every argv it is asked to launch (the process spawner seam)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> None:
        self.calls.append(list(argv))


class _StubClient:
    """A scriptable stand-in for ProcessComposeClient.

    ``project_state_results`` / ``bridge_states`` are pulled one per call; a
    ``RuntimeError`` sentinel models the REST server being unreachable (the real
    client raises RuntimeError on a transport failure). ``update_project`` records
    its (byte-exact) body so the priming reconcile can be asserted exactly once.
    """

    def __init__(
        self,
        *,
        project_state_results: list | None = None,
        bridge_states: list | None = None,
        list_processes_result: list | None = None,
        update_project_raises: Exception | None = None,
        process_info: object = None,
        process_info_raises: Exception | None = None,
    ) -> None:
        self._project_state_results = list(project_state_results or [])
        self._bridge_states = list(bridge_states or [])
        self._list_processes_result = list_processes_result or []
        # When set, the priming reconcile (the buggy first project-update) fails the
        # way the real client signals a PC reconcile / transport error: a raise.
        self._update_project_raises = update_project_raises
        # The declared config the idempotency home-ownership check reads back. A
        # real `get_process_info` returns a process config that embeds the
        # home-specific log path; the check confirms the answering supervisor is
        # THIS home's. `process_info_raises` models the info route being
        # unavailable (the verdict is then "cannot determine").
        self._process_info = process_info
        self._process_info_raises = process_info_raises
        self.update_project_calls: list[str] = []
        self.project_state_call_count = 0
        self.get_process_calls: list[str] = []
        self.get_process_info_calls: list[str] = []

    async def project_state(self):
        self.project_state_call_count += 1
        if not self._project_state_results:
            raise RuntimeError("project_state: connection refused")
        result = self._project_state_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def update_project(self, yaml_text: str):
        self.update_project_calls.append(yaml_text)
        if self._update_project_raises is not None:
            raise self._update_project_raises
        return {}

    async def get_process(self, name: str):
        self.get_process_calls.append(name)
        if not self._bridge_states:
            raise RuntimeError("get_process: connection refused")
        result = self._bridge_states.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def get_process_info(self, name: str):
        self.get_process_info_calls.append(name)
        if self._process_info_raises is not None:
            raise self._process_info_raises
        return self._process_info

    async def list_processes(self):
        return self._list_processes_result


async def _reachable_broker() -> bool:
    """A broker probe that always reports reachable, so start's §13.2 fast-fail
    precondition passes without touching a real broker."""
    return True


def _home(tmp_path: Path) -> str:
    return str(tmp_path)


def _make_fake_binary(path: Path) -> str:
    """An executable file standing in for the process-compose binary."""
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fake_pc_bin(tmp_path, monkeypatch) -> str:
    """Point binary resolution at a dummy executable so start/stop never touch a
    real process-compose; the spawn seam is injected so it is never executed."""
    binary = _make_fake_binary(tmp_path / "fake-process-compose")
    monkeypatch.setenv("CALFCORD_PROCESS_COMPOSE_BIN", binary)
    return binary


# --- resolve_pc_binary ------------------------------------------------------


def test_resolve_pc_binary_prefers_explicit_env(tmp_path, monkeypatch) -> None:
    explicit = _make_fake_binary(tmp_path / "custom-pc")
    monkeypatch.setenv("CALFCORD_PROCESS_COMPOSE_BIN", explicit)
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    assert lifecycle.resolve_pc_binary() == explicit


def test_resolve_pc_binary_falls_back_to_home_bin(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CALFCORD_PROCESS_COMPOSE_BIN", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pc = bin_dir / "process-compose"
    pc.write_text("")
    pc.chmod(0o755)
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    assert lifecycle.resolve_pc_binary() == str(pc)


def test_resolve_pc_binary_falls_back_to_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CALFCORD_PROCESS_COMPOSE_BIN", raising=False)
    # No $CALFCORD_HOME/bin binary; a process-compose on PATH must win.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    on_path = tmp_path / "pathdir" / "process-compose"
    on_path.parent.mkdir()
    on_path.write_text("")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert lifecycle.resolve_pc_binary() == str(on_path)


def test_resolve_pc_binary_raises_actionable_when_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CALFCORD_PROCESS_COMPOSE_BIN", raising=False)
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError) as excinfo:
        lifecycle.resolve_pc_binary()
    message = str(excinfo.value)
    assert "process-compose" in message
    # Actionable: point the user back at the installer to recover.
    assert "install" in message.lower()


# --- pc_port_for ------------------------------------------------------------


def test_pc_port_for_is_deterministic() -> None:
    home = "/srv/calfcord"
    assert lifecycle.pc_port_for(home) == lifecycle.pc_port_for(home)


def test_pc_port_for_differs_per_home() -> None:
    # Two installs on one host must not collide on the default :8080.
    assert lifecycle.pc_port_for("/srv/one") != lifecycle.pc_port_for("/srv/two")


def test_pc_port_for_is_in_documented_high_range() -> None:
    for home in ("/a", "/b/c", "/srv/calfcord", "/Users/x/.calfcord"):
        port = lifecycle.pc_port_for(home)
        assert lifecycle._PORT_RANGE_START <= port <= lifecycle._PORT_RANGE_END
        # Never the supervisor default, which is the whole point of deriving it.
        assert port != 8080


def test_pc_port_for_uses_absolute_path(tmp_path, monkeypatch) -> None:
    # The same install reached via a relative vs absolute path must hash the same,
    # so a CWD-relative invocation does not pick a different port.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "home").mkdir()
    abs_port = lifecycle.pc_port_for(str(tmp_path / "home"))
    rel_port = lifecycle.pc_port_for("home")
    assert abs_port == rel_port


# --- lockfile guard ---------------------------------------------------------


def test_lock_guard_creates_parent_and_acquires(tmp_path) -> None:
    home = _home(tmp_path)
    with lifecycle.lifecycle_lock(home):
        assert (tmp_path / "state").is_dir()


def _acquire_and_release(home: str) -> None:
    """Acquire the lifecycle lock and immediately release it (a single call so a
    contention test can assert the *second* acquire raises without nesting two
    ``with`` blocks)."""
    with lifecycle.lifecycle_lock(home):
        pass


def test_lock_guard_raises_on_contention(tmp_path) -> None:
    home = _home(tmp_path)
    with lifecycle.lifecycle_lock(home), pytest.raises(RuntimeError) as excinfo:
        _acquire_and_release(home)
    assert "in progress" in str(excinfo.value)


def test_lock_guard_releases_after_exit(tmp_path) -> None:
    home = _home(tmp_path)
    with lifecycle.lifecycle_lock(home):
        pass
    # A second acquire after the first releases must succeed (no leaked lock).
    with lifecycle.lifecycle_lock(home):
        pass


# --- start: idempotency -----------------------------------------------------


async def test_start_idempotent_when_already_running(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    # project_state succeeds on the first probe => supervisor already up, and the
    # declared config embeds THIS home's marker path, so the home-ownership check
    # (Fix #11) confirms the answering supervisor is ours and short-circuits.
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={"log_location": os.path.join(home, "state", "logs", "bridge.log")},
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=["assistant"],
        client=client,
        spawn=spawn,
        clock=_FakeClock(),
    )

    assert code == 0
    assert spawn.calls == []  # no second `up`
    assert client.update_project_calls == []  # no reconcile when already open
    assert "already open" in capsys.readouterr().out.lower()


async def test_start_idempotency_rejects_a_different_home_on_a_colliding_port(
    tmp_path, capsys
) -> None:
    # Fix #11: two homes can hash to the same REST port. The idempotency probe
    # verifies only that SOMETHING answers the port, not WHICH home's supervisor —
    # so install B would see install A's supervisor and skip its own launch. Before
    # trusting an "already up" verdict, confirm the answering supervisor is THIS
    # home's via its declared home-specific paths; a DIFFERENT home must fail
    # loudly, never return a false "already open".
    home = _home(tmp_path / "B")
    other_home = str(tmp_path / "A")
    spawn = _RecordingSpawn()
    # The supervisor answers (port collision), but its declared config embeds
    # ANOTHER home's path — it belongs to install A, not B.
    client = _StubClient(
        project_state_results=[{"running": True}],
        process_info={
            "log_location": os.path.join(other_home, "state", "logs", "bridge.log")
        },
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=["assistant"],
        client=client,
        spawn=spawn,
        clock=_FakeClock(),
        broker_probe=_reachable_broker,
    )

    assert code == 1
    # It must NOT have launched a second supervisor and must NOT claim "already open".
    assert spawn.calls == []
    out = capsys.readouterr().out.lower()
    assert "already open" not in out
    assert "error" in out
    # Actionable: it names the port collision so the operator can re-home / repick.
    assert "port" in out


# --- home-ownership match (Fix #11 + the round-2 anchoring) -----------------


async def test_supervisor_belongs_to_home_rejects_a_suffix_home_collision() -> None:
    # The crux the quote-anchored match exists for: a bare-substring scan would
    # find "/calf/state" INSIDE "/data/calf/state/logs/bridge.log" and wrongly
    # claim install A's colliding supervisor is ours. The anchored match requires
    # the marker to OPEN the quoted path value, so a suffix home is rejected
    # (False) — while the genuine same-home supervisor is still recognised (True).
    # A revert to the bare-substring scan flips the first assertion, so this pins
    # the fix (the prior different-home test used non-suffix sibling paths, which
    # both the old and new code reject identically).
    other = _StubClient(process_info={"log_location": "/data/calf/state/logs/bridge.log"})
    assert await lifecycle._supervisor_belongs_to_home(other, "/calf") is False
    same = _StubClient(process_info={"log_location": "/calf/state/logs/bridge.log"})
    assert await lifecycle._supervisor_belongs_to_home(same, "/calf") is True


async def test_supervisor_belongs_to_home_returns_none_when_info_unavailable() -> None:
    # Best-effort: when the info route is unreachable or empty the verdict is
    # "cannot determine" (None), so the caller keeps the prior idempotent
    # "already open" behaviour rather than failing a legitimate restart loudly.
    raising = _StubClient(process_info_raises=RuntimeError("info route unavailable"))
    assert await lifecycle._supervisor_belongs_to_home(raising, "/h") is None
    empty = _StubClient(process_info=None)
    assert await lifecycle._supervisor_belongs_to_home(empty, "/h") is None


# --- start: happy path ------------------------------------------------------


async def test_start_declares_mcp_server_slots(tmp_path, capsys, fake_pc_bin) -> None:
    """``start`` threads the caller-enumerated mcp.json server names through to
    the compose generator, so each server gets its disabled ``mcp-<server>``
    roster slot in the written project."""
    import yaml as _yaml

    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[
            RuntimeError("not up yet"),
            {"running": True},
        ],
        bridge_states=[{"status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=["assistant"],
        mcp_servers=["github"],
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 0
    project = _yaml.safe_load((tmp_path / "state" / "process-compose.yaml").read_text())
    proc = project["processes"]["mcp-github"]
    assert proc["command"] == "/h/shims/calfcord run mcp github"
    assert proc["disabled"] is True



async def test_start_happy_path(tmp_path, capsys, fake_pc_bin) -> None:
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    # First probe: not up (RuntimeError) -> launch. Then the REST server answers,
    # and the bridge reports Ready on the first readiness poll.
    client = _StubClient(
        project_state_results=[
            RuntimeError("not up yet"),
            {"running": True},
        ],
        bridge_states=[{"status": "Running", "is_ready": "Ready"}],
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=["assistant", "scribe"],
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 0

    # The rendered YAML is written to <home>/state/process-compose.yaml.
    yaml_path = tmp_path / "state" / "process-compose.yaml"
    assert yaml_path.is_file()

    # Exactly one detached `up` with the §13.2 flags.
    assert len(spawn.calls) == 1
    argv = spawn.calls[0]
    assert argv[1] == "up"
    assert "-D" in argv
    assert "-t=false" in argv
    assert "-p" in argv
    port_value = argv[argv.index("-p") + 1]
    assert int(port_value) == lifecycle.pc_port_for(home)
    assert "-L" in argv
    log_value = argv[argv.index("-L") + 1]
    assert log_value == str(tmp_path / "state" / "logs" / "process-compose.log")
    assert "-f" in argv
    f_value = argv[argv.index("-f") + 1]
    assert f_value == str(yaml_path)
    # NEVER --no-server (it would kill the REST API the readiness gate needs).
    assert "--no-server" not in argv

    # Priming reconcile (#494): update_project called EXACTLY once, byte-identical
    # to the YAML on disk.
    assert len(client.update_project_calls) == 1
    assert client.update_project_calls[0] == yaml_path.read_text()

    # Readiness gate actually polled the bridge.
    assert client.get_process_calls == ["bridge"]

    # Success banner ALWAYS names the next step (§12.6).
    out = capsys.readouterr().out
    assert "agent start" in out


async def test_start_log_dir_is_created(tmp_path, fake_pc_bin) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"is_ready": "Ready"}],
    )
    await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert (tmp_path / "state" / "logs").is_dir()


async def test_start_waits_for_rest_server_then_primes(tmp_path, fake_pc_bin) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    # First probe: not up. Then two transport errors (server still booting) before
    # the REST server answers; only then does the priming reconcile run.
    client = _StubClient(
        project_state_results=[
            RuntimeError("not up"),
            RuntimeError("booting"),
            RuntimeError("booting"),
            {"running": True},
        ],
        bridge_states=[{"is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert code == 0
    assert len(client.update_project_calls) == 1


# --- start: broker fast-fail precondition (§13.2) ---------------------------


async def test_start_fails_fast_when_broker_unreachable(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # §13.2: the broker is a fast-fail precondition. If it is not reachable, start
    # must bail BEFORE rendering/launching — no `up`, no supervisor — so a down
    # broker fails in a heartbeat instead of after a 90s bridge-readiness timeout.
    home = _home(tmp_path)
    spawn = _RecordingSpawn()

    async def _unreachable() -> bool:
        return False

    # The supervisor is not up (so we pass the idempotency short-circuit), but the
    # broker probe reports unreachable.
    client = _StubClient(project_state_results=[RuntimeError("not up")])

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=spawn,
        clock=_FakeClock(),
        broker_probe=_unreachable,
    )

    assert code == 1
    # Nothing was launched: no `up`, no `down`, no priming reconcile.
    assert spawn.calls == []
    assert client.update_project_calls == []
    # The rendered YAML must NOT have been written (we bail before rendering).
    assert not (tmp_path / "state" / "process-compose.yaml").exists()
    # Actionable error: names the unreachable broker and how to start it.
    out = capsys.readouterr().out.lower()
    assert "broker" in out
    assert "localhost:9092" in out
    assert "calfcord broker" in out


# --- start: readiness timeout -> teardown -> non-zero -----------------------


async def test_start_readiness_timeout_tears_down_and_returns_nonzero(
    tmp_path, capsys, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    spawn_blocking = _RecordingSpawn()
    clock = _FakeClock()
    # Server comes up, prime succeeds, but the bridge never becomes Ready: every
    # readiness poll reports Pending until the timeout budget is spent.
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"status": "Pending", "is_ready": "Not Ready"}] * 1000,
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=spawn,
        spawn_blocking=spawn_blocking,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
        ready_timeout_s=10,
    )

    assert code != 0

    # The detached `spawn` launched the `up` but NOT the teardown: a racy
    # fire-and-forget `down` could let a later `start` collide with a supervisor
    # still shutting down, so teardown must use the BLOCKING seam (§13.3 / Fix 3).
    assert all("down" not in c for c in spawn.calls)
    # Teardown: exactly one blocking `down -p <port>`.
    down_calls = [c for c in spawn_blocking.calls if "down" in c]
    assert len(down_calls) == 1
    down = down_calls[0]
    assert "-p" in down
    assert int(down[down.index("-p") + 1]) == lifecycle.pc_port_for(home)

    # The specific failure is printed (no green-light-that-lies, §12.6).
    out = capsys.readouterr().out
    assert "bridge" in out.lower()


async def test_start_running_but_not_ready_bridge_times_out_and_tears_down(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # The bridge process is Running (status=="Running") but its readiness probe
    # has NOT passed (is_ready=="Not Ready"). The strict gate (§13.3) must treat
    # this as a green-light-that-lies: poll until the budget is spent, tear the
    # substrate down, and return non-zero — NEVER accept Running alone.
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[{"status": "Running", "is_ready": "Not Ready"}] * 1000,
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=spawn,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
        ready_timeout_s=10,
    )

    assert code != 0
    # The bridge was polled (it never went Ready) and a teardown was issued.
    assert client.get_process_calls, "the readiness gate must actually poll the bridge"
    out = capsys.readouterr().out.lower()
    assert "bridge" in out


async def test_start_priming_reconcile_failure_tears_down_and_returns_nonzero(
    tmp_path, capsys, fake_pc_bin
) -> None:
    # Fix #5: the priming reconcile (`update_project`) runs AFTER the detached
    # supervisor is already up. If it raises (a PC reconcile error / transport
    # failure), an unhandled exception would orphan the supervisor and dump a
    # traceback — crashing `calfcord init`, since `start` is the wizard's start_fn.
    # It must fail like the readiness-gate path right below it: tear the substrate
    # back down via the BLOCKING seam, print an actionable error, and return 1.
    home = _home(tmp_path)
    spawn = _RecordingSpawn()
    spawn_blocking = _RecordingSpawn()
    clock = _FakeClock()
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        # The bridge would be Ready, but the priming reconcile blows up first, so
        # the readiness gate must never be reached.
        bridge_states=[{"is_ready": "Ready"}],
        update_project_raises=RuntimeError("process-compose POST /project failed"),
    )

    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=spawn,
        spawn_blocking=spawn_blocking,
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )

    assert code == 1
    # The reconcile was attempted exactly once (the buggy first update).
    assert len(client.update_project_calls) == 1
    # No orphan: the supervisor is torn down via the BLOCKING seam (a racy detached
    # `down` could let a retried `start` collide with a supervisor still stopping).
    assert all("down" not in c for c in spawn.calls)
    down_calls = [c for c in spawn_blocking.calls if "down" in c]
    assert len(down_calls) == 1
    down = down_calls[0]
    assert "-p" in down
    assert int(down[down.index("-p") + 1]) == lifecycle.pc_port_for(home)
    # The readiness gate must NOT have been reached (we bailed at the reconcile).
    assert client.get_process_calls == []
    # An actionable, non-traceback error that points at the supervisor log.
    out = capsys.readouterr().out.lower()
    assert "error" in out
    assert "process-compose.log" in out


async def test_start_server_up_timeout_returns_nonzero_without_priming(
    tmp_path, capsys, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    # Not up on the idempotency probe, then the REST server never answers within
    # the server-up budget: every subsequent project_state raises.
    client = _StubClient(
        project_state_results=[RuntimeError("not up")] * 1000,
        bridge_states=[{"is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert code != 0
    # The REST server never came up, so the priming reconcile must NOT have run.
    assert client.update_project_calls == []
    out = capsys.readouterr().out.lower()
    assert "rest server" in out


async def test_start_readiness_tolerates_transient_bridge_error(
    tmp_path, fake_pc_bin
) -> None:
    home = _home(tmp_path)
    clock = _FakeClock()
    # A transient transport error mid-readiness-poll (the bridge restarting under
    # restart: always) must not abort the gate; the next poll sees it Ready.
    client = _StubClient(
        project_state_results=[RuntimeError("not up"), {"running": True}],
        bridge_states=[RuntimeError("bridge bouncing"), {"is_ready": "Ready"}],
    )
    code = await lifecycle.start(
        home,
        server_urls="localhost:9092",
        launcher="/h/shims/calfcord",
        agent_ids=[],
        client=client,
        spawn=_RecordingSpawn(),
        clock=clock,
        sleep=clock.sleep,
        broker_probe=_reachable_broker,
    )
    assert code == 0
    assert client.get_process_calls == ["bridge", "bridge"]


def test_bridge_is_ready_rejects_non_dict() -> None:
    assert lifecycle._bridge_is_ready(None) is False
    assert lifecycle._bridge_is_ready("Ready") is False


def test_bridge_is_ready_requires_ready_probe_not_just_running() -> None:
    # A green light that lies (§12.6/§13.3): the bridge HAS a readiness probe, so
    # status=="Running" while the probe is "Not Ready" is exactly the false-green
    # the strict gate must reject. Only is_ready=="Ready" counts.
    assert lifecycle._bridge_is_ready({"status": "Running", "is_ready": "Not Ready"}) is False
    assert lifecycle._bridge_is_ready({"status": "Running"}) is False
    assert lifecycle._bridge_is_ready({"is_ready": "Ready"}) is True
    assert lifecycle._bridge_is_ready({"status": "Running", "is_ready": "Ready"}) is True


def test_default_spawn_launches_detached(monkeypatch) -> None:
    # The production spawn must start a session-detached child (so the supervisor
    # outlives the CLI) without inheriting the CLI's stdio. Assert the Popen call
    # shape instead of launching a real process.
    import subprocess

    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    lifecycle._default_spawn(["process-compose", "up"])
    assert captured["argv"] == ["process-compose", "up"]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL


def test_default_spawn_blocking_runs_to_completion_bounded(monkeypatch) -> None:
    # The blocking spawn must RUN-and-WAIT (subprocess.run, not Popen) with a
    # bounded timeout, so `down` synchronously completes before `stop` returns
    # (§13.3). Assert the call shape instead of launching a real process.
    import subprocess

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "run", fake_run)
    lifecycle._default_spawn_blocking(["process-compose", "down"])
    assert captured["argv"] == ["process-compose", "down"]
    # Bounded so a wedged `down` fails loudly instead of hanging the CLI.
    assert captured["kwargs"]["timeout"] == lifecycle._DOWN_TIMEOUT_SECONDS
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL


# --- stop -------------------------------------------------------------------


async def test_stop_idempotent_when_nothing_running(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    spawn_blocking = _RecordingSpawn()
    # REST unreachable => nothing to stop.
    client = _StubClient(project_state_results=[RuntimeError("not up")])
    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)
    assert code == 0
    assert spawn_blocking.calls == []  # no `down` issued
    assert "nothing to stop" in capsys.readouterr().out.lower()


async def test_stop_issues_down_via_blocking_seam(tmp_path, fake_pc_bin) -> None:
    # `down` must be SYNCHRONOUS (§13.3 / Fix 3): a fire-and-forget detached `down`
    # lets `stop` print "workspace closed" and return before the supervisor has
    # actually stopped, so a racing `start` could collide. `stop` therefore issues
    # `down` through the BLOCKING seam, never the detached `spawn`.
    home = _home(tmp_path)
    spawn_blocking = _RecordingSpawn()
    client = _StubClient(project_state_results=[{"running": True}])
    code = await lifecycle.stop(home, client=client, spawn_blocking=spawn_blocking)
    assert code == 0
    # Exactly one blocking `down -p <port>`.
    assert len(spawn_blocking.calls) == 1
    argv = spawn_blocking.calls[0]
    assert "down" in argv
    assert "-p" in argv
    assert int(argv[argv.index("-p") + 1]) == lifecycle.pc_port_for(home)


# --- status -----------------------------------------------------------------


async def test_status_not_running(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    client = _StubClient(project_state_results=[RuntimeError("not up")])
    code = await lifecycle.status(home, client=client)
    assert code == 0
    out = capsys.readouterr().out.lower()
    assert "not running" in out
    assert "calfcord start" in out


async def test_status_running_renders_board(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    processes = [
        {"name": "broker", "status": "Running", "is_ready": "Ready"},
        {"name": "bridge", "status": "Running", "is_ready": "Ready"},
        {"name": "assistant", "status": "Running", "is_ready": "-"},
    ]
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result={"data": processes},
    )
    code = await lifecycle.status(home, client=client)
    assert code == 0
    out = capsys.readouterr().out
    for name in ("broker", "bridge", "assistant"):
        assert name in out
    # Reboot non-survival surfaced honestly somewhere (§12.6): the daemon is
    # session-scoped, so status must say so.
    assert "reboot" in out.lower()


async def test_status_running_empty_roster(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    # Only the substrate is up; the roster board must say "(none running)".
    processes = [{"name": "broker", "status": "Running", "is_ready": "Ready"}]
    client = _StubClient(
        project_state_results=[{"running": True}],
        list_processes_result=processes,  # bare list (no "data" wrapper)
    )
    code = await lifecycle.status(home, client=client)
    assert code == 0
    out = capsys.readouterr().out.lower()
    assert "none running" in out


def test_process_rows_skips_non_dict_items() -> None:
    # A wire-shape wobble (a stray non-dict entry) must be skipped, not crash the
    # board.
    rows = lifecycle._process_rows(["junk", {"name": "broker", "status": "Running"}])
    assert [r["name"] for r in rows] == ["broker"]


# --- lock interaction with start/stop ---------------------------------------


def test_lockfile_path_is_under_state(tmp_path) -> None:
    home = _home(tmp_path)
    assert lifecycle._lock_path(home) == os.path.join(home, "state", "calfcord-lifecycle.lock")


# --- import isolation -------------------------------------------------------

# The lifecycle now imports ``calfcord.health.check`` for the broker fast-fail
# precondition (§13.2). That import must keep aiokafka lazy (it loads only inside
# ``default_broker_probe``'s coroutine), so importing the supervisor stays
# pure-filesystem. A fresh interpreter gives a clean ``sys.modules`` to assert
# against; mirrors ``tests/health/test_check.py``.
_ISOLATION_SCRIPT = """
import sys

import calfcord.supervisor.lifecycle  # noqa: F401

aiokafka_leaked = any(m == "aiokafka" or m.startswith("aiokafka.") for m in sys.modules)
assert not aiokafka_leaked, "supervisor.lifecycle eagerly imported aiokafka (must be lazy in the probe)"
print("ISOLATION_OK")
"""


def test_lifecycle_does_not_import_aiokafka() -> None:
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
