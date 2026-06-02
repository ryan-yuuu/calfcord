"""Tests for the factory hook entry point and AgentFactory integration."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from openhands.sdk.llm.auth import CredentialStore, OAuthCredentials

from calfkit_organization.agents.definition import Provider
from calfkit_organization.providers.codex import CodexNotLoggedInError, build_codex_subscription_client


def _seed_credentials(tmp_path: Path) -> None:
    """Drop a valid-looking credential file in tmp_path so the factory hook
    proceeds past its precondition check."""
    store = CredentialStore(credentials_dir=tmp_path)
    creds = OAuthCredentials(
        vendor="openai",
        # A 3-part JWT shape so extract_account_id doesn't blow up on construction;
        # body intentionally lacks the claim — extract returns None and the client
        # is built without the chatgpt-account-id header (warning logged, not error).
        access_token="header.eyJzdWIiOiJ4In0.sig",
        refresh_token="refresh-tok",
        expires_at=(int(time.time()) + 3600) * 1000,
    )
    store.save(creds)


@pytest.fixture(autouse=True)
def _preloaded_default_resolver(monkeypatch, tmp_path):
    """Hydrate the process-singleton PromptResolver with synthetic prompts.

    The factory hook constructs a CodexSubscriptionModelClient which calls
    ``get_default_resolver().validate(model_name)`` (and ``default_slug()``
    when ``model_name`` is None) synchronously. In real deployments the runner
    prewarms the resolver before any model client is built; tests must do the
    same. We bypass network entirely by hydrating the singleton's private
    state with a fixed catalog.
    """
    from calfkit_organization.providers.codex import prompts as _prompts
    from calfkit_organization.providers.codex.prompt_cache import PromptCache
    from calfkit_organization.providers.codex.prompts import CodexModel, PromptResolver

    resolver = PromptResolver(cache=PromptCache(base_dir=tmp_path / "_prompts_cache"))
    resolver._catalog = {
        "gpt-5.2": CodexModel(
            slug="gpt-5.2", base_instructions="GPT-5.2 OFFICIAL PROMPT", priority=0
        )
    }
    resolver._fallback_prompt = "FALLBACK PROMPT"
    resolver._loaded = True
    monkeypatch.setattr(_prompts, "_default_resolver", resolver)
    yield
    # Defensive — restore to None so a later test that needs a fresh singleton
    # picks up its own monkeypatched state rather than this one's.
    monkeypatch.setattr(_prompts, "_default_resolver", None)


class TestBuildCodexSubscriptionClient:
    def test_raises_when_no_credentials_cached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path))
        with pytest.raises(CodexNotLoggedInError, match="calfkit-auth codex login"):
            build_codex_subscription_client(model_name="gpt-5.2-codex")

    def test_constructs_client_when_credentials_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path))
        _seed_credentials(tmp_path)

        client = build_codex_subscription_client(model_name="gpt-5.2-codex")

        # Must be the calfkit-compatible base type so AgentFactory accepts it
        from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

        assert isinstance(client, PydanticModelClient)
        # Codex base URL is configured on the underlying pydantic-ai provider
        assert client.model_name == "gpt-5.2-codex"


class TestProviderLiteral:
    def test_openai_codex_is_a_valid_provider(self) -> None:
        import typing

        assert "openai-codex" in typing.get_args(Provider)


class TestDefaultModelClientFactory:
    def test_dispatches_openai_codex_when_credentials_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path))
        _seed_credentials(tmp_path)

        from calfkit_organization.agents.factory import _default_model_client_factory

        client = _default_model_client_factory("openai-codex", "gpt-5.2-codex")
        from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

        assert isinstance(client, PydanticModelClient)

    def test_raises_codex_not_logged_in_when_credentials_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path))

        from calfkit_organization.agents.factory import _default_model_client_factory

        with pytest.raises(CodexNotLoggedInError):
            _default_model_client_factory("openai-codex", "gpt-5.2-codex")

    def test_default_model_for_openai_codex_is_none(self) -> None:
        """openai-codex has no static default: the Codex client resolves the
        highest-priority model from the live catalog at construction."""
        from calfkit_organization.agents.factory import _PROVIDER_DEFAULT_MODELS

        assert _PROVIDER_DEFAULT_MODELS["openai-codex"] is None

    def test_dispatches_openai_codex_with_unset_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """model_name=None reaches the Codex client, which defaults from the
        catalog (the preloaded fixture's gpt-5.2)."""
        monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path))
        _seed_credentials(tmp_path)

        from calfkit_organization.agents.factory import _default_model_client_factory

        client = _default_model_client_factory("openai-codex", None)
        assert client.model_name == "gpt-5.2"
