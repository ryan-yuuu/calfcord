"""Tests for CodexSubscriptionModelClient construction and impersonation.

These tests verify the constructed model client carries the headers, body
fields, and settings that the Codex backend requires — without making
real network calls.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from openhands.sdk.llm.auth import CredentialStore, OAuthCredentials

from calfkit_organization.providers.codex.model_client import (
    CODEX_BASE_URL,
    OPENAI_BETA,
    ORIGINATOR,
    REFRESH_LEEWAY_SECONDS,
    CodexSubscriptionModelClient,
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

    def test_underlying_provider_uses_authlib_client(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        # Reach into pydantic-ai's provider to assert we wired the authlib transport.
        # Accessing _client / _http_client is fragile but is the only way to verify
        # the auth-rotation hook is actually attached without spinning up the network.
        provider = client._provider  # type: ignore[attr-defined]
        underlying = provider.client._client  # AsyncOpenAI._client is the httpx instance
        assert isinstance(underlying, AsyncOAuth2Client)

    def test_authlib_client_uses_long_refresh_leeway(self, tmp_path: Path) -> None:
        """Verify we widened authlib's default 60s to give long-running services
        plenty of buffer before token expiry."""
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        provider = client._provider  # type: ignore[attr-defined]
        underlying: AsyncOAuth2Client = provider.client._client
        assert underlying.leeway == REFRESH_LEEWAY_SECONDS

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
