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


class TestConstruction:
    def test_settings_carry_expected_headers(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="acct_demo")
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

        headers = client.model_settings.get("extra_headers", {})
        assert headers.get("originator") == ORIGINATOR
        assert headers.get("OpenAI-Beta") == OPENAI_BETA
        assert headers.get("chatgpt-account-id") == "acct_demo"

    def test_account_id_header_absent_when_jwt_lacks_claim(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id=None)
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

        headers = client.model_settings.get("extra_headers", {})
        # Required headers still set; account-id is omitted (warning logged)
        assert headers.get("originator") == ORIGINATOR
        assert headers.get("OpenAI-Beta") == OPENAI_BETA
        assert "chatgpt-account-id" not in headers

    def test_extra_body_forces_store_false(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

        body = client.model_settings.get("extra_body", {})
        assert body.get("store") is False

    def test_send_reasoning_ids_disabled(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

        # The setting is keyed as openai_send_reasoning_ids on OpenAIResponsesModelSettings
        assert client.model_settings.get("openai_send_reasoning_ids") is False

    def test_underlying_provider_uses_authlib_client(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

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
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

        provider = client._provider  # type: ignore[attr-defined]
        underlying: AsyncOAuth2Client = provider.client._client
        assert underlying.leeway == REFRESH_LEEWAY_SECONDS

    def test_base_url_points_at_codex_backend(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

        # OpenAIProvider exposes base_url via its OpenAI client
        provider = client._provider  # type: ignore[attr-defined]
        underlying = provider.client
        # The openai client normalises to include a trailing slash
        assert str(underlying.base_url).rstrip("/") == CODEX_BASE_URL


class TestImpersonationTransformation:
    @pytest.mark.asyncio
    async def test_map_messages_substitutes_instructions(self, tmp_path: Path) -> None:
        """The agent's actual system prompt should NOT be sent as ``instructions``.

        Instead it's prepended to the first user message and ``instructions``
        is replaced with the short fixed string OpenHands' transformation
        returns.
        """
        from calfkit._vendor.pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart
        from calfkit._vendor.pydantic_ai.models import ModelRequestParameters

        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(model_name="gpt-5.2-codex", store=store)

        messages = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="You are a banana expert. Always mention bananas."),
                    UserPromptPart(content="hello"),
                ]
            )
        ]

        new_instructions, transformed = await client._map_messages(
            messages,
            client.model_settings,
            ModelRequestParameters(),
        )

        # The instructions returned to pydantic-ai are NOT the agent's prompt.
        # They are the short fixed string that OpenHands' transform substitutes
        # (we don't assert the exact text — it lives in openhands-sdk and may
        # change across versions — but it must be a non-empty string distinct
        # from the agent's actual prompt).
        assert isinstance(new_instructions, str)
        assert new_instructions != "You are a banana expert. Always mention bananas."
        assert len(new_instructions) > 0

        # The agent's real prompt is prepended to a user message as input_text.
        flattened = _flatten_text(transformed)
        assert "You are a banana expert. Always mention bananas." in flattened


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
