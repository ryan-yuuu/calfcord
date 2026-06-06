"""Roster operations: a teammate clocking in/out of the running office (§3.4-§3.5).

These are the imperative glue above two seams — the broker-wide control-plane
probe (:func:`calfcord.control_plane.probe.probe_live_roster`) and the Process
Compose REST client (:class:`calfcord.supervisor.client.ProcessComposeClient`) —
that bring a *defined* agent online, take it offline, reload it, and report what
is running. They are deliberately *pure-ish orchestration*: every world-touching
dependency (the REST client, the probe, the clock) is injected, so the whole flow
is unit-testable with no real ``process-compose`` binary and no broker.

Three design contracts from the redesign live here:

* **Workspace check first.** Every op probes the local supervisor's REST surface
  (``project_state``) before doing anything else. The client raises ``RuntimeError``
  on a transport failure (server not up / wrong port), which is exactly "the
  office isn't open" — we map it to a one-line actionable hint, not a traceback.

* **Distributed-correct duplicate guard, CLI-side only (§3.5).** ``agent_start``
  first asks the *broker-wide* live roster (the probe) whether this name is
  already answering anywhere — including another host. If so it refuses to start a
  duplicate (which the bridge would otherwise accept as a benign re-announce,
  yielding double-replies / split-brain A2A) and returns ``0``: a duplicate start
  is a benign no-op, not a failure. No bridge change is required because the guard
  reads the wire, not the bridge's memory.

* **A brand-new agent needs a workspace reload, never an ``update_project`` (§13.1).**
  An agent authored *after* ``calfcord start`` is not a declared (``disabled``)
  slot, so ``POST /process/start/{name}`` errors. We do NOT recover by pushing an
  updated project: on v1.110.0 an ``update_project`` that changes the process set
  bounces broker+bridge. Instead we steer the operator to a clean reload
  (``calfcord stop && calfcord start``) and return non-zero.

``agent_ps`` renders the §3.4 union: the LOGICAL view (agents answering across the
whole org, from the probe) unioned with the PHYSICAL view (this host's Running
roster processes, from Process Compose). The cross-product yields three states —
running+registered, started-but-not-yet-registered (physical only), and running
on another host (logical only, expected under multi-host, NOT an error).

Kept off the bridge-only secrets path (no ``calfcord.mcp.config`` import) like the
rest of this package, so it stays importable from the CLI entry point.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

from calfcord.agents.definition import AgentDefinition
from calfcord.control_plane.probe import probe_live_roster
from calfcord.supervisor._workspace import (
    WORKSPACE_NOT_RUNNING_HINT,
    iter_process_dicts,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient, ProcessComposeError
from calfcord.supervisor.compose import _RESERVED_PROCESS_NAMES as _NON_AGENT_PROCESSES

# A broker-wide live-roster probe: hand it ``server_urls`` and it returns the
# AgentDefinitions of every agent answering across the org. Injected so tests
# script the roster without a real broker; production wraps ``probe_live_roster``.
Probe = Callable[[str], Awaitable[list[AgentDefinition]]]

# A wall clock, accepted for symmetry with ``lifecycle.status`` and future
# freshness reconciliation. Unused today; kept so callers need not special-case
# the roster ops when they grow a time dimension.
Clock = Callable[[], float]

# The single hint shown when an op needs a running workspace and there isn't one;
# the one shared :data:`_workspace.WORKSPACE_NOT_RUNNING_HINT` (Fix #14), aliased
# for the call sites below so every roster op speaks with one voice.
_NOT_RUNNING_HINT = WORKSPACE_NOT_RUNNING_HINT

# Process Compose's "this process is up" status string (v1.110.0). Only a Running
# roster process counts as *physically* up for the ps union; a Stopped/Pending
# declared slot is neither logical nor physically-up and so is not "running here".
_PC_RUNNING = "Running"


# A per-home client resolver alias kept for the call sites + the test that pins
# the default wiring (``test_roster._resolve_client``); the body is the one shared
# :func:`_workspace.resolve_client` (Fix #14 consolidation).
_resolve_client = resolve_client


def _resolve_probe(probe: Probe | None) -> Probe:
    """Resolve the live-roster probe, defaulting to the real control-plane probe.

    The default adapts :func:`probe_live_roster` (``(server_urls, *, timeout_s)``)
    to the injectable ``(server_urls) -> ...`` shape, so tests can stub a plain
    async callable.
    """
    if probe is not None:
        return probe

    async def _default_probe(server_urls: str) -> list[AgentDefinition]:
        return await probe_live_roster(server_urls)

    return _default_probe


# A workspace-readiness alias kept for the call sites below; the body is the one
# shared :func:`_workspace.workspace_is_up` (Fix #14 consolidation).
_workspace_is_up = workspace_is_up


async def agent_start(
    home: str | os.PathLike[str],
    *,
    name: str,
    server_urls: str,
    client: ProcessComposeClient | None = None,
    probe: Probe | None = None,
    now: Clock | None = None,
) -> int:
    """Bring agent ``name`` online: a teammate clocking into the live org (§3.5).

    Returns a POSIX exit code. The sequence is deliberate:

    1. **Workspace check** — if the supervisor REST is unreachable there is
       nothing to start; print the not-running hint and return ``1`` *before*
       spending a broker probe or a doomed start.
    2. **Duplicate guard (§3.5)** — query the broker-wide live roster; if ``name``
       is already answering *anywhere* (this host or another), do NOT start a
       second instance (the bridge would accept it as a benign re-announce and
       both would reply). Print a clear message and return ``0`` — a duplicate
       start is a benign no-op, not a failure.
    3. **Start** — otherwise ``POST /process/start/{name}`` against the local
       supervisor; on success print ``agent <name> online`` and return ``0``.
    4. **Not-declared vs. genuine fault (§13.1 / Fix #9)** — if the start raises,
       branch on the STRUCTURAL HTTP status the client carries: a **4xx** is the
       not-declared case (a brand-new agent authored after ``calfcord start`` is
       not a declared slot, so the PC server rejects it), so steer the operator to
       a workspace reload (``calfcord stop && calfcord start``) rather than an
       in-place ``update_project`` (which would bounce broker+bridge on v1.110.0)
       and return ``1``. Anything else — a **5xx**, or a transport failure with no
       status — is a genuine infra fault, NOT a brand-new agent; per the error
       convention it is re-raised loudly with caller/target/correlation rather than
       mistranslated into the benign reload hint that would mask it.

    ``client`` / ``probe`` are injected for testing; ``now`` is accepted for
    symmetry with the rest of the lifecycle surface and is unused today.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Duplicate guard (§3.5): the probe is broker-wide, so a name live on ANY host
    # is caught here, CLI-side, with no bridge change. Refusing to start a second
    # instance is the whole point — two same-id agents double-reply / split-brain.
    probe = _resolve_probe(probe)
    try:
        live = await probe(server_urls)
    except Exception:
        # The duplicate guard is best-effort (§3.5 already concedes a TOCTOU
        # window): if the broker is unreachable we cannot verify org-wide
        # duplicates, so warn and proceed with the local start rather than blocking
        # it — a same-host duplicate is impossible (one declared process per name).
        print("warning: could not verify org-wide duplicates (broker unreachable); proceeding.")
        live = []
    if any(defn.agent_id == name for defn in live):
        print(f"agent {name} is already running in the organization")
        return 0

    try:
        await client.start_process(name)
    except ProcessComposeError as exc:
        # The workspace check above already proved the REST server is up, so branch
        # on the structural status (Fix #9): a 4xx is "no such declared process" —
        # a brand-new agent authored after `calfcord start`. §13.1: do NOT recover
        # with `update_project` (it bounces the substrate); reload cleanly. A 5xx
        # (or no status — a transport fault) is a genuine infra failure, not a
        # brand-new agent, so it is re-raised loudly below rather than mistranslated
        # into the reload hint that would mask it.
        status = exc.status_code
        if status is not None and 400 <= status < 500:
            print(
                f"agent {name} is not in the running workspace. Bringing a brand-new "
                "agent online needs a workspace reload: run `calfcord stop` then "
                "`calfcord start` (an in-place update would bounce the broker and bridge)."
            )
            return 1
        raise RuntimeError(
            f"agent_start: starting agent {name!r} failed against the local "
            f"supervisor (not a not-declared 4xx): {exc}"
        ) from exc

    print(f"agent {name} online")
    return 0


async def agent_stop(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take agent ``name`` offline: a teammate clocking out (``PATCH`` stop).

    Workspace check first (the not-running hint + return ``1`` if the office isn't
    open); otherwise ``PATCH /process/stop/{name}``, print ``agent <name>
    stopped``, return ``0``.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    await client.stop_process(name)
    print(f"agent {name} stopped")
    return 0


async def agent_restart(
    home: str | os.PathLike[str],
    *,
    name: str,
    client: ProcessComposeClient | None = None,
) -> int:
    """Reload agent ``name`` after an edited ``.md`` (``POST`` restart).

    The node bakes its config at construction, so a restart is how a ``.md`` edit
    takes effect on a *running* agent. Workspace check first (the not-running hint
    + return ``1``); otherwise ``POST /process/restart/{name}``, print ``agent
    <name> restarted``, return ``0``.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    await client.restart_process(name)
    print(f"agent {name} restarted")
    return 0


async def agent_ps(
    home: str | os.PathLike[str],
    *,
    server_urls: str,
    client: ProcessComposeClient | None = None,
    probe: Probe | None = None,
    now: Clock | None = None,
) -> int:
    """Render the running-roster board: the §3.4 logical-plus-physical union.

    Returns ``0`` always — ps is read-only, and "nothing running" (including no
    workspace at all) is a valid state, not an error. If the supervisor is
    unreachable, print the not-running hint and return ``0`` *without* spending a
    broker probe (there is nothing local to union against).

    Otherwise it unions two views:

    * **LOGICAL** (global): every agent answering the discovery probe across the
      whole org — true liveness, host-agnostic.
    * **PHYSICAL** (host-local): this host's Process Compose roster processes (the
      non-substrate ones) that are ``Running``.

    The cross-product yields three rendered states (§3.4):

    * physical **and** logical → ``running`` (online here and registered);
    * physical **only** → ``started, not yet registered`` (up here but not yet
      answering — just starting, or wedged);
    * logical **only** → ``running on another host`` (expected under multi-host —
      this is NOT an error; the physical half is host-local by design).

    ``probe`` / ``client`` are injected for testing; ``now`` is accepted for
    symmetry with the lifecycle surface and is unused today.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 0

    physical = _running_roster_names(await client.list_processes())

    probe = _resolve_probe(probe)
    try:
        logical = {defn.agent_id for defn in await probe(server_urls)}
    except Exception:
        # The probe talks to the broker; a broker hiccup must not crash read-only
        # `ps`. Degrade to the physical (host-local) view with a note instead of
        # blowing up.
        logical = set()
        print("note: broker unreachable; showing locally-running agents only.")

    _render_ps_board(physical=physical, logical=logical)
    return 0


def _running_roster_names(payload: object) -> set[str]:
    """The names of this host's *roster* processes that are ``Running`` (§3.4).

    Filters Process Compose's process list to non-substrate (roster) entries in
    the ``Running`` state — the physical half of the ps union. The wire-shape
    tolerance (bare list vs ``{"data": [...]}``, skip non-dicts) is the one shared
    :func:`_workspace.iter_process_dicts` (Fix #14); this only applies the
    roster/Running filter, skipping unnamed entries defensively.
    """
    names: set[str] = set()
    for item in iter_process_dicts(payload):
        name = item.get("name")
        if not name or name in _NON_AGENT_PROCESSES:
            continue
        if item.get("status") == _PC_RUNNING:
            names.add(name)
    return names


def _render_ps_board(*, physical: set[str], logical: set[str]) -> None:
    """Print the three-state roster board for the physical/logical union (§3.4).

    Every name in either view gets exactly one row, sorted for deterministic
    output. The state is decided by which view(s) the name is in (see
    :func:`agent_ps`). An empty union prints an explicit "no agents running" line
    so the board is never a confusing blank.
    """
    everyone = sorted(physical | logical)
    if not everyone:
        print("no agents running in the organization.")
        return

    print("running agents:")
    for name in everyone:
        here = name in physical
        answering = name in logical
        if here and answering:
            state = "running"
        elif here:
            # Up on this host but not answering the probe yet — just starting, or
            # wedged. The drift case the union exists to surface.
            state = "started, not yet registered"
        else:
            # Answering but not a local process — another host is running it.
            # Expected under multi-host (§3.4); NOT an error.
            state = "running on another host"
        print(f"  {name:<16} {state}")
