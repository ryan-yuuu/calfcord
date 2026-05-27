"""Tests for CodexSubscriptionModelClient construction and impersonation.

These tests verify the constructed model client carries the headers, body
fields, and settings that the Codex backend requires — without making
real network calls.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from openhands.sdk.llm.auth import CredentialStore, OAuthCredentials

from calfkit_organization.providers.codex.model_client import (
    CODEX_BASE_URL,
    OPENAI_BETA,
    ORIGINATOR,
    CodexSubscriptionModelClient,
    _CodexBearerAuth,
)


def _seed(tmp_path: Path, *, account_id: str | None = None) -> CredentialStore:
    """Persist fake credentials in tmp_path and return the store.

    If ``account_id`` is given, the JWT payload includes the claim so the
    constructed client carries a ``chatgpt-account-id`` header.
    """
    import base64
    import json

    payload: dict = {"sub": "test-user"}
    if account_id is not None:
        payload["https://api.openai.com/auth"] = {"chatgpt_account_id": account_id}

    def _b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    token = f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"
    store = CredentialStore(credentials_dir=tmp_path)
    store.save(
        OAuthCredentials(
            vendor="openai",
            access_token=token,
            refresh_token="rt",
            expires_at=(int(time.time()) + 3600) * 1000,
        )
    )
    return store


def _loaded_resolver(
    tmp_path: Path,
    models: dict[str, str] | None = None,
    fallback: str = "FALLBACK PROMPT",
):
    """Build a PromptResolver loaded with synthetic prompts, no network."""
    from calfkit_organization.providers.codex.prompt_cache import PromptCache
    from calfkit_organization.providers.codex.prompts import PromptResolver

    resolver = PromptResolver(cache=PromptCache(base_dir=tmp_path / "prompts"))
    # Bypass network: hydrate the resolver's private state directly for tests.
    resolver._models = models or {
        "gpt-5.2": "GPT-5.2 OFFICIAL PROMPT",
        "gpt-5.3-codex": "GPT-5.3 OFFICIAL PROMPT",
    }
    resolver._fallback_prompt = fallback
    resolver._loaded = True
    return resolver


class TestConstruction:
    def test_settings_carry_expected_headers(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="acct_demo")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        headers = client.model_settings.get("extra_headers", {})
        assert headers.get("originator") == ORIGINATOR
        assert headers.get("OpenAI-Beta") == OPENAI_BETA
        assert headers.get("chatgpt-account-id") == "acct_demo"

    def test_account_id_header_absent_when_jwt_lacks_claim(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id=None)
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        headers = client.model_settings.get("extra_headers", {})
        # Required headers still set; account-id is omitted (warning logged)
        assert headers.get("originator") == ORIGINATOR
        assert headers.get("OpenAI-Beta") == OPENAI_BETA
        assert "chatgpt-account-id" not in headers

    def test_extra_body_forces_store_false(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        body = client.model_settings.get("extra_body", {})
        assert body.get("store") is False

    def test_send_reasoning_ids_disabled(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        # The setting is keyed as openai_send_reasoning_ids on OpenAIResponsesModelSettings
        assert client.model_settings.get("openai_send_reasoning_ids") is False

    def test_underlying_provider_uses_codex_bearer_auth(self, tmp_path: Path) -> None:
        """The OpenAI SDK's httpx client must have our _CodexBearerAuth attached
        as its auth — this is the hook that rotates the OAuth bearer on every
        outgoing request. Without it the api_key placeholder leaks onto the
        wire and the Codex backend returns 401 ``Could not parse your
        authentication token``."""
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        # Reach into pydantic-ai's provider to assert we wired the OAuth-aware
        # transport. Accessing _client is fragile but is the only way to verify
        # the auth hook is actually attached without spinning up the network.
        provider = client._provider  # type: ignore[attr-defined]
        underlying = provider.client._client  # AsyncOpenAI._client is the httpx instance
        assert isinstance(underlying, httpx.AsyncClient)
        assert isinstance(underlying.auth, _CodexBearerAuth)

    def test_base_url_points_at_codex_backend(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        # OpenAIProvider exposes base_url via its OpenAI client
        provider = client._provider  # type: ignore[attr-defined]
        underlying = provider.client
        # The openai client normalises to include a trailing slash
        assert str(underlying.base_url).rstrip("/") == CODEX_BASE_URL

    def test_raises_when_resolver_not_loaded(self, tmp_path: Path) -> None:
        """If runner forgot to prewarm, construction should fail loudly."""
        from calfkit_organization.providers.codex.prompt_cache import PromptCache
        from calfkit_organization.providers.codex.prompts import PromptResolver

        store = _seed(tmp_path, account_id="x")
        unloaded_resolver = PromptResolver(cache=PromptCache(base_dir=tmp_path / "prompts"))
        # NOT calling ensure_loaded() — simulates runner bug
        with pytest.raises(RuntimeError, match="ensure_loaded"):
            CodexSubscriptionModelClient(
                model_name="gpt-5.2-codex",
                store=store,
                resolver=unloaded_resolver,
            )


class TestImpersonationTransformation:
    @pytest.mark.asyncio
    async def test_map_messages_uses_resolved_codex_prompt(self, tmp_path: Path) -> None:
        """``instructions`` returned to pydantic-ai should be the verbatim
        per-model Codex CLI prompt resolved at construction time, not the
        agent's own system prompt or OpenHands' branded short string.
        """
        from calfkit._vendor.pydantic_ai.messages import (
            ModelRequest,
            SystemPromptPart,
            UserPromptPart,
        )
        from calfkit._vendor.pydantic_ai.models import ModelRequestParameters

        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_resolver(tmp_path, models={"gpt-5.2": "OFFICIAL GPT-5.2 PROMPT"})
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=resolver,
        )

        messages = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="You are a banana expert."),
                    UserPromptPart(content="hello"),
                ]
            )
        ]

        new_instructions, transformed = await client._map_messages(
            messages,
            client.model_settings,
            ModelRequestParameters(),
        )
        # gpt-5.2-codex longest-prefix matches gpt-5.2 → official prompt
        assert new_instructions == "OFFICIAL GPT-5.2 PROMPT"
        # Agent's real prompt still smuggled into a user message
        flattened = _flatten_text(transformed)
        assert "You are a banana expert." in flattened

    @pytest.mark.asyncio
    async def test_map_messages_uses_longest_prefix_match(self, tmp_path: Path) -> None:
        """gpt-5.2-codex should pull the gpt-5.2 entry's prompt via longest-prefix."""
        from calfkit._vendor.pydantic_ai.messages import ModelRequest, UserPromptPart
        from calfkit._vendor.pydantic_ai.models import ModelRequestParameters

        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_resolver(
            tmp_path,
            models={
                "gpt-5": "GENERIC GPT-5",
                "gpt-5.2": "SPECIFIC GPT-5.2",
            },
        )
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=resolver,
        )

        messages = [ModelRequest(parts=[UserPromptPart(content="hi")])]
        new_instructions, _ = await client._map_messages(
            messages,
            client.model_settings,
            ModelRequestParameters(),
        )
        assert new_instructions == "SPECIFIC GPT-5.2"


class TestCodexBearerAuth:
    """``_CodexBearerAuth`` is the per-request hook that overrides whatever
    Authorization header the OpenAI SDK sets (which would be the placeholder
    api_key) with the current OAuth access token. If this stops working the
    Codex backend rejects requests with 401 ``Could not parse your
    authentication token``."""

    @pytest.mark.asyncio
    async def test_auth_flow_overrides_authorization_header(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        # Stored credentials are fresh (1h expiry), so refresh_if_needed is
        # a no-op and the cached access_token gets injected as-is.
        cached_access_token = store.get(vendor="openai").access_token  # type: ignore[union-attr]

        auth = _CodexBearerAuth(store)
        request = httpx.Request(
            "POST",
            "https://chatgpt.com/backend-api/codex/responses",
            headers={"Authorization": "Bearer placeholder-from-openai-sdk"},
        )

        # async_auth_flow is an async generator that yields the (possibly
        # mutated) request; the framework would then await the response.
        gen = auth.async_auth_flow(request)
        yielded = await gen.__anext__()
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

        assert yielded.headers["Authorization"] == f"Bearer {cached_access_token}"

    @pytest.mark.asyncio
    async def test_auth_flow_raises_when_credentials_gone(self, tmp_path: Path) -> None:
        """If the credential file is deleted mid-session (e.g. operator ran
        ``calfkit-auth codex logout`` while the agent was running), the next
        request must surface a clear actionable error rather than sending
        an empty Bearer that the server would parse as malformed."""
        # Build the auth against an empty store — no credentials cached.
        empty_store = CredentialStore(credentials_dir=tmp_path / "empty")
        auth = _CodexBearerAuth(empty_store)
        request = httpx.Request("POST", "https://example.invalid/")
        gen = auth.async_auth_flow(request)
        with pytest.raises(RuntimeError, match="calfkit-auth codex login"):
            await gen.__anext__()


# --- Helpers -----------------------------------------------------------------


def _flatten_text(items: list) -> str:
    """Collect all text-bearing fields across a list of pydantic-ai input items."""
    chunks: list[str] = []
    for item in items:
        # Items are TypedDicts at runtime → just dicts
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
    return "\n".join(chunks)
