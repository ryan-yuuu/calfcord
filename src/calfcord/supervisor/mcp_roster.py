"""Per-server MCP lifecycle: ``mcp-<server>`` roster slots clocking in/out.

Each ``mcp.json`` server runs as its own Process Compose slot (see
:func:`calfcord.supervisor.compose.build_compose_project`), so the verbs
here are the agent-roster shape — including the not-declared reload path: a
server added to ``mcp.json`` *after* ``calfcord start`` has no declared
slot, exactly like a brand-new agent ``.md``, and gets the same
workspace-reload hint instead of a raw 4xx.

What they deliberately lack is the agent-only broker-wide duplicate guard:
two hosts hosting the same toolbox id are competing consumers on one
dispatch topic — a legitimate (if unusual) scale-out, not the agent
split-brain where two same-id agents double-reply in Discord.

``mcp start <server>`` of a Running slot restarts it in place (behavior #2),
which is also the documented way to re-apply an edited ``mcp.json`` entry;
``mcp start --all`` sweeps every *configured* server, making it the
"re-pick up mcp.json" command. Stop/restart sweeps operate on the *running*
``mcp-`` slots instead — stopping what exists, not what is configured.
"""

from __future__ import annotations

import os

from calfcord.mcp.selector import is_valid_server_name
from calfcord.supervisor._workspace import (
    WORKSPACE_NOT_RUNNING_HINT,
    iter_process_dicts,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient, ProcessComposeError
from calfcord.supervisor.compose import MCP_SLOT_PREFIX
from calfcord.supervisor.compose import mcp_slot_name as slot_name

_NOT_RUNNING_HINT = WORKSPACE_NOT_RUNNING_HINT
_PC_RUNNING = "Running"


def _reload_hint(server: str) -> str:
    """The not-declared message: a server added after ``calfcord start``."""
    return (
        f"mcp server {server} is not in the running workspace. A server added "
        "to mcp.json after `calfcord start` needs a workspace reload: run "
        "`calfcord stop` then `calfcord start` (an in-place update would "
        "bounce the broker and bridge)."
    )


def _not_declared_exit(exc: ProcessComposeError, server: str, verb: str) -> int:
    """Map a 4xx to the reload hint (exit 1); re-raise anything else loudly.

    Shared by start/restart so the not-declared-vs-genuine-fault split
    (mirroring ``roster.agent_start``) lives in exactly one place here.
    """
    if exc.status_code is not None and 400 <= exc.status_code < 500:
        print(_reload_hint(server))
        return 1
    raise RuntimeError(
        f"mcp_{verb}: {verb}ing MCP server {server!r} failed against the "
        f"local supervisor (not a not-declared 4xx): {exc}"
    ) from exc


def _check_server_name(server: str) -> bool:
    """Refuse a name that could never match a declared slot, pre-REST."""
    if is_valid_server_name(server):
        return True
    print(
        f"error: invalid MCP server name {server!r}; "
        "must match [a-z0-9_]{1,64} (an mcp.json key)"
    )
    return False


async def _running_mcp_slots(client: ProcessComposeClient) -> set[str]:
    """This host's ``mcp-`` slots currently in the ``Running`` state."""
    running: set[str] = set()
    for item in iter_process_dicts(await client.list_processes()):
        name = item.get("name")
        if (
            isinstance(name, str)
            and name.startswith(MCP_SLOT_PREFIX)
            and item.get("status") == _PC_RUNNING
        ):
            running.add(name)
    return running


async def running_servers(client: ProcessComposeClient) -> set[str]:
    """Bare server names of this host's Running MCP slots.

    The public read for anything outside this module (``mcp list``'s state
    column): callers get server names, never slot names, so the
    ``mcp-`` prefix convention stays encapsulated here and in compose.
    """
    return {
        slot.removeprefix(MCP_SLOT_PREFIX) for slot in await _running_mcp_slots(client)
    }


async def mcp_start(
    home: str | os.PathLike[str],
    *,
    server: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Bring MCP server ``server`` online; a Running slot is restarted in place.

    Returns a POSIX exit code. Workspace check first; start-of-running is a
    restart (behavior #2 — also the edited-entry pickup); a 4xx from the
    supervisor is the not-declared case (server added after ``calfcord
    start``) and prints the workspace-reload hint, while a 5xx / transport
    fault is genuine infra and propagates loudly.
    """
    if not _check_server_name(server):
        return 1
    home = os.fspath(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    return await _start_checked(client, server, await _running_mcp_slots(client))


async def _start_checked(
    client: ProcessComposeClient, server: str, running_slots: set[str]
) -> int:
    """Start (or restart-in-place) one server, workspace already verified.

    ``running_slots`` is passed in so the ``--all`` sweep reads the
    supervisor's process list once for N servers instead of N times.
    """
    slot = slot_name(server)
    if slot in running_slots:
        await client.restart_process(slot)
        print(f"mcp server {server} restarted")
        return 0

    try:
        await client.start_process(slot)
    except ProcessComposeError as exc:
        return _not_declared_exit(exc, server, "start")

    print(f"mcp server {server} online")
    return 0


async def mcp_stop(
    home: str | os.PathLike[str],
    *,
    server: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take MCP server ``server`` offline (``PATCH /process/stop``)."""
    if not _check_server_name(server):
        return 1
    home = os.fspath(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    await client.stop_process(slot_name(server))
    print(f"mcp server {server} stopped")
    return 0


async def mcp_restart(
    home: str | os.PathLike[str],
    *,
    server: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Reload MCP server ``server`` after an mcp.json edit (``POST`` restart).

    Issues the restart unconditionally (a stopped slot restarting back up is
    the expected effect). The 4xx → reload-hint branch mirrors
    :func:`mcp_start`: restart of a never-declared slot is the same
    added-after-``start`` case.
    """
    if not _check_server_name(server):
        return 1
    home = os.fspath(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    try:
        await client.restart_process(slot_name(server))
    except ProcessComposeError as exc:
        return _not_declared_exit(exc, server, "restart")
    print(f"mcp server {server} restarted")
    return 0


async def mcp_start_all(
    home: str | os.PathLike[str],
    *,
    servers: list[str],
    client: ProcessComposeClient | None = None,
) -> int:
    """Start (or restart-in-place) every *configured* server — the
    "re-pick up mcp.json" sweep.

    ``servers`` is the caller-enumerated mcp.json name list (the CLI reads it
    via the no-secrets ``list_server_names``). Per-server failures don't stop
    the sweep; the exit code aggregates (0 only if every server succeeded).
    """
    if not servers:
        print("no MCP servers configured; add one with `calfcord mcp add`")
        return 0
    home = os.fspath(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    running_slots = await _running_mcp_slots(client)
    worst = 0
    for server in servers:
        if not _check_server_name(server):
            worst = max(worst, 1)
            continue
        worst = max(worst, await _start_checked(client, server, running_slots))
    return worst


async def mcp_stop_all(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
) -> int:
    """Stop every *running* ``mcp-`` slot on this host."""
    home = os.fspath(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    running = sorted(await _running_mcp_slots(client))
    if not running:
        print("no MCP servers running on this host")
        return 0
    for slot in running:
        await client.stop_process(slot)
        print(f"mcp server {slot.removeprefix(MCP_SLOT_PREFIX)} stopped")
    return 0


async def mcp_restart_all(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
) -> int:
    """Restart every *running* ``mcp-`` slot on this host."""
    home = os.fspath(home)
    client = resolve_client(client, home)

    if not await workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    running = sorted(await _running_mcp_slots(client))
    if not running:
        print("no MCP servers running on this host")
        return 0
    for slot in running:
        await client.restart_process(slot)
        print(f"mcp server {slot.removeprefix(MCP_SLOT_PREFIX)} restarted")
    return 0
