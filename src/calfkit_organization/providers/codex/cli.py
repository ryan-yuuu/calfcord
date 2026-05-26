"""``calfkit-auth`` command-line interface.

Today this only ships the ``codex`` subcommand for OpenAI ChatGPT
subscription login; the top-level ``calfkit-auth`` namespace leaves room
for future auth providers (e.g. ``calfkit-auth anthropic ...``) without
having to rename the entry point.

Commands:

  calfkit-auth codex login [--device-code] [--no-browser] [--force]
      Run the OAuth flow and cache credentials under ``~/.calfcord/auth/``.
  calfkit-auth codex logout
      Delete cached credentials.
  calfkit-auth codex status
      Print whether credentials are present, their expiry, and the
      decoded ChatGPT account id.
  calfkit-auth codex refresh
      Force a token refresh now (debugging convenience).

All credential I/O is delegated to OpenHands SDK's
:class:`~openhands.sdk.llm.auth.OpenAISubscriptionAuth`, pointed at our
custom credential directory.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from openhands.sdk.llm.auth import OpenAISubscriptionAuth

from calfkit_organization.providers.codex.jwt import extract_account_id
from calfkit_organization.providers.codex.token_store import (
    get_credential_store,
    get_credentials_dir,
    load_credentials,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calfkit-auth", description="Authentication for calfcord LLM providers.")
    sub = parser.add_subparsers(dest="provider", required=True)

    codex = sub.add_parser("codex", help="OpenAI ChatGPT subscription auth (Codex models).")
    codex_sub = codex.add_subparsers(dest="command", required=True)

    login = codex_sub.add_parser("login", help="Authenticate via browser OAuth (or --device-code).")
    login.add_argument("--device-code", action="store_true", help="Use device-code flow (no local browser).")
    login.add_argument("--no-browser", action="store_true", help="Print the auth URL instead of opening a browser.")
    login.add_argument("--force", action="store_true", help="Force a fresh login even if cached credentials exist.")

    codex_sub.add_parser("logout", help="Delete cached credentials.")
    codex_sub.add_parser("status", help="Show whether credentials are present and when they expire.")
    codex_sub.add_parser("refresh", help="Force a token refresh now.")

    return parser


async def _cmd_login(args: argparse.Namespace) -> int:
    store = get_credential_store()
    auth = OpenAISubscriptionAuth(credential_store=store)

    if not args.force:
        # Reuse cached credentials when valid; refresh silently if expired.
        try:
            creds = await auth.refresh_if_needed()
        except Exception as exc:  # noqa: BLE001 — surface any refresh failure cleanly
            print(f"Cached credentials could not be refreshed ({exc}); performing fresh login.", file=sys.stderr)
            creds = None
        if creds is not None:
            print(f"Already logged in; credentials cached at {get_credentials_dir()}", file=sys.stderr)
            return 0

    auth_method = "device_code" if args.device_code else "browser"
    try:
        await auth.login(auth_method=auth_method, open_browser=not args.no_browser)
    except Exception as exc:  # noqa: BLE001 — top-level CLI handler
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    print(f"Login successful; credentials cached at {get_credentials_dir()}", file=sys.stderr)
    return 0


def _cmd_logout(_args: argparse.Namespace) -> int:
    store = get_credential_store()
    if load_credentials(store) is None:
        print("Not logged in; nothing to remove.", file=sys.stderr)
        return 0
    auth = OpenAISubscriptionAuth(credential_store=store)
    auth.logout()
    print("Logged out; cached credentials removed.", file=sys.stderr)
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    store = get_credential_store()
    creds = load_credentials(store)
    if creds is None:
        print(f"Not logged in. Credential dir: {get_credentials_dir()}")
        print("Run: uv run calfkit-auth codex login")
        return 1

    expires_at_s = creds.expires_at // 1000
    expires_dt = datetime.fromtimestamp(expires_at_s, tz=timezone.utc)
    seconds_remaining = expires_at_s - int(time.time())
    account_id = extract_account_id(creds.access_token) or "<could not decode>"

    print(f"Logged in. Credential dir: {get_credentials_dir()}")
    print(f"  ChatGPT account id: {account_id}")
    print(f"  Access token expires: {expires_dt.isoformat()}")
    if seconds_remaining > 0:
        print(f"  Time remaining: {seconds_remaining}s ({seconds_remaining // 60}m)")
    else:
        print(f"  Time remaining: expired {-seconds_remaining}s ago (refresh on next use)")
    return 0


async def _cmd_refresh(_args: argparse.Namespace) -> int:
    """Refresh the access token if it is expired.

    OpenHands' ``refresh_if_needed`` only refreshes when the cached token
    is within its expiry window; calling this on a still-fresh token is a
    no-op. To unconditionally re-mint, use ``login --force`` instead.
    """
    store = get_credential_store()
    if load_credentials(store) is None:
        print("Not logged in. Run: uv run calfkit-auth codex login", file=sys.stderr)
        return 1
    auth = OpenAISubscriptionAuth(credential_store=store)
    try:
        await auth.refresh_if_needed()
    except Exception as exc:  # noqa: BLE001 — top-level CLI handler
        print(f"Refresh failed: {exc}", file=sys.stderr)
        return 1
    return _cmd_status(_args)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Only the ``codex`` provider exists today; future expansion would dispatch here.
    if args.provider != "codex":
        parser.error(f"unknown provider: {args.provider}")

    if args.command == "login":
        return asyncio.run(_cmd_login(args))
    if args.command == "logout":
        return _cmd_logout(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "refresh":
        return asyncio.run(_cmd_refresh(args))

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    sys.exit(main())
