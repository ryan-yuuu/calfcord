"""Generic component lifecycle: a named SINGLETON roster process clocking in/out.

The router, the tools host, and the MCP host are each a single declared Process
Compose slot — unlike agents (of which a host runs many, and which can collide
org-wide), a component is exactly one process per role per host. Their start/stop
flow is therefore the same workspace-check-then-REST shape as
:mod:`calfcord.supervisor.roster`, *minus* the agent-only pieces:

* **No broker-wide duplicate guard.** ``agent_start`` probes the org for a name
  already answering anywhere (two same-id agents double-reply / split-brain). A
  singleton component cannot duplicate on one host — there is one declared slot —
  so a probe would be dead work. The role-specific veneer above (``router_start``)
  owns any *cross-host* policy; this base stays minimal.
* **No not-declared reload path.** Components are always pre-declared in the
  generated project (substrate + the three fixed roster slots), so a
  ``start_process`` failure here is a genuine REST/infra fault, not "a brand-new
  agent authored after ``start``" — it is left to propagate (a loud raise) rather
  than mistranslated into the agent-only reload hint.

This is the DRY base ``router start|stop``, ``tools start|stop``, and
``mcp start|stop`` all build on, so the workspace-check + the REST call site live
in exactly one place. Like the rest of :mod:`calfcord.supervisor`, it is kept off
the bridge-only secrets path (no ``calfcord.mcp.config`` import) so it stays
importable from the CLI entry point and on a host with no MCP credentials.
"""

from __future__ import annotations

import os

from calfcord.supervisor._workspace import (
    WORKSPACE_NOT_RUNNING_HINT,
    iter_process_dicts,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient

# The single hint shown when an op needs a running workspace and there isn't one;
# the one shared :data:`_workspace.WORKSPACE_NOT_RUNNING_HINT` (Fix #14), aliased
# for the call sites below so every lifecycle surface speaks the same one voice.
_NOT_RUNNING_HINT = WORKSPACE_NOT_RUNNING_HINT

# Process Compose's "this process is up" status string (v1.110.0). A component
# counts as locally up — and so a ``start`` becomes a restart (behavior #2) —
# only when its declared slot is in this exact state; a Stopped/Pending/Disabled
# slot is not "running here" and a ``start`` is a genuine clock-in. The same
# constant the agent-roster physical view keys on, kept in lockstep so both
# surfaces read the same one liveness word.
_PC_RUNNING = "Running"

# A per-home client resolver alias kept for the call sites + the test that pins the
# default wiring (``test_component._resolve_client``); the body is the one shared
# :func:`_workspace.resolve_client` (Fix #14 consolidation).
_resolve_client = resolve_client

# A workspace-readiness alias kept for the call sites below; the body is the one
# shared :func:`_workspace.workspace_is_up` (Fix #14 consolidation).
_workspace_is_up = workspace_is_up


async def _is_running_locally(client: ProcessComposeClient, name: str) -> bool:
    """Whether this host's declared slot ``name`` is in the ``Running`` state.

    Reads Process Compose's ``GET /processes`` and looks for ``name`` reported
    ``Running`` — the same physical-liveness read the agent-roster ps view uses,
    via the one shared :func:`_workspace.iter_process_dicts` wire-shape tolerance
    (bare list vs ``{"data": [...]}``, skip non-dicts). It backs the behavior #2
    start-becomes-restart decision: a singleton that is already up is restarted in
    place rather than re-``start``ed.
    """
    for item in iter_process_dicts(await client.list_processes()):
        if item.get("name") == name:
            return item.get("status") == _PC_RUNNING
    return False


async def component_start(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Bring the singleton component ``name`` online (``POST /process/start``).

    Returns a POSIX exit code. Workspace check first: if the supervisor REST is
    unreachable there is nothing to start, so print the not-running hint and return
    ``1`` *before* a doomed start.

    **Start of an already-running component is a restart (behavior #2).** A
    component's node bakes its config at construction, so re-running ``start`` on a
    slot that is already ``Running`` locally is the useful idempotency: re-apply an
    edited config (``router set`` etc.) by restarting in place (``POST
    /process/restart``, print ``<name> restarted``) rather than a no-op ``POST
    start`` the supervisor would reject for a running slot. Otherwise start the
    named declared slot, print ``<name> online``, and return ``0``.

    No duplicate guard (a singleton cannot duplicate on one host) and no
    not-declared reload path (components are always pre-declared) — a
    ``start_process`` fault here is genuine infra and propagates.

    ``client`` is injected for testing; in production it defaults to a per-home
    REST client.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    if await _is_running_locally(client, name):
        await client.restart_process(name)
        print(f"{name} restarted")
        return 0

    await client.start_process(name)
    print(f"{name} online")
    return 0


async def component_stop(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take the singleton component ``name`` offline (``PATCH /process/stop``).

    Workspace check first (the not-running hint + return ``1`` if the office isn't
    open); otherwise stop the named slot, print ``<name> stopped``, return ``0``.

    ``client`` is injected for testing; in production it defaults to a per-home
    REST client.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    await client.stop_process(name)
    print(f"{name} stopped")
    return 0


async def component_restart(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Reload the singleton component ``name`` after a config edit (``POST`` restart).

    The node bakes its config at construction, so a restart is how a ``router
    set`` / ``router edit`` (or a tools/mcp config change) takes effect on a
    *running* singleton. Workspace check first (the not-running hint + return ``1``
    if the office isn't open); otherwise ``POST /process/restart/{name}``, print
    ``<name> restarted``, return ``0``.

    Unlike ``component_start`` this never consults the Running set: restart is the
    verb's whole job, so it issues the REST restart unconditionally (a stopped slot
    restarting back up is the correct, expected effect).

    ``client`` is injected for testing; in production it defaults to a per-home
    REST client.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    await client.restart_process(name)
    print(f"{name} restarted")
    return 0
