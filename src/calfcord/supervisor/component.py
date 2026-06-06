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

from calfcord.supervisor.client import ProcessComposeClient
from calfcord.supervisor.lifecycle import pc_port_for

# The single hint shown when an op needs a running workspace and there isn't one.
# Mirrors :data:`calfcord.supervisor.roster._NOT_RUNNING_HINT` so every lifecycle
# surface (substrate, agent roster, components) speaks the same one voice.
_NOT_RUNNING_HINT = "workspace not running (start it with: calfcord start)"


def _resolve_client(client: ProcessComposeClient | None, home: str) -> ProcessComposeClient:
    """Resolve the REST client, defaulting to a per-home supervisor client.

    Mirrors :func:`calfcord.supervisor.lifecycle._resolve_client` /
    :func:`calfcord.supervisor.roster._resolve_client`: the port is derived from
    ``$CALFCORD_HOME`` so a second install on one host talks to its own supervisor
    on the same port the ``up -p`` flag pinned.
    """
    return client if client is not None else ProcessComposeClient(port=pc_port_for(home))


async def _workspace_is_up(client: ProcessComposeClient) -> bool:
    """Whether the supervisor REST server answers — a successful ``project_state``.

    The client raises ``RuntimeError`` on a transport failure (server not up /
    wrong port), which is exactly "the workspace isn't open" here; any other error
    is a real bug and is left to propagate (it is not swallowed into "down").
    """
    try:
        await client.project_state()
    except RuntimeError:
        return False
    return True


async def component_start(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Bring the singleton component ``name`` online (``POST /process/start``).

    Returns a POSIX exit code. Workspace check first: if the supervisor REST is
    unreachable there is nothing to start, so print the not-running hint and return
    ``1`` *before* a doomed start. Otherwise start the named declared slot, print
    ``<name> online``, and return ``0``.

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
