"""Render the Process Compose project that supervises a calfcord host.

The Process Compose YAML is *derived state* — calfcord generates it from the
agents on disk and config; the user never edits it (design §3.1). This module is
the pure heart of that generation: agent ids + home dir + launcher prefix in, a
process-compose project ``dict`` out. No filesystem, no broker, no network — so
the structure is fully golden-testable and the same generator works whether the
host runs detached, in dev, or under a future supervisor swap.

The shape is the §13.2 Phase-0 contract, pinned against Process Compose
``v1.110.0`` (config schema ``version: "0.5"``):

* **Substrate** (``broker``, ``bridge``) autostarts under ``calfcord start``;
  **roster** (every agent + ``tools``) is declared
  ``disabled`` and waits for an explicit ``... start`` (a ``POST /process/start``).
  "Nothing runs that the user did not start" is a trust property, so the split is
  encoded here, not left to the launcher.
* Every ``command`` invokes the calfcord *launcher* (the shim) rather than a
  reconstructed ``uv run`` line, so the venv + ``--env-file`` + default env come
  from one place and no secret literal is ever inlined into the YAML (design
  §12.3). The launcher prefix is a parameter so this generator is mode-agnostic
  and unit-testable.
* ``bridge`` and roster members gate on the broker via ``depends_on``
  (``process_healthy``); readiness is an ``exec`` probe (the bridge has no HTTP
  server) calling ``<launcher> _healthcheck <component>``.
* ``restart: always`` for the substrate (``broker``, ``bridge``); ``on_failure``
  for the whole roster — every agent *and* ``tools`` — which
  now all run via ``run_worker_until_signal`` and therefore force a non-zero exit
  on any uncommanded exit (a crash *or* a clean signal-less return), while an
  operator-commanded ``stop`` is suppressed from restart by Process Compose
  regardless of exit code. (The bridge stays ``always``: it owns its own signals
  and exits 0 on a clean shutdown, so ``on_failure`` would never fire to recover
  it from an uncommanded clean return.) Never ``exit_on_failure``.

The REST port is intentionally absent: it is a flag to ``process-compose up``
(``-p <PC_PORT>``), not a field in the project file (design §13.2).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

import yaml

# Process Compose config-file schema version (NOT the binary version). v1.110.0
# reads the "0.5" schema; confirmed against the process-compose docs.
COMPOSE_SCHEMA_VERSION = "0.5"

# Readiness-probe cadence — the §13.2 pinned values. An exec probe (the bridge
# exposes no HTTP endpoint) shells out to the launcher's internal healthcheck.
_PROBE_INITIAL_DELAY_SECONDS = 2
_PROBE_PERIOD_SECONDS = 3
_PROBE_TIMEOUT_SECONDS = 5
_PROBE_SUCCESS_THRESHOLD = 1
_PROBE_FAILURE_THRESHOLD = 3

# Autorestart backoff shared by both restart policies (always for the substrate,
# on_failure for the roster); max_restarts 0 == unlimited retries in Process
# Compose, applied uniformly.
_RESTART_BACKOFF_SECONDS = 2
_RESTART_MAX_RESTARTS = 0

# Per-host log rotation (project-level); §13.2.
_LOG_MAX_SIZE_MB = 10
_LOG_MAX_AGE_DAYS = 7
_LOG_MAX_BACKUPS = 5
_LOG_COMPRESS = True

# Graceful shutdown: SIGTERM with a 10s grace window — comfortably above the
# ~2s an agent needs to publish its AgentDepartureEvent — signalling the whole
# group (parent_only: false) so child workers under the shim also stop.
_SHUTDOWN_SIGNAL = 15
_SHUTDOWN_TIMEOUT_SECONDS = 10
_SHUTDOWN_PARENT_ONLY = False

_HEALTHY = "process_healthy"

# The filename stem of the supervisor's *own* log (``process-compose up -L
# <stem>.log``). It is not a process the generator declares, but it sits beside
# the per-process logs and is a legitimate tail target, so its name must agree
# across the modules that write it (``lifecycle._SUPERVISOR_LOG_FILENAME``) and
# read it (``cli.logs``). Both derive from this one stem so the literal can never
# drift: lifecycle reconstructs the filename as ``SUPERVISOR_LOG_STEM + ".log"``,
# and ``cli.logs`` passes the stem to ``_log_location`` (which appends ``.log``).
# Homed here because both consumers already import ``compose`` and ``compose`` is
# import-light, keeping the logs CLI's decoupling intact.
SUPERVISOR_LOG_STEM = "process-compose"

# Process names owned by the substrate + non-agent components. An agent id equal
# to one of these would silently overwrite that process's entry in the shared
# `processes` dict (corrupting the substrate), so the generator rejects it.
_RESERVED_PROCESS_NAMES = frozenset({"broker", "bridge", "tools"})

# The slot-name convention for MCP servers, homed here (like
# SUPERVISOR_LOG_STEM) because compose declares the slots and the roster
# drives them — both must agree on the literal, and compose is the
# import-light module the supervisor package already leans on.
MCP_SLOT_PREFIX = "mcp-"


def mcp_slot_name(server: str) -> str:
    """The Process Compose slot for MCP server ``server`` (``mcp-<server>``)."""
    return f"{MCP_SLOT_PREFIX}{server}"


def _log_location(home: str, name: str) -> str:
    return os.path.join(home, "state", "logs", f"{name}.log")


def _restart(policy: str) -> dict:
    """An ``availability`` block for ``always`` or ``on_failure``."""
    return {
        "restart": policy,
        "backoff_seconds": _RESTART_BACKOFF_SECONDS,
        "max_restarts": _RESTART_MAX_RESTARTS,
    }


def _readiness_probe(launcher: str, component: str) -> dict:
    """An exec readiness probe driving ``depends_on: process_healthy``."""
    return {
        "exec": {"command": f"{launcher} _healthcheck {component}"},
        "initial_delay_seconds": _PROBE_INITIAL_DELAY_SECONDS,
        "period_seconds": _PROBE_PERIOD_SECONDS,
        "timeout_seconds": _PROBE_TIMEOUT_SECONDS,
        "success_threshold": _PROBE_SUCCESS_THRESHOLD,
        "failure_threshold": _PROBE_FAILURE_THRESHOLD,
    }


def _process(
    *,
    command: str,
    home: str,
    name: str,
    disabled: bool,
    restart_policy: str,
    depends_on: Mapping[str, str] | None = None,
    readiness_probe: dict | None = None,
) -> dict:
    proc: dict = {
        "command": command,
        "disabled": disabled,
        "availability": _restart(restart_policy),
        "shutdown": {
            "signal": _SHUTDOWN_SIGNAL,
            "timeout_seconds": _SHUTDOWN_TIMEOUT_SECONDS,
            "parent_only": _SHUTDOWN_PARENT_ONLY,
        },
        "log_location": _log_location(home, name),
    }
    if depends_on is not None:
        proc["depends_on"] = {dep: {"condition": condition} for dep, condition in depends_on.items()}
    if readiness_probe is not None:
        proc["readiness_probe"] = readiness_probe
    return proc


def build_compose_project(
    *, agent_ids: Iterable[str], home: str, launcher: str, mcp_servers: Iterable[str] = ()
) -> dict:
    """Build the Process Compose project that supervises one calfcord host.

    ``agent_ids`` are the roster agents to declare (the caller enumerates the
    host's ``agents/*.md``; this function never reads the filesystem), and
    ``mcp_servers`` the MCP server names to declare (the caller enumerates
    ``mcp.json`` via the no-secrets ``list_server_names`` reader) — each gets
    its own ``mcp-<server>`` roster slot, so one broken server entry can
    never take down sibling servers and each restarts independently. ``home``
    is ``$CALFCORD_HOME`` — only the per-process ``state/logs/<name>.log`` paths
    use it. ``launcher`` is the shim prefix every ``command`` is built on
    (e.g. ``$CALFCORD_HOME/shims/calfcord``); the generator never reconstructs
    ``uv run`` flags or inlines secrets.

    Returns a plain ``dict`` (serialize with :func:`render_compose`). See the
    module docstring for the substrate/roster, restart, depends_on, and probe
    contract this encodes. Raises :class:`ValueError` if an ``agent_id`` collides
    with a reserved substrate/component process name or an MCP server slot.
    """
    agent_ids = list(agent_ids)
    mcp_servers = list(mcp_servers)
    mcp_slots = {mcp_slot_name(server): server for server in mcp_servers}
    reserved = sorted(_RESERVED_PROCESS_NAMES.intersection(agent_ids))
    if reserved:
        raise ValueError(
            f"agent id(s) {reserved} collide with reserved process name(s) "
            f"{sorted(_RESERVED_PROCESS_NAMES)}; rename the agent(s)"
        )
    slot_collisions = sorted(set(mcp_slots).intersection(agent_ids))
    if slot_collisions:
        raise ValueError(
            f"agent id(s) {slot_collisions} collide with MCP server process "
            f"slot(s) (one per mcp.json server, named mcp-<server>); rename the "
            f"agent(s) or the server(s)"
        )

    processes: dict[str, dict] = {}

    # Substrate — autostarts, health-gated. The broker's readiness probe checks
    # metadata reachability (not bare TCP); the bridge's checks the Discord
    # heartbeat. `start` gates downstream on the bridge.
    processes["broker"] = _process(
        command=f"{launcher} broker",
        home=home,
        name="broker",
        disabled=False,
        restart_policy="always",
        readiness_probe=_readiness_probe(launcher, "broker"),
    )
    processes["bridge"] = _process(
        command=f"{launcher} run bridge",
        home=home,
        name="bridge",
        disabled=False,
        restart_policy="always",
        depends_on={"broker": _HEALTHY},
        readiness_probe=_readiness_probe(launcher, "bridge"),
    )

    # Roster — declared disabled; each member clocks in on an explicit start and
    # gates on the broker being healthy. Every roster member (agents *and*
    # tools) runs via run_worker_until_signal, which forces a non-zero
    # exit on any uncommanded exit, so on_failure restarts a crash while an
    # operator-commanded stop is suppressed from restart by Process Compose.
    for agent_id in agent_ids:
        processes[agent_id] = _process(
            command=f"{launcher} run agent {agent_id}",
            home=home,
            name=agent_id,
            disabled=True,
            restart_policy="on_failure",
            depends_on={"broker": _HEALTHY},
        )

    for component in ("tools",):
        processes[component] = _process(
            command=f"{launcher} run {component}",
            home=home,
            name=component,
            disabled=True,
            restart_policy="on_failure",
            depends_on={"broker": _HEALTHY},
        )

    # MCP servers — roster members like agents (disabled, on_failure,
    # broker-gated), one slot per mcp.json server for failure isolation.
    for slot, server in mcp_slots.items():
        processes[slot] = _process(
            command=f"{launcher} run mcp {server}",
            home=home,
            name=slot,
            disabled=True,
            restart_policy="on_failure",
            depends_on={"broker": _HEALTHY},
        )

    return {
        "version": COMPOSE_SCHEMA_VERSION,
        "log_configuration": {
            "rotation": {
                "max_size_mb": _LOG_MAX_SIZE_MB,
                "max_age_days": _LOG_MAX_AGE_DAYS,
                "max_backups": _LOG_MAX_BACKUPS,
                "compress": _LOG_COMPRESS,
            }
        },
        "processes": processes,
    }


def render_compose(*, agent_ids: Iterable[str], home: str, launcher: str, mcp_servers: Iterable[str] = ()) -> str:
    """Render the Process Compose project as a YAML string.

    Thin serializer over :func:`build_compose_project` — ``sort_keys=False`` keeps
    the substrate-before-roster ordering the builder emits, which makes the
    generated file readable even though the user never edits it.
    """
    project = build_compose_project(agent_ids=agent_ids, home=home, launcher=launcher, mcp_servers=mcp_servers)
    return yaml.safe_dump(project, sort_keys=False)
