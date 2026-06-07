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
    live: list[AgentDefinition] | None = None,
    now: Clock | None = None,
) -> int:
    """Bring agent ``name`` online: a teammate clocking into the live org (§3.5).

    Returns a POSIX exit code. The sequence is deliberate:

    1. **Workspace check** — if the supervisor REST is unreachable there is
       nothing to start; print the not-running hint and return ``1`` *before*
       spending a broker probe or a doomed start.
    2. **Already-running-here is a restart (behavior #2)** — if ``name`` is a
       ``Running`` process on THIS host, a re-``start`` is the useful idempotency:
       reload it in place (``POST /process/restart/{name}``), print ``agent <name>
       restarted``, return ``0``. This branch comes BEFORE the org probe so a local
       instance is never mistaken for a remote duplicate — it is ours to restart,
       not a second host's to refuse.
    3. **Duplicate guard (§3.5)** — otherwise query the broker-wide live roster; if
       ``name`` is already answering on *another host*, do NOT start a second
       instance (the bridge would accept it as a benign re-announce and both would
       reply). Print a clear message and return ``0`` — a duplicate start is a
       benign no-op, not a failure.
    4. **Start** — otherwise ``POST /process/start/{name}`` against the local
       supervisor; on success print ``agent <name> online`` and return ``0``.
    5. **Not-declared vs. genuine fault (§13.1 / Fix #9)** — if the start raises,
       branch on the STRUCTURAL HTTP status the client carries: a **4xx** is the
       not-declared case (a brand-new agent authored after ``calfcord start`` is
       not a declared slot, so the PC server rejects it), so steer the operator to
       a workspace reload (``calfcord stop && calfcord start``) rather than an
       in-place ``update_project`` (which would bounce broker+bridge on v1.110.0)
       and return ``1``. Anything else — a **5xx**, or a transport failure with no
       status — is a genuine infra fault, NOT a brand-new agent; per the error
       convention it is re-raised loudly with caller/target/correlation rather than
       mistranslated into the benign reload hint that would mask it.

    ``client`` / ``probe`` are injected for testing. ``live`` is the
    pre-resolved broker-wide roster: when given (the ``start --all`` sweep probes
    once and threads it in), the duplicate guard reads it directly and does NOT
    re-probe — so N agents cost ONE probe and ONE aggregate broker-down warning,
    not N. When ``None`` (the standalone single-start), this probes itself, exactly
    as before. ``now`` is accepted for symmetry with the rest of the lifecycle
    surface and is unused today.
    """
    # Reserved-name chokepoint: the substrate (broker/bridge) and the singleton
    # components (tools/router/mcp) are owned by `calfcord start` and their own
    # component verbs — never the agent roster. The id pattern does NOT reject a
    # creatable `tools.md` (only `calfcord start`'s build_compose_project does), so
    # an `agent start tools` would otherwise drive `start_process('tools')` against
    # the live singleton. Refuse here, before any workspace check / probe / start,
    # so this single seam closes the exposure for both `agent start <reserved>` and
    # (via the upstream filter in agent_start_all) `start --all`.
    if name in _NON_AGENT_PROCESSES:
        print(
            f"error: {name!r} is a reserved component, not an agent; "
            f"manage it with `calfcord {name} start` (or `calfcord start`)."
        )
        return 1

    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    # Already-running-here is a restart (behavior #2). A re-`start` of a process
    # that is Running on THIS host reloads it in place — the same effect as
    # `restart`. This is checked BEFORE the org probe so a *local* instance is never
    # misread as a remote duplicate: it is ours to restart, not a peer's to refuse.
    if name in await _running_agent_names(client):
        await client.restart_process(name)
        print(f"agent {name} restarted")
        return 0

    # Duplicate guard (§3.5): the probe is broker-wide, so a name live on ANOTHER
    # host is caught here, CLI-side, with no bridge change. Refusing to start a
    # second instance is the whole point — two same-id agents double-reply /
    # split-brain. (A same-host duplicate was already handled as a restart above.)
    # When the bulk sweep pre-resolved the roster (``live`` given), reuse it — it
    # already probed once and emitted any single aggregate broker-down warning, so
    # we must NOT re-probe (that would be N round-trips / N warnings for one
    # operator action). A standalone single-start (``live is None``) probes itself.
    if live is None:
        probe = _resolve_probe(probe)
        try:
            live = await probe(server_urls)
        except Exception:
            # The duplicate guard is best-effort (§3.5 already concedes a TOCTOU
            # window): if the broker is unreachable we cannot verify org-wide
            # duplicates, so warn and proceed with the local start rather than
            # blocking it — a same-host duplicate is impossible (one declared
            # process per name).
            print(
                "warning: could not verify org-wide duplicates "
                "(broker unreachable); proceeding."
            )
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


async def agent_start_all(
    home: str | os.PathLike[str],
    *,
    agent_ids: list[str],
    server_urls: str,
    client: ProcessComposeClient | None = None,
    probe: Probe | None = None,
    now: Clock | None = None,
) -> int:
    """Bring every DEFINED agent online on this host (``start --all``, behavior #1).

    ``--all`` is the uniform-surface bulk verb (decision B): for ``start`` the
    target is every *defined* agent (the caller passes ``agent_ids`` from the
    ``.md`` files — roster.py stays off the agents-dir read so it does not grow a
    disk dependency). Each id runs the SAME single-start logic as ``agent_start``,
    so a locally-running one restarts (behavior #2), a stopped one starts, and one
    only answering on another host hits the duplicate refusal — one honest,
    LOCAL-only sweep of this host's supervisor.

    Workspace check first (the shared not-running hint + ``1`` if the office is
    shut), mirroring the single ops. An empty defined set is a clean no-op
    (``no agents defined``, ``0``). Otherwise it is **best-effort**: a per-item
    failure is reported and the sweep continues to the next id, then a one-line
    summary closes it. Returns ``1`` if any id HARD-failed (a genuine fault — a 4xx
    reload-needed or a raised 5xx/transport error), else ``0``; the restart and
    duplicate-refuse outcomes are successes, not failures.

    The §3.5 duplicate guard reads the same broker-wide roster for EVERY id, so it
    is probed ONCE up front and threaded into each per-id ``agent_start`` (via its
    ``live`` param). A broker-down probe therefore yields ONE aggregate warning for
    the operator's single action — not one per id — and the count is reflected in
    the closing summary; the sweep then proceeds with an empty roster (the guard is
    best-effort, §3.5).

    ``client`` / ``probe`` are injected for testing; only the resolved client
    object/port is shared across the sweep — each id still issues its own
    workspace + roster reads through ``agent_start``. ``now`` is accepted for
    symmetry with the rest of the lifecycle surface and is unused.
    """
    # Drop reserved names BEFORE the empty-check: main.py passes the raw `.md`
    # stems from detect_agents, and a creatable `tools.md` / `router.md` / `mcp.md`
    # / `broker.md` / `bridge.md` is not rejected by the id pattern. Without this
    # filter the sweep would fall through to `start_process('tools')` against the
    # live singleton — breaking the "--all never touches another component type"
    # invariant. (agent_start's own guard would also refuse each one, but filtering
    # here keeps them out of the per-id sweep AND the summary count entirely.) An
    # all-reserved input collapses to the empty set → the clean no-op below.
    agent_ids = [n for n in agent_ids if n not in _NON_AGENT_PROCESSES]

    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    if not agent_ids:
        print("no agents defined")
        return 0

    # Probe the broker-wide roster ONCE for the whole sweep: the §3.5 guard reads
    # the same org-wide view for every id, so a per-id re-probe would be N
    # round-trips (and N broker-down warnings) for one operator action. The
    # resolved roster is threaded into each per-id ``agent_start`` via ``live``, so
    # none of them re-probe.
    probe = _resolve_probe(probe)
    try:
        live: list[AgentDefinition] = await probe(server_urls)
        probe_unavailable = False
    except Exception:
        # One aggregate broker-down warning for the whole action (not one per id),
        # then proceed with an empty roster — the guard is best-effort (§3.5) and a
        # same-host duplicate is impossible. The summary reflects that the org-wide
        # check was skipped fleet-wide so the operator gets a single signal.
        print(
            "warning: could not verify org-wide duplicates "
            "(broker unreachable); proceeding for all agents."
        )
        live = []
        probe_unavailable = True

    failures = 0
    for name in agent_ids:
        try:
            rc = await agent_start(
                home,
                name=name,
                server_urls=server_urls,
                client=client,
                probe=probe,
                live=live,
            )
        except Exception as exc:
            # Best-effort: a raised fault on one id (e.g. a 5xx that agent_start
            # re-raises loudly) must not abort the rest of the sweep. Surface it and
            # keep going; the non-zero summary tells the operator to look.
            print(f"agent {name}: failed to start ({exc})")
            failures += 1
            continue
        if rc != 0:
            failures += 1

    summary = f"start --all: {len(agent_ids)} agent(s) processed, {failures} failed."
    if probe_unavailable:
        # Surface the skipped fleet-wide guard in the closing summary so the single
        # aggregate signal carries through to the operator's last line of output.
        summary += " (org-wide duplicate check skipped: broker unreachable)"
    print(summary)
    return 1 if failures else 0


async def agent_stop_all(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
) -> int:
    """Take every RUNNING local agent offline (``stop --all``, behavior #1).

    LOCAL-only and read-from-the-supervisor: the target set is exactly this host's
    Running agent processes (:func:`_running_agent_names` — the same physical filter
    ``ps`` uses, so the substrate and the tools/router/mcp singletons are never
    swept). There is no over-the-wire control; ``--all`` acts on THIS host.

    Workspace check first (the shared not-running hint + ``1``). Nothing running
    locally is a clean no-op (``no agents running locally``, ``0``). Otherwise it is
    **best-effort**: each stop is attempted, a per-item failure is reported and the
    sweep continues, and a one-line summary closes it. Returns ``1`` if any stop
    failed, else ``0``.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    targets = sorted(await _running_agent_names(client))
    if not targets:
        print("no agents running locally")
        return 0

    failures = 0
    for name in targets:
        try:
            await client.stop_process(name)
        except Exception as exc:
            print(f"agent {name}: failed to stop ({exc})")
            failures += 1
            continue
        print(f"agent {name} stopped")

    print(f"stop --all: {len(targets)} agent(s) processed, {failures} failed.")
    return 1 if failures else 0


async def agent_restart_all(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
) -> int:
    """Reload every RUNNING local agent (``restart --all``, behavior #1).

    Same LOCAL-only target set as :func:`agent_stop_all` — this host's Running agent
    processes (:func:`_running_agent_names`), never the substrate or the singletons.
    Useful after a provider/key change that affects a whole host's agents at once.

    Workspace check first (the shared not-running hint + ``1``). Nothing running
    locally is a clean no-op (``no agents running locally``, ``0``). Otherwise
    **best-effort**: each restart is attempted, a per-item failure is reported and
    the sweep continues, and a one-line summary closes it. Returns ``1`` if any
    restart failed, else ``0``.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _workspace_is_up(client):
        print(_NOT_RUNNING_HINT)
        return 1

    targets = sorted(await _running_agent_names(client))
    if not targets:
        print("no agents running locally")
        return 0

    failures = 0
    for name in targets:
        try:
            await client.restart_process(name)
        except Exception as exc:
            print(f"agent {name}: failed to restart ({exc})")
            failures += 1
            continue
        print(f"agent {name} restarted")

    print(f"restart --all: {len(targets)} agent(s) processed, {failures} failed.")
    return 1 if failures else 0


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


async def _running_agent_names(client: ProcessComposeClient) -> set[str]:
    """This host's ``Running`` agent names — the local target/membership set.

    The single source of truth for "which agents are physically up here": the same
    physical filter the ps board uses (:func:`_running_roster_names` over
    ``list_processes``), so ``agent_start``'s already-running-here restart branch
    (behavior #2) and the ``stop_all``/``restart_all`` sweeps agree on exactly what
    counts as a locally-running agent (``Running`` status, not a reserved
    substrate/singleton process). Factored to one place so that definition cannot
    drift between the single-start guard and the bulk verbs.
    """
    return _running_roster_names(await client.list_processes())


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
