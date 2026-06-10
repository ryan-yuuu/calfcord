"""Substrate lifecycle orchestration: ``start`` / ``stop`` / ``status`` (§13.1-§13.3).

This is the imperative glue above the two pure-ish seams — :mod:`compose` (renders
the project) and :mod:`client` (the REST wire) — that brings the *office* up and
down. Everything that touches the world (the binary path, the process launcher,
the wall clock) is injected, so the whole flow is unit-testable with no real
``process-compose`` binary and no broker.

The hard-won contract from the Phase-0 spike (design §13) lives here:

* **Detached launch** is ``up -f <yaml> -D -t=false -p <port> -L <log>`` — never
  ``--no-server`` (that kills the REST API the readiness gate polls). The REST
  port is *derived from the home path* (:func:`pc_port_for`) so two installs on
  one host do not both grab the supervisor default :8080.
* **Priming reconcile for upstream bug #494** (§13.1): immediately after ``up``,
  issue exactly one no-op ``update_project`` with the byte-identical rendered
  YAML, so the buggy first project-update lands on a no-op instead of bouncing the
  substrate.
* **Readiness gate** (§12.6 / §13.3): ``up -D`` returning 0 does NOT mean healthy,
  so ``start`` polls the **bridge** to ``is_ready``/Running with a timeout and, on
  timeout, tears the substrate down and returns non-zero — a green light that
  lies is worse than a red one.
* **Lock + idempotency** (§12.4): an exclusive ``flock`` serializes start/stop so
  two concurrent starts cannot race two supervisors; ``start`` probes first and
  short-circuits if the office is already open, and ``stop`` is a no-op if nothing
  is running.

Import-light like the rest of this package.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path

from calfcord.health.check import BrokerProbe, default_broker_probe
from calfcord.supervisor._workspace import (
    iter_process_dicts,
    resolve_client,
    workspace_is_up,
)
from calfcord.supervisor.client import ProcessComposeClient
from calfcord.supervisor.compose import SUPERVISOR_LOG_STEM, render_compose

# A process launcher: hand it an argv and it starts the process. Production wires
# this to a detached ``subprocess.Popen`` (the ``up`` must outlive ``start``); a
# blocking variant is fine for the short-lived ``down``. Tests record the argv.
Spawn = Callable[[Sequence[str]], None]

# Monotonic seconds, for measuring the readiness-gate budget. Injected so a test
# clock can advance instantly in lockstep with ``sleep``.
Clock = Callable[[], float]

# The inter-poll wait. Injected so tests drive the poll loop with zero real time.
Sleep = Callable[[float], Awaitable[None]]

# Derived REST-port range (§12.4 multi-home): a documented high band that avoids
# the supervisor default :8080 and the broker's :9092, so a second $CALFCORD_HOME
# on the same host gets its own stable, non-colliding port. 800 ports is ample
# headroom against hash collisions for the handful of installs on one machine.
_PORT_RANGE_START = 8100
_PORT_RANGE_END = 8899
_PORT_RANGE_WIDTH = _PORT_RANGE_END - _PORT_RANGE_START + 1

# Readiness gate cadence (§13.2/§13.3): poll the bridge every few seconds until it
# is ready or the budget is spent. A modest default budget covers a cold broker
# provision + Discord connect without hanging the CLI forever.
_DEFAULT_READY_TIMEOUT_SECONDS = 90
_READINESS_POLL_INTERVAL_SECONDS = 2.0

# Bounded wait for the REST server itself to answer after a detached ``up`` (the
# socket binds a beat after the process forks). Separate from the readiness gate:
# this only proves the supervisor is talking, not that the bridge is healthy.
_SERVER_UP_TIMEOUT_SECONDS = 30
_SERVER_UP_POLL_INTERVAL_SECONDS = 0.5

# Bounded wait for a blocking ``down`` to complete (§13.2 shutdown grace is 10s;
# this caps the synchronous teardown a little above that so an orderly stop is
# never cut short, while a wedged supervisor still fails loudly instead of
# hanging the CLI forever).
_DOWN_TIMEOUT_SECONDS = 20

_LOCK_FILENAME = "calfcord-lifecycle.lock"
_COMPOSE_FILENAME = "process-compose.yaml"
# Derive from the single shared stem so the writer (this module's ``up -L``) and
# the reader (``calfcord logs``) can never drift apart (review #19).
_SUPERVISOR_LOG_FILENAME = f"{SUPERVISOR_LOG_STEM}.log"

# Substrate processes, for the status board's substrate-vs-roster split.
_SUBSTRATE = frozenset({"broker", "bridge"})


def resolve_pc_binary() -> str:
    """Locate the ``process-compose`` binary, or raise an actionable error.

    Precedence (design §12.4): an explicit ``$CALFCORD_PROCESS_COMPOSE_BIN`` (dev
    override / packaging) → the install's ``$CALFCORD_HOME/bin/process-compose``
    (the ``ensure_process_compose`` bootstrap target, mirroring ``ensure_tansu``)
    → a ``process-compose`` on ``PATH``. Each candidate must be an existing,
    executable file; a stale env var pointing at nothing falls through rather than
    masking a working PATH binary.
    """
    explicit = os.environ.get("CALFCORD_PROCESS_COMPOSE_BIN")
    if explicit and _is_executable_file(explicit):
        return explicit

    home = os.environ.get("CALFCORD_HOME")
    if home:
        candidate = os.path.join(home, "bin", "process-compose")
        if _is_executable_file(candidate):
            return candidate

    on_path = shutil.which("process-compose")
    if on_path:
        return on_path

    raise RuntimeError(
        "process-compose binary not found "
        "(checked $CALFCORD_PROCESS_COMPOSE_BIN, $CALFCORD_HOME/bin/process-compose, "
        "and PATH); re-run the calfcord installer to bootstrap it, or set "
        "$CALFCORD_PROCESS_COMPOSE_BIN to a process-compose v1.110.0 binary."
    )


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def pc_port_for(home: str | os.PathLike[str]) -> int:
    """A deterministic Process Compose REST port derived from the home path.

    Two ``$CALFCORD_HOME`` installs on one host must not both grab the supervisor
    default :8080 (§12.4). We hash the *absolute* home — so a relative invocation
    picks the same port as the absolute one — with a stable digest (NOT Python's
    per-process-salted ``hash()``) into a documented high band. Same home always
    yields the same port, across processes and reboots, so every REST call and the
    ``up -p`` flag agree.
    """
    absolute = os.path.abspath(os.fspath(home))
    digest = hashlib.sha256(absolute.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:8], "big") % _PORT_RANGE_WIDTH
    return _PORT_RANGE_START + offset


def _lock_path(home: str | os.PathLike[str]) -> str:
    return os.path.join(os.fspath(home), "state", _LOCK_FILENAME)


@contextlib.contextmanager
def lifecycle_lock(home: str | os.PathLike[str]):
    """Hold an exclusive ``flock`` over ``<home>/state/calfcord-lifecycle.lock``.

    Serializes ``start``/``stop`` so two concurrent invocations cannot race two
    supervisors against one home (§12.4). Uses ``LOCK_EX | LOCK_NB`` so a second
    holder fails *immediately* with a clear error instead of blocking
    indefinitely. The parent ``state/`` dir is created on demand; the lock file
    itself is kept (its presence is harmless — the advisory lock, not the file,
    is the guard) and the fd is always closed on exit, releasing the lock.
    """
    path = _lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(
                f"another calfcord start/stop is in progress for {home} "
                f"(could not acquire {path})"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# A workspace-readiness alias kept for the internal call sites here; the body is
# the one shared :func:`_workspace.workspace_is_up` (Fix #14 consolidation).
_supervisor_is_up = workspace_is_up


def _home_marker(home: str) -> str:
    """The home-specific path the rendered project embeds in every process.

    Each declared process's ``log_location`` is ``<home>/state/logs/<name>.log``
    (see :func:`compose._log_location`), and that absolute path opens every
    quoted string value in which it appears — so ``<home>/state`` is a prefix of
    one of the answering supervisor's quoted config paths iff that supervisor was
    launched for THIS home (see :func:`_supervisor_belongs_to_home`).
    """
    return os.path.join(home, "state")


async def _supervisor_belongs_to_home(
    client: ProcessComposeClient, home: str
) -> bool | None:
    """Whether the answering supervisor was launched for ``home`` (Fix #11).

    :func:`pc_port_for` maps two homes into an 800-port band, so a collision is
    possible: two installs can hash to one REST port. The bare ``project_state``
    idempotency probe only proves *something* answers that port, not *whose*
    supervisor it is — so before trusting an "already up" verdict we read back a
    declared process's config (which embeds the home-specific log path) and check
    for this home's marker.

    Returns ``True`` (this home's), ``False`` (a DIFFERENT home colliding on the
    port — the caller must fail loudly, never a false "already open"), or ``None``
    when it cannot be determined (the info route is unavailable), in which case the
    caller keeps the prior best-effort idempotent behavior.
    """
    try:
        # ``bridge`` is always a declared substrate process, so its config is the
        # stable place to read the home-specific log path back from.
        info = await client.get_process_info("bridge")
    except RuntimeError:
        return None
    if not info:
        return None
    # Robust to whatever JSON key Process Compose uses for the log path (the API
    # shape is version-fragile, §12.4 Risk #2): scan the whole serialized config
    # rather than pinning a single field name. Match the marker only where it
    # OPENS a quoted path value (repr quotes string values, and the absolute
    # home path starts the log_location/working_dir it appears in) so a home
    # whose path is a *suffix* of another's — e.g. "/calf" vs "/data/calf" —
    # cannot false-positive on a bare substring scan and silently adopt the
    # other install's colliding supervisor.
    marker = _home_marker(home)
    serialized = repr(info)
    return f"'{marker}" in serialized or f'"{marker}' in serialized


def _bridge_is_ready(state: object) -> bool:
    """Whether a ``get_process('bridge')`` state object reports its probe passed.

    The bridge declares a readiness probe (§13.2), so its *health* is the probe
    verdict, not mere liveness: Process Compose v1.110.0 sets ``is_ready: "Ready"``
    only once that exec probe passes. We gate STRICTLY on it — ``status: "Running"``
    while ``is_ready`` is anything other than ``"Ready"`` is exactly the
    green-light-that-lies the readiness gate exists to reject (§12.6/§13.3), so a
    Running-but-not-yet-ready bridge must NOT read healthy.
    """
    return isinstance(state, dict) and state.get("is_ready") == "Ready"


async def _await_supervisor_up(
    client: ProcessComposeClient, *, clock: Clock, sleep: Sleep
) -> bool:
    """Poll the REST server until it answers, bounded by the server-up timeout."""
    deadline = clock() + _SERVER_UP_TIMEOUT_SECONDS
    while True:
        if await _supervisor_is_up(client):
            return True
        if clock() >= deadline:
            return False
        await sleep(_SERVER_UP_POLL_INTERVAL_SECONDS)


async def _await_bridge_ready(
    client: ProcessComposeClient,
    *,
    timeout_s: float,
    clock: Clock,
    sleep: Sleep,
) -> bool:
    """Poll the bridge until it is ready, bounded by ``timeout_s`` (§13.3).

    A transport error mid-poll (the supervisor restarting the bridge under
    ``restart: always``) is treated as "not ready yet", not a fatal error, so a
    transient bounce does not abort the gate before the budget is spent.
    """
    deadline = clock() + timeout_s
    while True:
        try:
            state = await client.get_process("bridge")
        except RuntimeError:
            state = None
        if _bridge_is_ready(state):
            return True
        if clock() >= deadline:
            return False
        await sleep(_READINESS_POLL_INTERVAL_SECONDS)


def _default_spawn(argv: Sequence[str]) -> None:
    """Launch a detached child that outlives this process (the production spawn).

    ``start_new_session=True`` puts the child in its own session so the supervisor
    keeps running after the CLI exits (and is not felled by a Ctrl-C delivered to
    the CLI's terminal group). stdout/stderr are discarded — the supervisor writes
    its own ``-L`` log file — so no pipe fills up and wedges the child.
    """
    import subprocess

    # argv is built from a pinned binary path + literal flags (no shell, no user
    # string interpolation), so the child launch is safe.
    subprocess.Popen(
        list(argv),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _default_spawn_blocking(argv: Sequence[str]) -> None:
    """Run ``argv`` to completion, bounded by a timeout (the blocking spawn).

    Unlike :func:`_default_spawn` (which detaches a child that must outlive the
    CLI), this is for the short-lived ``down`` teardown: ``stop`` and the start
    readiness-timeout teardown must wait for the supervisor to actually stop
    before returning, so a later ``start`` cannot collide with a supervisor that
    is still shutting down. stdout/stderr are discarded (the supervisor logs to
    its ``-L`` file); a bounded ``timeout`` turns a wedged ``down`` into a loud
    failure rather than an indefinite hang.
    """
    import subprocess

    # argv is a pinned binary path + literal flags (no shell, no user string
    # interpolation), so the launch is safe.
    subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_DOWN_TIMEOUT_SECONDS,
        check=False,
    )


# A per-home client resolver alias for the internal call sites; the body is the
# one shared :func:`_workspace.resolve_client` (Fix #14 consolidation).
_resolve_client = resolve_client


async def start(
    home: str | os.PathLike[str],
    *,
    server_urls: str,
    launcher: str,
    agent_ids: Iterable[str],
    mcp_servers: Iterable[str] = (),
    ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_SECONDS,
    client: ProcessComposeClient | None = None,
    spawn: Spawn | None = None,
    spawn_blocking: Spawn | None = None,
    clock: Clock | None = None,
    sleep: Sleep | None = None,
    broker_probe: BrokerProbe | None = None,
) -> int:
    """Open the workspace: render, launch detached, prime, gate on readiness.

    Returns a POSIX exit code: ``0`` once the substrate (broker + bridge) is up
    and the bridge is ready; non-zero if the broker precondition fails fast, or
    (after tearing the substrate back down) if the bridge does not become ready
    within ``ready_timeout_s`` — never a green light that lies (§12.6).

    The broker is a **fast-fail precondition** (§13.2): before rendering or
    launching anything, ``start`` probes the broker via ``broker_probe`` (default
    derived from ``server_urls``); a down broker returns non-zero immediately with
    an actionable hint instead of burning the full bridge-readiness budget waiting
    for a bridge that can never connect.

    ``client`` / ``spawn`` / ``spawn_blocking`` / ``clock`` / ``sleep`` /
    ``broker_probe`` are injected for testing; in production they default to a
    per-home REST client, a detached subprocess spawner (``up`` must outlive the
    CLI), a blocking spawner (for the synchronous ``down`` teardown),
    ``time.monotonic``, ``asyncio.sleep``, and the real broker metadata probe.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)
    spawn = spawn or _default_spawn
    spawn_blocking = spawn_blocking or _default_spawn_blocking
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep

    with lifecycle_lock(home):
        # Idempotency (§12.4): if the office is already open, do NOT launch a
        # second supervisor — just confirm and point at the next step. But the
        # port can collide across homes (Fix #11): verify the answering supervisor
        # is THIS home's before trusting "already open". A DIFFERENT home colliding
        # on the port must fail loudly, never a false "already open" that silently
        # skips install B's own launch.
        if await _supervisor_is_up(client):
            belongs = await _supervisor_belongs_to_home(client, home)
            if belongs is False:
                print(
                    f"error: another calfcord install is already using REST port "
                    f"{pc_port_for(home)} on this host (a port collision between "
                    "two $CALFCORD_HOME installs). Stop the other install, or run "
                    "this one under a different $CALFCORD_HOME, then re-run "
                    "`calfcord start`."
                )
                return 1
            print(
                "workspace already open (broker + bridge). "
                "Next: calfcord agent start <name>"
            )
            return 0

        # Broker fast-fail precondition (§13.2): the bridge cannot reach Ready
        # without a live broker, so probe it BEFORE rendering/launching. A down
        # broker fails here in a heartbeat instead of after the full bridge
        # readiness timeout — and we leave the workspace untouched (no `up`).
        probe = broker_probe or default_broker_probe(server_urls)
        if not await probe():
            print(
                f"error: broker not reachable at {server_urls}; "
                "start it with `calfcord broker`, then re-run `calfcord start`."
            )
            return 1

        port = pc_port_for(home)
        yaml_text = render_compose(
            agent_ids=list(agent_ids),
            home=home,
            launcher=launcher,
            mcp_servers=list(mcp_servers),
        )
        yaml_path = _write_compose(home, yaml_text)
        log_path = _ensure_log_path(home)
        binary = resolve_pc_binary()

        # Detached launch — §13.2 flags exactly; NEVER --no-server.
        spawn(
            [
                binary,
                "up",
                "-f",
                yaml_path,
                "-D",
                "-t=false",
                "-p",
                str(port),
                "-L",
                log_path,
            ]
        )

        if not await _await_supervisor_up(client, clock=clock, sleep=sleep):
            print(
                "error: process-compose REST server did not come up "
                f"within {_SERVER_UP_TIMEOUT_SECONDS}s; "
                f"check {log_path}"
            )
            return 1

        # Priming reconcile for upstream #494 (§13.1): exactly one no-op
        # project-update with the byte-identical YAML, so the buggy first update
        # lands on a no-op instead of bouncing the substrate. This runs AFTER the
        # detached supervisor is already up, so a raise here (a PC reconcile error
        # / transport failure) must NOT be left bare: an unhandled exception would
        # orphan the supervisor and dump a traceback — and since `start` is the
        # wizard's start_fn, it would crash `calfcord init`. Fail like the
        # readiness-gate path below: tear the substrate back down via the BLOCKING
        # seam (a racy detached `down` could let a retried `start` collide with a
        # supervisor still stopping), report actionably, and return non-zero.
        try:
            await client.update_project(yaml_text)
        except RuntimeError:
            with contextlib.suppress(Exception):
                spawn_blocking([binary, "down", "-p", str(port)])
            print(
                "error: workspace failed to prime; tore it down. "
                f"See {log_path} or run: calfcord doctor"
            )
            return 1

        if not await _await_bridge_ready(
            client, timeout_s=ready_timeout_s, clock=clock, sleep=sleep
        ):
            # No green light that lies (§12.6): tear the substrate down and report
            # the specific failure + the likeliest cause. Use the BLOCKING seam so
            # the supervisor is actually stopped before we return — a fire-and-
            # forget detached `down` could let a retried `start` collide with a
            # supervisor still shutting down (§13.3).
            with contextlib.suppress(Exception):
                spawn_blocking([binary, "down", "-p", str(port)])
            print(
                "error: bridge did not become ready within "
                f"{ready_timeout_s:g}s; tore down the workspace. "
                "Likely the broker could not be reached or Discord privileged "
                f"intents are off. See {log_path} or run: calfcord doctor"
            )
            return 1

    print(
        "workspace open (broker + bridge). No agents running yet "
        "-> calfcord agent start <name>"
    )
    return 0


async def stop(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
    spawn_blocking: Spawn | None = None,
) -> int:
    """Close the workspace; idempotent — a no-op if nothing is running (§12.4).

    ``down`` is issued through the **blocking** seam so ``stop`` returns (and
    prints "workspace closed") only after the supervisor has actually stopped — a
    fire-and-forget detached ``down`` would let a racing ``start`` collide with a
    supervisor still shutting down (§13.3).
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)
    spawn_blocking = spawn_blocking or _default_spawn_blocking

    with lifecycle_lock(home):
        if not await _supervisor_is_up(client):
            print("nothing to stop (workspace not running).")
            return 0

        binary = resolve_pc_binary()
        spawn_blocking([binary, "down", "-p", str(pc_port_for(home))])

    print("workspace closed.")
    return 0


async def status(
    home: str | os.PathLike[str],
    *,
    client: ProcessComposeClient | None = None,
    clock: Clock | None = None,
) -> int:
    """Render a glanceable org board, or a "not running" hint (§12.6).

    ``clock`` is accepted for symmetry with ``start`` (and future freshness
    reconciliation against heartbeats); it is unused today but keeps the seam
    stable so a caller need not special-case ``status``.
    """
    home = os.fspath(home)
    client = _resolve_client(client, home)

    if not await _supervisor_is_up(client):
        print("workspace not running (start it with: calfcord start)")
        return 0

    processes = _process_rows(await client.list_processes())
    substrate = [p for p in processes if p["name"] in _SUBSTRATE]
    roster = [p for p in processes if p["name"] not in _SUBSTRATE]

    print("workspace is open.")
    print("substrate:")
    for row in substrate:
        print(_format_row(row))
    print("roster:")
    if roster:
        for row in roster:
            print(_format_row(row))
    else:
        print("  (none running)")
    # Reboot non-survival, stated honestly (§12.6): the daemon is session-scoped.
    print("note: the workspace does not survive a reboot; re-run `calfcord start`.")
    return 0


def _process_rows(payload: object) -> list[dict]:
    """Normalize ``list_processes()`` into row dicts (name/status/is_ready).

    The wire-shape tolerance (bare list vs ``{"data": [...]}``, skip non-dicts) is
    the one shared :func:`_workspace.iter_process_dicts` (Fix #14); this only
    projects the board's three columns onto each entry.
    """
    return [
        {
            "name": item.get("name", "?"),
            "status": item.get("status", "?"),
            "is_ready": item.get("is_ready", "-"),
        }
        for item in iter_process_dicts(payload)
    ]


def _format_row(row: dict) -> str:
    return f"  {row['name']:<16} {row['status']:<10} ready={row['is_ready']}"


def _write_compose(home: str, yaml_text: str) -> str:
    path = os.path.join(home, "state", _COMPOSE_FILENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(yaml_text, encoding="utf-8")
    return path


def _ensure_log_path(home: str) -> str:
    logs_dir = os.path.join(home, "state", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, _SUPERVISOR_LOG_FILENAME)
