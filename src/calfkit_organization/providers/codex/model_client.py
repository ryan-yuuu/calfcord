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

4. Resolve + validate the model against the live catalog at construction.
   ``model_name=None`` (agent left ``model:`` unset) selects the
   highest-priority model from ``models.json``; a configured model is
   checked via :meth:`PromptResolver.validate` and fails fast
   (:class:`~calfkit_organization.providers.codex.prompts.UnknownCodexModelError`
   / :class:`~calfkit_organization.providers.codex.prompts.DeprecatedCodexModelError`)
   when it is unknown, hidden, or deprecated. As a backstop for models the
   catalog still advertises but the backend has retired, the request path
   (:meth:`_open_codex_stream`, called from :meth:`_responses_create`)
   translates the request-time ``400 ... not supported when using Codex with a
   ChatGPT account`` into :class:`CodexModelNotSupportedError`.

Construction bypasses calfkit's ``OpenAIResponsesModelClient.__init__``
because it builds its own ``OpenAIProvider`` without the ``http_client``
hook we need. We call the underlying vendored pydantic-ai initializer
directly with a provider configured to use our OAuth-aware client.

The runner MUST call :func:`prompts.prewarm_codex_prompts` once at
startup before constructing any :class:`CodexSubscriptionModelClient`;
construction calls :meth:`PromptResolver.validate`/:meth:`default_slug`
synchronously and will raise :class:`RuntimeError` if the resolver has not
been loaded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from calfkit._vendor.pydantic_ai.exceptions import ModelHTTPError
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
    CodexModelError,
    PromptResolver,
    get_default_resolver,
)
from calfkit_organization.providers.codex.token_store import load_credentials

logger = logging.getLogger(__name__)

# Substring the Codex backend returns in the 400 body when a model is barred
# from ChatGPT-account auth, e.g.::
#
#     {"detail": "The 'gpt-5.3-codex' model is not supported when using
#                 Codex with a ChatGPT account."}
#
# Construction-time validation against the live catalog catches the *known*
# cases (deprecated / hidden / unknown slugs); this string backstops the
# residual case where the backend retires a model that ``models.json`` still
# advertises as available.
_CHATGPT_UNSUPPORTED_MARKER = "not supported when using Codex with a ChatGPT account"

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


class CodexModelNotSupportedError(RuntimeError):
    """Raised when the Codex backend rejects the wire model at request time.

    The backend returns ``400 ... not supported when using Codex with a
    ChatGPT account`` for models that ``models.json`` still lists as available
    but that have actually been retired for ChatGPT-subscription auth. We
    translate that opaque ``ModelHTTPError`` into this actionable error naming
    the current catalog default, so the failure reads as a config problem
    rather than a transport error deep in the request path.
    """


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
        model_name: str | None,
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

        # Resolve the wire model + its fingerprinted prompt from the live
        # catalog. If the runner forgot to call ``prewarm_codex_prompts()``,
        # the resolver raises RuntimeError ("before ensure_loaded") — we let it
        # propagate so the bug surfaces loudly at construction.
        self._resolver = resolver or get_default_resolver()
        # ``model_name is None`` means the agent left ``model:`` unset for the
        # Codex provider: pick the current flagship from the catalog instead of
        # a hard-coded default that rots when OpenAI retires a model.
        resolved_model = model_name or self._resolver.default_slug()
        if model_name is None:
            logger.info(
                "No Codex model configured; defaulting to highest-priority %r",
                resolved_model,
            )
        # Fail fast on a misconfigured model (unknown / hidden / deprecated)
        # before building any transport. ``validate`` does the same longest-
        # prefix match as ``resolve``, so the matched entry's prompt is the one
        # OpenAI fingerprints — use it directly rather than re-matching.
        entry = self._resolver.validate(resolved_model)
        self._codex_instructions: str = entry.base_instructions
        # ``resolved_model`` is the single source of truth from here on: it's the
        # configured/derived slug that goes on the wire (it may be a forward-
        # compatible variant like ``gpt-5.5-codex`` that prefix-matched the
        # ``gpt-5.5`` catalog entry for its prompt). The ``model_name`` parameter
        # is not used past this point.

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

        # NOTE on omitted keys: do NOT pass ``temperature``, ``max_tokens``,
        # ``parallel_tool_calls`` here — even with value ``None``. The Codex
        # backend rejects any of those fields appearing in the request body
        # (returns ``400 Unsupported parameter: max_output_tokens`` etc).
        # pydantic-ai's ``_responses_create`` reads them via
        # ``model_settings.get('max_tokens', OMIT)`` — present-but-None
        # serializes to ``null`` on the wire, but absent (so .get returns
        # the OMIT sentinel) gets dropped from the JSON body entirely.
        settings = OpenAIResponsesModelSettings(  # type: ignore[typeddict-item]
            # Codex backend persists nothing server-side when store=False;
            # required so reasoning items don't get stored and then 404 on the
            # next turn (a documented OpenHands bug we sidestep).
            extra_body={"store": False},
            extra_headers=extra_headers,
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
        OpenAIResponsesModel.__init__(self, resolved_model, provider=provider, settings=settings)

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

    async def _responses_create(
        self,
        messages: Any,
        stream: bool,
        model_settings: Any,
        model_request_parameters: Any,
    ) -> Any:
        """Force ``stream=True`` on every wire request; reconstruct a Response
        for non-streaming callers.

        The Codex backend mandates streaming (returns
        ``400 'Stream must be set to true'`` otherwise). But pydantic-ai's
        ``Agent.run()`` calls the model's ``request()`` path, which asks
        for a non-streaming Response object. Solution: always open the
        stream, drain it, and reconstruct a Response from the events.

        Codex sends a ``response.completed`` event whose embedded Response
        has an empty ``.output`` list — the actual output items arrive
        as ``response.output_item.done`` events along the way. We
        accumulate them and patch them onto the final Response before
        returning.

        Streaming callers (``request_stream()``) pass through unchanged.

        Either path may raise the backend's ``400 ... not supported when using
        Codex with a ChatGPT account``; :meth:`_open_codex_stream` translates
        that into :class:`CodexModelNotSupportedError`.
        """
        if stream:
            return await self._open_codex_stream(
                messages, model_settings, model_request_parameters
            )

        stream_obj = await self._open_codex_stream(
            messages, model_settings, model_request_parameters
        )

        final_response: Any = None
        output_items: list[Any] = []
        async for event in stream_obj:
            event_type = getattr(event, "type", None)
            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if item is not None:
                    output_items.append(item)
            elif event_type == "response.completed":
                final_response = getattr(event, "response", None)

        if final_response is None:
            raise RuntimeError(
                "Codex stream ended without a response.completed event; "
                "cannot reconstruct a non-streaming Response."
            )

        # Patch accumulated items onto the Response if the completed event
        # came back with an empty output list (Codex backend quirk).
        if not getattr(final_response, "output", None) and output_items:
            final_response.output = output_items

        return final_response

    async def _open_codex_stream(
        self,
        messages: Any,
        model_settings: Any,
        model_request_parameters: Any,
    ) -> Any:
        """Open the wire stream, translating the ChatGPT-unsupported 400.

        The OpenAI SDK sends the request when the stream is created, so a
        ``model not supported`` rejection surfaces synchronously from this
        ``await`` rather than mid-iteration. We catch it here and re-raise as
        :class:`CodexModelNotSupportedError`; any other ``ModelHTTPError``
        propagates unchanged.
        """
        try:
            return await super()._responses_create(
                messages=messages,
                stream=True,
                model_settings=model_settings,
                model_request_parameters=model_request_parameters,
            )
        except ModelHTTPError as exc:
            if exc.status_code == 400 and _CHATGPT_UNSUPPORTED_MARKER in str(exc):
                raise self._chatgpt_unsupported_error() from exc
            raise

    def _chatgpt_unsupported_error(self) -> CodexModelNotSupportedError:
        """Build the actionable error for a backend ChatGPT-unsupported 400."""
        try:
            default = repr(self._resolver.default_slug())
        except (CodexModelError, RuntimeError) as exc:
            # The catalog can be empty (CodexModelError) or unloaded
            # (RuntimeError). Either way, fall back to a generic phrase rather
            # than let a secondary failure mask the original 400 — but log it,
            # since an empty/unloaded catalog at this point is itself a signal.
            logger.warning("Could not resolve catalog default for error message: %s", exc)
            default = "the current default"
        return CodexModelNotSupportedError(
            f"Codex backend rejected model {self.model_name!r} for this ChatGPT "
            f"account (HTTP 400): the model is not available to Codex "
            f"ChatGPT-subscription auth. Set the agent's `model:` to {default} "
            f"(or unset `model:` to use the highest-priority catalog default)."
        )
