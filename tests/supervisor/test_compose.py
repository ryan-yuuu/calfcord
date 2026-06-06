"""Golden tests for the Process Compose project generator.

``build_compose_project`` is a pure function: agent ids + home dir + launcher
prefix in, a process-compose project ``dict`` out (no I/O, no broker). The tests
assert on the *parsed* structure — both the dict directly and the round-trip
through :func:`render_compose` / ``yaml.safe_load`` — rather than brittle string
matching, so a formatting change never breaks them while a contract change
(the §13.2 pinned facts) does.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from calfcord.supervisor.compose import build_compose_project, render_compose

_HOME = "/srv/calfcord"
_LAUNCHER = "/srv/calfcord/shims/calfcord"
_AGENTS = ["assistant", "scribe"]


def _project(agent_ids: list[str] | None = None) -> dict:
    return build_compose_project(
        agent_ids=_AGENTS if agent_ids is None else agent_ids,
        home=_HOME,
        launcher=_LAUNCHER,
    )


def _processes(agent_ids: list[str] | None = None) -> dict:
    return _project(agent_ids)["processes"]


def test_substrate_processes_are_present() -> None:
    procs = _processes()
    assert "broker" in procs
    assert "bridge" in procs


def test_roster_processes_are_present() -> None:
    procs = _processes()
    for name in ("tools", "router", "mcp", "assistant", "scribe"):
        assert name in procs


def test_substrate_autostarts_roster_is_disabled() -> None:
    procs = _processes()
    # Substrate: nothing runs that the user did not start, except the office itself.
    assert procs["broker"]["disabled"] is False
    assert procs["bridge"]["disabled"] is False
    # Roster: present but waits for an explicit `agent/router/tools/mcp start`.
    for name in ("tools", "router", "mcp", "assistant", "scribe"):
        assert procs[name]["disabled"] is True


def test_broker_has_no_dependencies() -> None:
    # The broker is the root of the office; nothing precedes it.
    assert "depends_on" not in _processes()["broker"]


def test_bridge_depends_on_broker_health() -> None:
    assert _processes()["bridge"]["depends_on"] == {
        "broker": {"condition": "process_healthy"}
    }


def test_every_roster_member_health_gates_on_broker() -> None:
    procs = _processes()
    for name in ("tools", "router", "mcp", "assistant", "scribe"):
        assert procs[name]["depends_on"] == {"broker": {"condition": "process_healthy"}}


def test_substrate_readiness_probes_are_exec_only() -> None:
    procs = _processes()
    for component in ("broker", "bridge"):
        probe = procs[component]["readiness_probe"]
        assert set(probe["exec"]) == {"command"}
        # Exec only — the bridge has no HTTP server to http_get against.
        assert "http_get" not in probe
        assert probe["initial_delay_seconds"] == 2
        assert probe["period_seconds"] == 3
        assert probe["timeout_seconds"] == 5
        assert probe["success_threshold"] == 1
        assert probe["failure_threshold"] == 3


def test_readiness_probe_commands_invoke_the_launcher_healthcheck() -> None:
    procs = _processes()
    assert procs["broker"]["readiness_probe"]["exec"]["command"] == (
        f"{_LAUNCHER} _healthcheck broker"
    )
    assert procs["bridge"]["readiness_probe"]["exec"]["command"] == (
        f"{_LAUNCHER} _healthcheck bridge"
    )


def test_roster_has_no_readiness_probe() -> None:
    # Only the substrate is health-gated; roster liveness is reconstructed over
    # the control plane, not via a readiness probe (design §3.4).
    procs = _processes()
    for name in ("tools", "router", "mcp", "assistant", "scribe"):
        assert "readiness_probe" not in procs[name]


def test_substrate_and_agents_restart_always() -> None:
    # broker/bridge/agents exit 0 on a clean signal-less return, so on_failure
    # would never fire — they must restart: always.
    procs = _processes()
    for name in ("broker", "bridge", "assistant", "scribe"):
        availability = procs[name]["availability"]
        assert availability["restart"] == "always"
        assert availability["backoff_seconds"] == 2
        assert availability["max_restarts"] == 0


def test_tools_router_mcp_restart_on_failure() -> None:
    # tools/router/mcp use run_worker_until_signal → non-zero exit, so on_failure
    # restarts them; backoff matches the substrate.
    procs = _processes()
    for name in ("tools", "router", "mcp"):
        availability = procs[name]["availability"]
        assert availability["restart"] == "on_failure"
        assert availability["backoff_seconds"] == 2
        # Unlimited restarts (0) is the deliberate policy for this group too, so
        # pin it like the always group — a future finite cap shouldn't slip in silently.
        assert availability["max_restarts"] == 0


def test_no_process_uses_exit_on_failure() -> None:
    for proc in _processes().values():
        assert proc["availability"]["restart"] != "exit_on_failure"


def test_command_strings_invoke_the_launcher() -> None:
    procs = _processes(["assistant", "scribe"])
    assert procs["broker"]["command"] == f"{_LAUNCHER} broker"
    assert procs["bridge"]["command"] == f"{_LAUNCHER} run bridge"
    assert procs["assistant"]["command"] == f"{_LAUNCHER} run agent assistant"
    assert procs["scribe"]["command"] == f"{_LAUNCHER} run agent scribe"
    assert procs["tools"]["command"] == f"{_LAUNCHER} run tools"
    assert procs["router"]["command"] == f"{_LAUNCHER} run router"
    assert procs["mcp"]["command"] == f"{_LAUNCHER} run mcp"


def test_launcher_prefix_is_parameterized() -> None:
    # A different launcher (e.g. a dev `uv run calfcord-cli` shim) flows through
    # untouched — the generator never reconstructs uv-run flags.
    procs = build_compose_project(
        agent_ids=["assistant"], home=_HOME, launcher="uv run calfcord-cli"
    )["processes"]
    assert procs["broker"]["command"] == "uv run calfcord-cli broker"
    assert procs["assistant"]["command"] == "uv run calfcord-cli run agent assistant"


def test_per_process_log_locations_live_under_state_logs() -> None:
    procs = _processes(["assistant"])
    for name in ("broker", "bridge", "assistant", "tools", "router", "mcp"):
        assert procs[name]["log_location"] == f"{_HOME}/state/logs/{name}.log"


def test_every_process_has_a_shutdown_block() -> None:
    for proc in _processes().values():
        assert proc["shutdown"] == {
            "signal": 15,
            "timeout_seconds": 10,
            "parent_only": False,
        }


def test_project_declares_the_compose_schema_version() -> None:
    # Process Compose v1.110.0 reads the "0.5" config schema (NOT the binary tag).
    assert _project()["version"] == "0.5"


def test_project_level_log_rotation_block() -> None:
    assert _project()["log_configuration"]["rotation"] == {
        "max_size_mb": 10,
        "max_age_days": 7,
        "max_backups": 5,
        "compress": True,
    }


def test_no_agents_still_yields_a_valid_substrate() -> None:
    procs = _processes([])
    assert {"broker", "bridge", "tools", "router", "mcp"} == set(procs)


def test_reserved_agent_id_is_rejected() -> None:
    # An agent named like a substrate/component process would silently clobber it
    # via the shared `processes` dict key — reject it loudly instead of corrupting
    # the substrate.
    for reserved in ("broker", "bridge", "tools", "router", "mcp"):
        with pytest.raises(ValueError):
            build_compose_project(agent_ids=[reserved], home=_HOME, launcher=_LAUNCHER)


def test_render_round_trips_to_the_same_structure() -> None:
    rendered = render_compose(agent_ids=_AGENTS, home=_HOME, launcher=_LAUNCHER)
    assert isinstance(rendered, str)
    assert yaml.safe_load(rendered) == _project()


def test_render_emits_substrate_before_roster() -> None:
    # sort_keys=False keeps the readable substrate-first ordering the builder emits.
    rendered = render_compose(agent_ids=_AGENTS, home=_HOME, launcher=_LAUNCHER)
    order = list(yaml.safe_load(rendered)["processes"])
    assert order[:2] == ["broker", "bridge"]


# Importing the generator must never pull in the bridge-only MCP loader
# (``calfcord.mcp.config`` expands ``$VAR`` secrets from mcp.json — design §12.3).
# A fresh interpreter gives a clean ``sys.modules`` to assert against; mirrors
# ``tests/mcp/test_import_isolation.py``.
_ISOLATION_SCRIPT = """
import sys

import calfcord.supervisor.compose  # noqa: F401

leaked = "calfcord.mcp.config" in sys.modules
assert not leaked, (
    "supervisor.compose transitively imported the bridge-only MCP loader "
    "(all calfcord.mcp.*: "
    + repr([m for m in sys.modules if m.startswith("calfcord.mcp")])
    + ")"
)
print("ISOLATION_OK")
"""


def test_compose_does_not_import_mcp_config() -> None:
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
        "isolation subprocess exited 0 but did not run to completion "
        f"(no ISOLATION_OK sentinel)\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
