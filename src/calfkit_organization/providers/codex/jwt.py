"""JWT payload decoding for ChatGPT account-id extraction.

The OAuth access token returned by OpenAI's Codex auth flow is a JWT
that carries the ChatGPT account id as a custom claim under the
namespace ``https://api.openai.com/auth``. The Codex backend requires
this id to be sent in the ``chatgpt-account-id`` request header — without
it, requests fail with workspace-authorization errors.

We do not verify the JWT signature: the upstream Codex CLI doesn't
either, and we don't rely on the JWT for trust — we send it back to the
issuer who validates it. Skipping JWKS avoids a synchronous network call
at model-client construction time (a footgun in OpenHands' implementation
where it blocks the event loop on cold cache).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Final

logger = logging.getLogger(__name__)

_AUTH_NAMESPACE: Final[str] = "https://api.openai.com/auth"
_ACCOUNT_ID_KEY: Final[str] = "chatgpt_account_id"


def extract_account_id(token: str) -> str | None:
    """Return the ``chatgpt_account_id`` claim from a JWT, or ``None``.

    Accepts either an id_token or access_token; both carry the claim
    when issued by OpenAI's Codex OAuth flow.
    """
    try:
        _header_b64, payload_b64, _sig_b64 = token.split(".")
    except ValueError:
        logger.warning("Token is not a well-formed JWT (expected 3 dot-separated parts)")
        return None

    # JWT base64url payloads omit padding; restore it before decoding.
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Failed to decode JWT payload: %s", exc)
        return None

    auth_claim = payload.get(_AUTH_NAMESPACE)
    if not isinstance(auth_claim, dict):
        return None
    account_id = auth_claim.get(_ACCOUNT_ID_KEY)
    return account_id if isinstance(account_id, str) and account_id else None
