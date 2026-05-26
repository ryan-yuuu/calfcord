"""Tests for the OpenHands ↔ authlib token format bridge."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openhands.sdk.llm.auth import OAuthCredentials

from calfkit_organization.providers.codex.token_store import (
    authlib_token_to_credentials,
    credentials_to_authlib_token,
    get_credentials_dir,
    make_persist_callback,
)


def _sample_creds(expires_at_ms: int | None = None) -> OAuthCredentials:
    return OAuthCredentials(
        vendor="openai",
        access_token="access-xyz",
        refresh_token="refresh-abc",
        expires_at=expires_at_ms if expires_at_ms is not None else (int(time.time()) + 3600) * 1000,
    )


class TestCredentialsToAuthlibToken:
    def test_round_trip_preserves_tokens_and_expiry(self) -> None:
        creds = _sample_creds(expires_at_ms=1_700_000_000_000)
        token = credentials_to_authlib_token(creds)
        back = authlib_token_to_credentials(token)

        assert back.access_token == creds.access_token
        assert back.refresh_token == creds.refresh_token
        assert back.expires_at == creds.expires_at

    def test_ms_to_seconds_conversion(self) -> None:
        creds = _sample_creds(expires_at_ms=1_700_000_000_000)
        token = credentials_to_authlib_token(creds)
        # Authlib uses seconds; OpenHands stores ms
        assert token["expires_at"] == 1_700_000_000

    def test_token_type_is_bearer(self) -> None:
        token = credentials_to_authlib_token(_sample_creds())
        assert token["token_type"] == "Bearer"


class TestAuthlibTokenToCredentials:
    def test_uses_expires_at_when_present(self) -> None:
        token = {
            "token_type": "Bearer",
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": 1_700_000_000,
        }
        creds = authlib_token_to_credentials(token)
        assert creds.expires_at == 1_700_000_000 * 1000

    def test_falls_back_to_expires_in_when_expires_at_missing(self) -> None:
        before = int(time.time())
        token = {
            "token_type": "Bearer",
            "access_token": "x",
            "refresh_token": "y",
            "expires_in": 3600,
        }
        creds = authlib_token_to_credentials(token)
        # Should be roughly (now + 3600) * 1000, within a generous window
        # to tolerate slow CI / test runners.
        assert (before + 3600) * 1000 <= creds.expires_at <= (before + 3601) * 1000

    def test_defaults_expires_in_to_one_hour(self) -> None:
        before = int(time.time())
        token = {"access_token": "x", "refresh_token": "y"}
        creds = authlib_token_to_credentials(token)
        # Default 3600s when neither field present
        assert creds.expires_at >= (before + 3599) * 1000


class TestMakePersistCallback:
    @pytest.mark.asyncio
    async def test_callback_saves_via_store(self) -> None:
        store = MagicMock()
        callback = make_persist_callback(store)

        token = {
            "token_type": "Bearer",
            "access_token": "refreshed-access",
            "refresh_token": "refreshed-refresh",
            "expires_at": int(time.time()) + 3600,
        }
        await callback(token)

        assert store.save.call_count == 1
        saved: OAuthCredentials = store.save.call_args[0][0]
        assert saved.access_token == "refreshed-access"
        assert saved.refresh_token == "refreshed-refresh"

    @pytest.mark.asyncio
    async def test_callback_ignores_legacy_kwargs(self) -> None:
        """authlib passes the previous tokens as kwargs; we should ignore them."""
        store = MagicMock()
        callback = make_persist_callback(store)
        token = {"access_token": "a", "refresh_token": "b", "expires_in": 100}

        await callback(token, refresh_token="old-refresh", access_token="old-access")

        saved: OAuthCredentials = store.save.call_args[0][0]
        assert saved.access_token == "a"
        assert saved.refresh_token == "b"

    @pytest.mark.asyncio
    async def test_callback_does_not_save_on_malformed_response(self) -> None:
        """A token missing required fields should be logged, not persist garbage."""
        store = MagicMock()
        callback = make_persist_callback(store)

        # Missing access_token entirely
        await callback({"expires_in": 100})

        store.save.assert_not_called()


class TestGetCredentialsDir:
    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFCORD_AUTH_DIR", raising=False)
        assert get_credentials_dir() == Path.home() / ".calfcord" / "auth"

    def test_respects_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CALFCORD_AUTH_DIR", str(tmp_path))
        assert get_credentials_dir() == tmp_path
