"""``calfcord doctor`` — a non-interactive preflight for an install.

Answers "will the four processes actually boot?" before the operator starts them, instead of
letting a missing token / unreachable broker / missing app id / unparseable agent surface only as a
crash. It is
deliberately read-only and scriptable: each check yields a :class:`Result`, the whole set renders
once, and the exit code is the contract (``1`` iff any check ``fail``s; warnings never fail).

It evaluates the *effective* configuration the runners will see — ``os.environ`` (which the shim
populates from ``config/.env`` via ``uv run --env-file``, with shell exports winning) — not the
``.env`` file's literal contents, so a shell-exported override isn't silently missed. The file is
consulted only to answer "is there a config file at all".

The bot token is a secret: it is sent only in the ``Authorization`` header and NEVER printed — not
in a detail line, a summary, or an error. The underlying httpx exception text is never echoed; only
a fixed message or the bare HTTP status code is shown, so the token can't leak through an error path.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, get_args

from calfcord.cli._envfile import read_env

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import httpx

    from calfcord.agents.definition import AgentDefinition
    from calfcord.health.heartbeat import Heartbeat
    from calfcord.supervisor.client import ProcessComposeClient

    # A live-roster probe: hand it ``server_urls`` and it returns the
    # AgentDefinitions of every agent answering the control-plane discovery ping
    # across the org. Injected so tests script the deep probe without a real broker
    # (production adapts :func:`calfcord.control_plane.probe.probe_live_roster`).
    ProbeFn = Callable[[str], Awaitable[list[AgentDefinition]]]
    # A heartbeat reader, injected so the daemon-liveness check needs no real beat
    # file (production is :func:`calfcord.health.heartbeat.read_beat`).
    ReadBeatFn = Callable[[Path, str], Heartbeat | None]

_DISCORD_ME_URL = "https://discord.com/api/v10/users/@me"
_TCP_TIMEOUT = 2.0
_HTTP_TIMEOUT = 5.0

Status = Literal["ok", "warn", "fail"]
_SYMBOLS: dict[Status, str] = {"ok": "✓", "warn": "⚠", "fail": "✗"}
# A typo'd status would silently miscount the exit code, so pin the render map to the
# status domain at import (mirrors the THINKING_EFFORTS drift assert in _fields.py).
assert set(_SYMBOLS) == set(get_args(Status)), "_SYMBOLS drifted from Status"


@dataclass(frozen=True)
class Result:
    """One preflight check's outcome — a :data:`Status` plus a human-readable detail line."""

    name: str
    status: Status
    detail: str


def _parse_broker(url: str) -> tuple[str, int] | None:
    """Parse a ``CALF_HOST_URL`` value into ``(host, port)``, or ``None`` if unusable.

    Mirrors what the runners tolerate (the value is passed verbatim to the Kafka client): a bare
    host (port defaults to 9092), a ``host:port``, the first endpoint of a comma-separated list, an
    optional ``scheme://`` prefix, and bracketed IPv6. Never raises — a malformed value returns
    ``None`` so the caller reports a clean ``fail`` rather than a traceback.
    """
    endpoint = url.strip().split(",", 1)[0].strip()
    if not endpoint:
        return None
    if "://" in endpoint:
        endpoint = endpoint.split("://", 1)[1]

    if endpoint.startswith("["):  # bracketed IPv6: [host] or [host]:port
        host, _, rest = endpoint[1:].partition("]")
        port_str = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port_str = endpoint.rpartition(":")
        if not sep:  # no colon at all -> bare host
            host, port_str = endpoint, ""

    if not host:
        return None
    if not port_str:
        return (host, 9092)
    try:
        port = int(port_str)
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    return (host, port)


def _tcp_reachable(host: str, port: int, timeout: float = _TCP_TIMEOUT) -> bool:
    """TCP reachability probe — module-level so tests can monkeypatch it; closes the socket, never raises."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _discord_username(token: str, *, client_factory: Callable[[], httpx.Client] | None) -> str:
    """GET ``/users/@me`` and return the bot username. The token rides ONLY in the header.

    Raises the underlying httpx error (``HTTPStatusError`` for non-2xx, other ``HTTPError`` for
    transport failures); the caller classifies it. httpx is imported lazily so importing ``doctor``
    itself stays cheap and the offline path never imports it.
    """
    import httpx

    factory = client_factory or (lambda: httpx.Client(timeout=_HTTP_TIMEOUT))
    with factory() as client:
        resp = client.get(_DISCORD_ME_URL, headers={"Authorization": f"Bot {token}"})
    resp.raise_for_status()
    return resp.json().get("username", "?")


def _check_config(env_path: Path) -> Result:
    if not env_path.is_file():
        return Result("config", "fail", f"no config at {env_path} — run `calfcord init`")
    try:
        values = read_env(env_path)
    except (OSError, ValueError) as exc:  # unreadable / non-UTF-8 / malformed — don't let it crash doctor
        return Result("config", "fail", f"config at {env_path} is unreadable: {exc}")
    if not values:
        return Result("config", "warn", f"{env_path} has no values yet — fill it in (or run `calfcord init`)")
    return Result("config", "ok", str(env_path))


def _check_broker() -> Result:
    url = os.environ.get("CALF_HOST_URL", "").strip()
    if not url:
        return Result("broker", "warn", "CALF_HOST_URL not set (processes won't start until it is)")
    parsed = _parse_broker(url)
    if parsed is None:
        return Result("broker", "fail", f"CALF_HOST_URL is set but unparseable: {url!r}")
    host, port = parsed
    if _tcp_reachable(host, port):
        return Result("broker", "ok", f"reachable at {host}:{port}")
    return Result("broker", "fail", f"set but unreachable at {host}:{port}")


def _check_token(*, offline: bool, client_factory: Callable[[], httpx.Client] | None) -> Result:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return Result("discord token", "fail", "DISCORD_BOT_TOKEN not set")
    if offline:
        return Result("discord token", "ok", "set (not validated, --offline)")

    import httpx  # imported here so the offline / missing-token paths stay network- and import-free

    try:
        username = _discord_username(token, client_factory=client_factory)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):  # token not accepted -> the runner won't boot, so this is a hard fail
            return Result("discord token", "fail", f"token rejected by Discord ({code})")
        if code == 429:
            return Result("discord token", "warn", "Discord rate-limited the check; try again shortly")
        return Result("discord token", "warn", f"unexpected response from Discord ({code})")
    except (ValueError, AttributeError):  # a 200 with a non-JSON / non-dict body (edge proxy, interstitial)
        return Result("discord token", "warn", "reached Discord but couldn't read the response")
    except (httpx.HTTPError, OSError):
        return Result("discord token", "warn", "couldn't reach Discord to validate the token")
    return Result("discord token", "ok", f"valid (bot: {username})")


def _check_appid() -> Result:
    appid = os.environ.get("DISCORD_APPLICATION_ID", "").strip()
    if not appid:
        return Result("discord app id", "fail", "DISCORD_APPLICATION_ID not set (required)")
    if not appid.isdigit():
        return Result("discord app id", "fail", f"DISCORD_APPLICATION_ID is not numeric: {appid!r}")
    return Result("discord app id", "ok", appid)


def _check_agents(agents_dir: Path) -> Result:
    # Imported in-body so importing ``doctor`` stays cheap; agent_inspect transitively pulls heavier deps.
    from calfcord.cli.agent_inspect import _parse_all

    parsed, failed = _parse_all(agents_dir)
    if failed:
        return Result("agents", "fail", f"{len(failed)} failed to parse: {', '.join(failed)}")
    if not parsed:
        return Result("agents", "warn", f"no agents found in {agents_dir}")
    return Result("agents", "ok", f"{len(parsed)} agent(s) parse")


# --------------------------------------------------------------------- runtime checks
#
# The five checks above are STATIC — they answer "will the processes boot?" from
# config alone, with no running daemon. When the workspace IS open (the substrate
# started detached via ``calfcord start``) doctor adds a RUNTIME section that proves
# the live org actually functions end-to-end — the "green light that lies" the
# design (§12.1) exists to catch: a fresh-but-silent bridge, a broker that no agent
# can reach, a process up locally that never joined the org. The section is the only
# place that can see those, so it carries its own checks (§4.4 / §13.3).
#
# It runs ONLY when the daemon is up, detected via the bridge heartbeat (§12.1: the
# bridge beat — written on Discord ``on_ready`` — is the authoritative "daemon up"
# signal; broker TCP is a fast-fail precondition, not the liveness signal). A closed
# workspace is a valid read-only state, never a failure: doctor says so and stops.

# The bridge heartbeat names the daemon; its presence gates the whole runtime section.
_DAEMON_COMPONENT = "bridge"


def _check_daemon_alive(beat: Heartbeat, *, now: datetime) -> Result:
    """Whether the bridge heartbeat is fresh — a live daemon, not a zombie (§12.1).

    A present-but-stale beat is a wedged/killed bridge whose timer stopped: a hard
    ``fail`` framed as a fix (reopen the workspace), because a stale beat is exactly
    the green-light-that-lies. ``now`` is injected so freshness is deterministic.
    """
    # In-body import keeps importing ``doctor`` free of the health package's deps.
    from calfcord.health.heartbeat import is_fresh

    if is_fresh(beat, now=now):
        who = f" (bot: {beat.identity})" if beat.identity else ""
        return Result("daemon", "ok", f"bridge alive{who}")
    return Result(
        "daemon",
        "fail",
        "bridge heartbeat is stale (wedged/zombie) — restart: `calfcord stop && calfcord start`",
    )


def _check_deep_probe(roster: list[AgentDefinition]) -> Result:
    """Whether the control-plane deep probe proves the org answers (§4.4 / §12.1).

    A non-empty roster proves broker + bridge + agents function together end-to-end
    (the ping↔response loop only completes if the broker serves and agents are
    live), and names the registered agents. An EMPTY roster is the "green but no
    replies" symptom — the daemon is up but no agent is online — surfaced as a
    ``warn`` with the fix (bring one online), never a fail (an empty roster is a
    valid, recoverable state).
    """
    if roster:
        names = ", ".join(sorted(defn.agent_id for defn in roster))
        return Result("deep probe", "ok", f"{len(roster)} registered agent(s): {names}")
    return Result(
        "deep probe",
        "warn",
        "bridge answers but no agents are registered — bring one online: `calfcord agent start <name>`",
    )


def _check_drift(*, running_local: set[str], registered: set[str]) -> Result:
    """Whether agents running locally match those registered org-wide (§4.4 / §3.4).

    Drift = a process Process Compose reports ``Running`` on THIS host that does not
    answer the discovery probe: up but never joined the org, or wedged. Surfaced as
    a ``warn`` naming the drifting agents with the fix (restart them), never a fail
    — drift is a recoverable operational state, and the inverse (registered but not
    local) is expected multi-host and is NOT drift.
    """
    drifted = sorted(running_local - registered)
    if not drifted:
        return Result("drift", "ok", "running agents match the registered roster")
    return Result(
        "drift",
        "warn",
        "process up but not registered: "
        + ", ".join(drifted)
        + " — restart: `calfcord agent restart <name>`",
    )


async def _gather_runtime(
    *,
    home: Path,
    server_urls: str,
    now: datetime,
    read_beat_fn: ReadBeatFn,
    probe_fn: ProbeFn,
    pc_client: ProcessComposeClient,
) -> list[Result]:
    """Build the runtime-section results, or ``[]`` when the daemon is down.

    Daemon-down (no bridge beat) is a valid read-only state, so it yields no runtime
    results — :func:`run` prints the closed-workspace hint instead. When the daemon
    IS up the deep probe and drift checks degrade to a ``warn`` (never a crash, never
    a fail) if the broker is unreachable for the probe — the bridge being alive is
    already established by the heartbeat, so a probe miss is a soft signal.
    """
    beat = read_beat_fn(home, _DAEMON_COMPONENT)
    if beat is None:
        return []  # workspace closed — handled by the caller, not a runtime finding.

    results = [_check_daemon_alive(beat, now=now)]

    # The deep probe (and the drift check that consumes its result) talks to the
    # broker; a stale daemon means the bridge timer stopped, so probing further
    # would only add noise to an already-decided "restart the workspace" verdict.
    if results[0].status == "fail":
        return results

    try:
        roster = await probe_fn(server_urls)
    except Exception:
        # The bridge is alive (beat is fresh) but the deep probe could not reach the
        # broker. Degrade to a warn — never crash a read-only doctor, never fail the
        # run on a soft signal — and skip drift (no registered set to compare).
        results.append(
            Result(
                "deep probe",
                "warn",
                "couldn't reach the broker to probe the live roster; try again shortly",
            )
        )
        return results

    results.append(_check_deep_probe(roster))

    # Drift reuses the roster module's physical-view extractor (DRY): the same
    # wire-shape tolerance and reserved-name filtering ``agent ps`` uses.
    from calfcord.supervisor.roster import _running_roster_names

    running_local = _running_roster_names(await pc_client.list_processes())
    registered = {defn.agent_id for defn in roster}
    results.append(_check_drift(running_local=running_local, registered=registered))
    return results


def run(
    *,
    env_path: Path,
    agents_dir: Path,
    offline: bool = False,
    client_factory: Callable[[], httpx.Client] | None = None,
    home: Path | None = None,
    server_urls: str | None = None,
    now: datetime | None = None,
    read_beat_fn: ReadBeatFn | None = None,
    probe_fn: ProbeFn | None = None,
    pc_client: ProcessComposeClient | None = None,
) -> int:
    """Run every preflight check, print the report, and return the exit code (1 iff any check fails).

    The five STATIC checks (config / broker / token / app id / agents) always run.
    When ``home`` is supplied (a native install) doctor additionally runs the
    RUNTIME section — daemon liveness, a deep control-plane probe, and local↔org
    drift — but only if the daemon is actually up (a fresh bridge heartbeat exists);
    a closed workspace prints a next-step hint and adds no findings. doctor stays
    **read-only**: the runtime seams (``read_beat_fn`` / ``probe_fn`` / ``pc_client``
    / ``now``) are injected so tests need no real broker, supervisor, or beat file,
    and default in production to the heartbeat reader, the control-plane probe, and a
    per-home Process Compose client.
    """
    results = [
        _check_config(env_path),
        _check_broker(),
        _check_token(offline=offline, client_factory=client_factory),
        _check_appid(),
        _check_agents(agents_dir),
    ]
    runtime_results, daemon_up = _runtime_section(
        home=home,
        server_urls=server_urls,
        now=now,
        read_beat_fn=read_beat_fn,
        probe_fn=probe_fn,
        pc_client=pc_client,
    )

    all_results = results + runtime_results
    width = max(len(r.name) for r in all_results)
    for r in results:
        print(f"{_SYMBOLS[r.status]} {r.name:<{width}}  {r.detail}")
    if runtime_results:
        print("\nruntime (workspace is open):")
        for r in runtime_results:
            print(f"{_SYMBOLS[r.status]} {r.name:<{width}}  {r.detail}")
    elif home is not None and not daemon_up:
        # The install has a home but the workspace is closed: not a failure, but
        # always name the next step so a returning user is never stranded (§12.6).
        print("\nworkspace not running — open it with: `calfcord start`")

    failures = sum(1 for r in all_results if r.status == "fail")
    warnings = sum(1 for r in all_results if r.status == "warn")
    print()
    if failures:
        print(f"{failures} problem(s) found — fix the ✗ items above before starting calfcord.")
        return 1
    if warnings:
        print(f"ready, with {warnings} warning(s) — review the ⚠ items above.")
        return 0
    print("all checks passed — you're ready to start calfcord.")
    return 0


def _runtime_section(
    *,
    home: Path | None,
    server_urls: str | None,
    now: datetime | None,
    read_beat_fn: ReadBeatFn | None,
    probe_fn: ProbeFn | None,
    pc_client: ProcessComposeClient | None,
) -> tuple[list[Result], bool]:
    """Resolve the runtime seams and gather the section; ``([], False)`` when N/A.

    Returns ``(results, daemon_up)``. The runtime section is skipped entirely (no
    results, ``daemon_up=False``) when ``home`` is ``None`` — a dev invocation has
    no install heartbeats to read. Otherwise the seams default to production
    implementations and :func:`_gather_runtime` decides daemon-up from the bridge
    heartbeat; a non-empty result means the daemon is up.
    """
    if home is None:
        return [], False

    if now is None:
        now = datetime.now(UTC)
    if server_urls is None:
        server_urls = os.environ.get("CALF_HOST_URL", "").strip() or "localhost"
    if read_beat_fn is None:
        from calfcord.health.heartbeat import read_beat

        read_beat_fn = read_beat
    if probe_fn is None:
        from calfcord.control_plane.probe import probe_live_roster

        async def probe_fn(urls: str) -> list[AgentDefinition]:  # type: ignore[misc]
            return await probe_live_roster(urls)

    if pc_client is None:
        from calfcord.supervisor.client import ProcessComposeClient
        from calfcord.supervisor.lifecycle import pc_port_for

        pc_client = ProcessComposeClient(port=pc_port_for(home))

    results = asyncio.run(
        _gather_runtime(
            home=home,
            server_urls=server_urls,
            now=now,
            read_beat_fn=read_beat_fn,
            probe_fn=probe_fn,
            pc_client=pc_client,
        )
    )
    return results, bool(results)
