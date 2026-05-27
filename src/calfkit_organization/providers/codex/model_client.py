"""Calfkit model client for OpenAI Codex via ChatGPT subscription.

Subclasses :class:`calfkit.providers.OpenAIResponsesModelClient` to:

1. Replace the underlying HTTP transport with an :class:`httpx.AsyncClient`
   whose ``auth`` is a :class:`_CodexBearerAuth`. That auth runs
   :meth:`httpx.Auth.async_auth_flow` on every outgoing request,
   refreshing the OAuth access token via OpenHands'
   :class:`OpenAISubscriptionAuth` when expired and writing the new
   bearer onto the request's ``Authorization`` header. Critically, the
   auth flow runs from ``httpx.send()`` — the path the OpenAI SDK
   actually uses — so token rotation works transparently. Earlier
   designs that wired ``authlib.integrations.httpx_client.AsyncOAuth2Client``
   as the http_client looked right but silently failed: authlib hooks
   into ``request()``, which the OpenAI SDK bypasses.

2. Point requests at ``https://chatgpt.com/backend-api/codex`` and inject
   the headers that mark requests as coming from the official Codex CLI
   (``originator: codex_cli_rs``, ``OpenAI-Beta: responses=experimental``,
   ``chatgpt-account-id: <from JWT>``).

3. Override :meth:`_map_messages` to apply the Codex impersonation
   transformation. The agent's real system prompt is smuggled into a
   synthetic leading user message; the ``instructions`` we send is the
   verbatim official Codex CLI system prompt for the requested model,
   fetched live from ``openai/codex`` (see :mod:`prompts`). The Codex
   backend fingerprints ``instructions`` against the official strings,
   so we resolve a per-model prompt at construction time rather than
   substituting a branded short string.

Construction bypasses calfkit's ``OpenAIResponsesModelClient.__init__``
because it builds its own ``OpenAIProvider`` without the ``http_client``
hook we need. We call the underlying vendored pydantic-ai initializer
directly with a provider configured to use our OAuth-aware client.

The runner MUST call :func:`prompts.prewarm_codex_prompts` once at
startup before constructing any :class:`CodexSubscriptionModelClient`;
construction calls :meth:`PromptResolver.resolve` synchronously and
will raise :class:`RuntimeError` if the resolver has not been loaded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from calfkit._vendor.pydantic_ai.models.openai import (
    OpenAIResponsesModel,
    OpenAIResponsesModelSettings,
)
from calfkit._vendor.pydantic_ai.providers.openai import OpenAIProvider
from calfkit.providers import OpenAIResponsesModelClient
from openhands.sdk.llm.auth import (
    CredentialStore,
    OpenAISubscriptionAuth,
    transform_for_subscription,
)

from calfkit_organization.providers.codex.jwt import extract_account_id
from calfkit_organization.providers.codex.prompts import (
    PromptResolver,
    get_default_resolver,
)
from calfkit_organization.providers.codex.token_store import load_credentials

logger = logging.getLogger(__name__)

# Codex CLI OAuth + API constants — matched to the official ``codex`` CLI so
# requests on the wire are indistinguishable from it.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
ORIGINATOR = "codex_cli_rs"
OPENAI_BETA = "responses=experimental"

# Placeholder string assigned to OpenAIProvider's required api_key; the
# value never reaches the wire because _CodexBearerAuth.async_auth_flow
# overrides the Authorization header on every request.
_API_KEY_PLACEHOLDER = "placeholder-overridden-by-auth-flow"
REFRESH_LEEWAY_SECONDS = 300


class _CodexBearerAuth(httpx.Auth):
    """httpx.Auth that injects a fresh Codex OAuth bearer on every request.

    Why this exists instead of authlib's ``AsyncOAuth2Client``:
    the OpenAI Python SDK dispatches requests via ``httpx.AsyncClient.send()``,
    which bypasses authlib's ``request()`` interceptor (where its OAuth
    flow + token-refresh logic lives). Result: the literal ``api_key``
    placeholder we pass to OpenAIProvider would go out as the Bearer
    token, and the Codex backend rejects it with HTTP 401 "Could not
    parse your authentication token".

    ``httpx.Auth.async_auth_flow`` runs on EVERY request regardless of how
    the request is dispatched, so it's the right hook for transparent
    token rotation when wrapping another SDK's HTTP client.

    On each request:
      1. Acquire the asyncio lock so concurrent in-flight requests don't
         race two refreshes against the OpenAI token endpoint.
      2. Call OpenHands' :meth:`OpenAISubscriptionAuth.refresh_if_needed`,
         which is a no-op when the cached access token is still fresh and
         performs the refresh + writes the new tokens back to the
         ``CredentialStore`` when expired.
      3. Overwrite the ``Authorization`` header on the outgoing request
         (the OpenAI SDK pre-populated it from the placeholder api_key
         — we replace it with the real bearer).
    """

    requires_request_body = False
    requires_response_body = False

    def __init__(self, store: CredentialStore):
        self._store = store
        self._auth = OpenAISubscriptionAuth(credential_store=store)
        self._lock = asyncio.Lock()

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        async with self._lock:
            creds = await self._auth.refresh_if_needed()
        if creds is None:
            raise RuntimeError(
                "Codex credentials disappeared mid-session. "
                "Re-login with: uv run calfkit-auth codex login"
            )
        request.headers["Authorization"] = f"Bearer {creds.access_token}"
        yield request


class CodexSubscriptionModelClient(OpenAIResponsesModelClient):
    """OpenAI Responses model client backed by a ChatGPT subscription."""

    def __init__(
        self,
        *,
        model_name: str,
        store: CredentialStore,
        resolver: PromptResolver | None = None,
    ):
        creds = load_credentials(store)
        if creds is None:
            # The factory hook should have validated this already; defensive guard
            # so a programming error here surfaces with a clear message rather
            # than a NoneType attribute error two frames deep.
            raise RuntimeError(
                "Cannot construct CodexSubscriptionModelClient without saved credentials. "
                "Run: uv run calfkit-auth codex login"
            )

        # Resolve the verbatim Codex CLI system prompt for this model. If the
        # runner forgot to call ``prewarm_codex_prompts()`` first, the default
        # resolver's ``resolve()`` raises RuntimeError with a clear message —
        # we let that propagate so the bug surfaces loudly at construction
        # rather than silently sending an unfingerprinted instructions block.
        self._codex_instructions: str = (resolver or get_default_resolver()).resolve(model_name)

        # Per-request OAuth bearer injection via httpx.Auth. The OpenAI SDK
        # calls ``send()`` (not ``request()``), so authlib's AsyncOAuth2Client
        # request-interceptor approach would silently fail — the auth flow
        # would never fire and the api_key placeholder would go out as the
        # Bearer token. ``httpx.Auth.async_auth_flow`` runs on every send,
        # which is the correct hook.
        http_client = httpx.AsyncClient(auth=_CodexBearerAuth(store))

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
        # http_client hook). Build the provider ourselves with the OAuth-aware
        # client, then call the underlying pydantic-ai initializer directly.
        # ``api_key`` is a non-empty placeholder because OpenAIProvider needs
        # one set; _CodexBearerAuth overwrites the Authorization header on
        # every outgoing request.
        provider = OpenAIProvider(
            http_client=http_client,
            base_url=CODEX_BASE_URL,
            api_key=_API_KEY_PLACEHOLDER,
        )

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

        * Discard the agent's instructions and substitute the verbatim Codex
          CLI system prompt for this model (resolved at construction time
          from the live ``openai/codex`` source). The Codex backend
          fingerprints this field — a mismatch fails the check.
        * Prepend the agent's real system prompt to the first user message
          as an ``input_text`` part so the model still receives it.

        The second step is delegated to OpenHands'
        :func:`transform_for_subscription`, which mutates ``openai_messages``
        in place to inject the synthetic leading user message. Its return
        value is discarded — the instructions we ship are ``self._codex_instructions``,
        not OpenHands' branded short string.
        """
        instructions, openai_messages = await super()._map_messages(
            messages, model_settings, model_request_parameters
        )
        # The base method returns either a non-empty string or an ``Omit`` sentinel
        # (truthy filter handles both). ``transform_for_subscription`` expects a
        # list of system-prompt strings.
        system_chunks: list[str] = [instructions] if isinstance(instructions, str) and instructions else []
        # Side effect only: mutates openai_messages to smuggle the agent's
        # real system prompt into a synthetic leading user message.
        transform_for_subscription(system_chunks, openai_messages)
        return self._codex_instructions, openai_messages
