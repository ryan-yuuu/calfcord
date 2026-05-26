"""Bridge between OpenHands' :class:`CredentialStore` and authlib's token dict.

Both libraries persist OAuth credentials, but with different schemas and
time units:

* OpenHands ``OAuthCredentials``: ``expires_at`` in **milliseconds**, tokens
  as plain strings, serialised via Pydantic to ``{vendor}_oauth.json``.
* Authlib ``AsyncOAuth2Client``: in-memory token dict with ``expires_at``
  in **seconds**, ``token_type``, and standard OAuth2 fields.

This module owns the conversion in both directions and provides a single
canonical on-disk location at ``~/.calfcord/auth/`` (override with
``CALFCORD_AUTH_DIR``). OpenHands writes during initial login; authlib
writes during runtime refresh — both go through the same OpenHands
``CredentialStore`` so the file format stays consistent.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from openhands.sdk.llm.auth import CredentialStore, OAuthCredentials

logger = logging.getLogger(__name__)

_VENDOR = "openai"
_AUTH_DIR_ENV = "CALFCORD_AUTH_DIR"
_DEFAULT_AUTH_DIR = Path.home() / ".calfcord" / "auth"


def get_credentials_dir() -> Path:
    """Resolve the credential directory, honouring ``CALFCORD_AUTH_DIR``."""
    override = os.environ.get(_AUTH_DIR_ENV)
    return Path(override) if override else _DEFAULT_AUTH_DIR


def get_credential_store() -> CredentialStore:
    """Construct an OpenHands :class:`CredentialStore` rooted in our auth dir."""
    return CredentialStore(credentials_dir=get_credentials_dir())


def credentials_to_authlib_token(creds: OAuthCredentials) -> dict[str, Any]:
    """Convert OpenHands credentials to the dict shape AsyncOAuth2Client expects.

    Authlib measures ``expires_at`` in Unix **seconds**; OpenHands stores
    **milliseconds**. We also include ``token_type`` because authlib
    defaults to checking it when injecting the Authorization header.
    """
    return {
        "token_type": "Bearer",
        "access_token": creds.access_token,
        "refresh_token": creds.refresh_token,
        "expires_at": creds.expires_at // 1000,
    }


def authlib_token_to_credentials(token: dict[str, Any]) -> OAuthCredentials:
    """Convert an authlib token dict back into an OpenHands :class:`OAuthCredentials`.

    The ``expires_at`` field in authlib's dict is Unix seconds; we
    convert to milliseconds for OpenHands' schema. If ``expires_at`` is
    missing (some refresh responses omit it when ``expires_in`` was used),
    we recompute from ``expires_in`` against ``time.time()``.
    """
    expires_at_s = token.get("expires_at")
    if expires_at_s is None:
        expires_in = int(token.get("expires_in", 3600))
        expires_at_s = int(time.time()) + expires_in

    return OAuthCredentials(
        vendor=_VENDOR,
        access_token=token["access_token"],
        # Some refresh responses omit refresh_token (means: keep the old one).
        # Authlib handles that internally by reusing the prior value, so by the
        # time this function sees the dict, refresh_token should be present.
        refresh_token=token["refresh_token"],
        expires_at=int(expires_at_s) * 1000,
    )


def make_persist_callback(store: CredentialStore):
    """Return an async ``update_token`` callback for AsyncOAuth2Client.

    Authlib invokes this on every successful refresh with the new token
    dict. We persist via OpenHands' ``CredentialStore`` so the on-disk
    format stays consistent with whatever login wrote, and so a future
    login (e.g. ``calfkit-auth codex login --force``) can read it back.
    """

    async def _persist(token: dict[str, Any], refresh_token: str | None = None, access_token: str | None = None) -> None:
        # Authlib passes the previous refresh_token/access_token as kwargs for
        # callbacks that need diffing; we ignore them — the new values live in
        # ``token``.
        del refresh_token, access_token
        try:
            creds = authlib_token_to_credentials(token)
        except KeyError as exc:
            logger.error("Refresh response missing required field; not persisting: %s", exc)
            return
        store.save(creds)
        logger.info("Codex access token refreshed; new expiry in %ds", creds.expires_at // 1000 - int(time.time()))

    return _persist


def load_credentials(store: CredentialStore) -> OAuthCredentials | None:
    """Load saved credentials, or ``None`` if no login has been performed."""
    return store.get(vendor=_VENDOR)
