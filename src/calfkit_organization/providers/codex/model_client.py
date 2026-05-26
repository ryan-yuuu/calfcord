"""Calfkit model client for OpenAI Codex via ChatGPT subscription.

Subclasses :class:`calfkit.providers.OpenAIResponsesModelClient` to:

1. Replace the underlying HTTP transport with an
   :class:`~authlib.integrations.httpx_client.AsyncOAuth2Client`, which
   transparently refreshes the OAuth access token before each request
   when it is about to expire. This eliminates the need for a background
   refresh task or per-request bearer-injection plumbing — authlib's
   ``request()`` interceptor handles both.

2. Point requests at ``https://chatgpt.com/backend-api/codex`` and inject
   the headers that mark requests as coming from the official Codex CLI
   (``originator: codex_cli_rs``, ``OpenAI-Beta: responses=experimental``,
   ``chatgpt-account-id: <from JWT>``).

3. Override :meth:`_map_messages` to apply the Codex impersonation
   transformation: replace the agent's actual system prompt with a short
   fixed instruction string, and prepend the real system prompt to the
   first user message as an ``input_text`` part. The Codex backend
   rejects long or structurally complex ``instructions`` blocks; this
   matches what OpenHands ships and what the wire shape from the
   official Codex CLI produces.

Construction bypasses calfkit's ``OpenAIResponsesModelClient.__init__``
because it builds its own ``OpenAIProvider`` without the ``http_client``
hook we need. We call the underlying vendored pydantic-ai initializer
directly with a provider configured to use our authlib client.
"""

from __future__ import annotations

import logging
from typing import Any

from authlib.integrations.httpx_client import AsyncOAuth2Client
from calfkit._vendor.pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from calfkit._vendor.pydantic_ai.providers.openai import OpenAIProvider
from calfkit.providers import OpenAIResponsesModelClient
from openhands.sdk.llm.auth import CredentialStore, transform_for_subscription

from calfkit_organization.providers.codex.jwt import extract_account_id
from calfkit_organization.providers.codex.token_store import (
    credentials_to_authlib_token,
    load_credentials,
    make_persist_callback,
)

logger = logging.getLogger(__name__)

# Codex CLI OAuth + API constants — matched to the official ``codex`` CLI so
# requests on the wire are indistinguishable from it.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
ORIGINATOR = "codex_cli_rs"
OPENAI_BETA = "responses=experimental"

# Refresh tokens up to 5 minutes before expiry. Authlib's default leeway is 60s,
# which is enough for short conversations but cuts close for long-running
# services where a request can be in flight when the window opens. 300s gives
# plenty of buffer without refreshing wastefully often.
REFRESH_LEEWAY_SECONDS = 300


class CodexSubscriptionModelClient(OpenAIResponsesModelClient):
    """OpenAI Responses model client backed by a ChatGPT subscription."""

    def __init__(self, *, model_name: str, store: CredentialStore):
        creds = load_credentials(store)
        if creds is None:
            # The factory hook should have validated this already; defensive guard
            # so a programming error here surfaces with a clear message rather
            # than a NoneType attribute error two frames deep.
            raise RuntimeError(
                "Cannot construct CodexSubscriptionModelClient without saved credentials. "
                "Run: uv run calfkit-auth codex login"
            )

        token = credentials_to_authlib_token(creds)

        # AsyncOAuth2Client extends httpx.AsyncClient and handles proactive
        # refresh inside its ``request()`` method, lock-guarded against
        # concurrent refreshes. The OpenAI SDK accepts it as ``http_client``
        # via OpenAIProvider, so all requests flow through it.
        http_client = AsyncOAuth2Client(
            client_id=CLIENT_ID,
            token=token,
            token_endpoint=TOKEN_URL,
            update_token=make_persist_callback(store),
            leeway=REFRESH_LEEWAY_SECONDS,
        )

        # ChatGPT account id is required by the Codex backend; it comes from a
        # custom claim in the JWT. Decoding doesn't verify the signature
        # (the issuer validates on the receiving end).
        account_id = extract_account_id(creds.access_token)
        if not account_id:
            logger.warning(
                "Could not extract chatgpt_account_id from access token; "
                "Codex requests may fail with workspace-authorization errors"
            )

        extra_headers: dict[str, str] = {
            "originator": ORIGINATOR,
            "OpenAI-Beta": OPENAI_BETA,
        }
        if account_id:
            extra_headers["chatgpt-account-id"] = account_id

        settings = OpenAIResponsesModelSettings(  # type: ignore[typeddict-item]
            # Codex backend persists nothing server-side when store=False;
            # required so reasoning items don't get stored and then 404 on the
            # next turn (a documented OpenHands bug we sidestep).
            extra_body={"store": False},
            extra_headers=extra_headers,
            # Codex rejects these; pass None to drop them from request bodies.
            temperature=None,
            max_tokens=None,
            # Skip sending stored reasoning IDs back on follow-up turns; with
            # ``store=False`` they would 404. Calfkit/pydantic-ai already
            # supports this knob — using it avoids the OpenHands workaround
            # of manually mutating message objects mid-request.
            openai_send_reasoning_ids=False,
        )

        # Bypass calfkit's __init__ (it constructs an OpenAIProvider without an
        # http_client hook). Build the provider ourselves with the authlib
        # client, then call the underlying pydantic-ai initializer directly.
        # ``api_key`` is a non-empty placeholder because OpenAIProvider asserts
        # one is set; authlib overrides the Authorization header per-request.
        provider = OpenAIProvider(http_client=http_client, base_url=CODEX_BASE_URL, api_key="bearer-from-oauth")

        self.model_settings = settings
        OpenAIResponsesModel.__init__(self, model_name, provider=provider, settings=settings)

    async def _map_messages(
        self,
        messages: list[Any],
        model_settings: Any,
        model_request_parameters: Any,
    ) -> tuple[Any, list[Any]]:
        """Apply the Codex impersonation transformation to the request body.

        The base implementation returns ``(instructions, input_items)`` where
        ``instructions`` is the agent's system prompt joined into a single
        string (or an ``Omit`` sentinel). We:

        * Discard the agent's instructions and substitute a short fixed string
          (Codex backend rejects long/complex instructions; this matches what
          OpenHands ships and what works on the wire).
        * Prepend the agent's real system prompt to the first user message as
          an ``input_text`` part so the model still receives it.

        Both steps are delegated to OpenHands' :func:`transform_for_subscription`
        which contains the precise wire-shape that the Codex backend accepts.
        """
        instructions, openai_messages = await super()._map_messages(
            messages, model_settings, model_request_parameters
        )
        # The base method returns either a non-empty string or an ``Omit`` sentinel
        # (truthy filter handles both). ``transform_for_subscription`` expects a
        # list of system-prompt strings.
        system_chunks: list[str] = [instructions] if isinstance(instructions, str) and instructions else []
        new_instructions, transformed = transform_for_subscription(system_chunks, openai_messages)
        return new_instructions, transformed
