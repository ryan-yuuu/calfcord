"""``calfcord doctor`` — a non-interactive preflight for an install.

Answers "will the four processes actually boot?" before the operator starts them, instead of
letting a missing token / unreachable broker / unparseable agent surface only as a crash. It is
deliberately read-only and scriptable: each check yields a :class:`Result`, the whole set renders
once, and the exit code is the contract (``1`` iff any check ``fail``s; warnings never fail).

It evaluates the *effective* configuration the runners will see — ``os.environ`` (which the shim
populates from ``config/.env`` via ``uv run --env-file``, with shell exports winning) — not the
``.env`` file's literal contents, so a shell-exported override isn't silently missed. The file is
consulted only to answer "is there a config file at all".

The bot token is a secret: it is sent only in the ``Authorization`` header and NEVER printed — not
in a detail line, a summary, or an error (raw httpx exceptions are swallowed, not echoed).
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

from calfcord.cli._envfile import read_env

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

_DISCORD_ME_URL = "https://discord.com/api/v10/users/@me"
_TCP_TIMEOUT = 2.0
_HTTP_TIMEOUT = 5.0
_SYMBOLS = {"ok": "✓", "warn": "⚠", "fail": "✗"}


@dataclass(frozen=True)
class Result:
    """One preflight check's outcome. ``status`` is ``"ok"`` | ``"warn"`` | ``"fail"``."""

    name: str
    status: str
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
        return (host, int(port_str))
    except ValueError:
        return None


def _tcp_reachable(host: str, port: int, timeout: float = _TCP_TIMEOUT) -> bool:
    """True if a TCP connection to ``host:port`` opens within ``timeout``. Module-level so tests can patch it."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _discord_username(token: str, *, client_factory: Callable[[], httpx.Client] | None) -> str:
    """GET ``/users/@me`` and return the bot username. The token rides ONLY in the header.

    Raises the underlying httpx error (``HTTPStatusError`` for non-2xx, other ``HTTPError`` for
    transport failures); the caller classifies it. httpx is imported lazily so the CLI entry point
    stays light.
    """
    import httpx

    factory = client_factory or (lambda: httpx.Client(timeout=_HTTP_TIMEOUT))
    with factory() as client:
        resp = client.get(_DISCORD_ME_URL, headers={"Authorization": f"Bot {token}"})
    resp.raise_for_status()
    return resp.json().get("username", "?")


def _check_config(env_path: Path) -> Result:
    if env_path.is_file() and read_env(env_path):
        return Result("config", "ok", str(env_path))
    return Result("config", "fail", f"no config at {env_path} — run `calfcord init`")


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
    import httpx

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return Result("discord token", "fail", "DISCORD_BOT_TOKEN not set")
    if offline:
        return Result("discord token", "ok", "set (not validated, --offline)")
    try:
        username = _discord_username(token, client_factory=client_factory)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return Result("discord token", "fail", "token rejected by Discord (401)")
        return Result("discord token", "warn", f"unexpected response from Discord ({exc.response.status_code})")
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
    # Imported in-body to keep the CLI entry point light (agent_inspect transitively pulls heavier deps).
    from calfcord.cli.agent_inspect import _parse_all

    parsed, failed = _parse_all(agents_dir)
    if failed:
        return Result("agents", "fail", f"{len(failed)} failed to parse: {', '.join(failed)}")
    if not parsed:
        return Result("agents", "warn", f"no agents found in {agents_dir}")
    return Result("agents", "ok", f"{len(parsed)} agent(s) parse")


def run(
    *,
    env_path: Path,
    agents_dir: Path,
    offline: bool = False,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> int:
    """Run every preflight check, print the report, and return the exit code (1 iff any check fails)."""
    results = [
        _check_config(env_path),
        _check_broker(),
        _check_token(offline=offline, client_factory=client_factory),
        _check_appid(),
        _check_agents(agents_dir),
    ]
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{_SYMBOLS[r.status]} {r.name:<{width}}  {r.detail}")

    failures = sum(1 for r in results if r.status == "fail")
    warnings = sum(1 for r in results if r.status == "warn")
    print()
    if failures:
        print(f"{failures} problem(s) found — fix the ✗ items above before starting calfcord.")
        return 1
    if warnings:
        print(f"ready, with {warnings} warning(s) — review the ⚠ items above.")
        return 0
    print("all checks passed — you're ready to start calfcord.")
    return 0
