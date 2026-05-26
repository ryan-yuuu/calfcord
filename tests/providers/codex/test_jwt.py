"""Tests for JWT claim extraction."""

from __future__ import annotations

import base64
import json

import pytest

from calfkit_organization.providers.codex.jwt import extract_account_id


def _make_jwt(payload: dict) -> str:
    """Build a JWT-shaped string with the given payload (signature ignored)."""

    def _b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    header = _b64({"alg": "RS256", "typ": "JWT"})
    body = _b64(payload)
    return f"{header}.{body}.signature-not-verified"


class TestExtractAccountId:
    def test_returns_account_id_from_valid_token(self) -> None:
        token = _make_jwt(
            {"https://api.openai.com/auth": {"chatgpt_account_id": "acct_abc123"}, "sub": "u"}
        )
        assert extract_account_id(token) == "acct_abc123"

    def test_returns_none_when_claim_namespace_missing(self) -> None:
        token = _make_jwt({"sub": "user-only"})
        assert extract_account_id(token) is None

    def test_returns_none_when_account_id_key_missing(self) -> None:
        token = _make_jwt({"https://api.openai.com/auth": {"other": "thing"}})
        assert extract_account_id(token) is None

    def test_returns_none_when_account_id_empty_string(self) -> None:
        token = _make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": ""}})
        assert extract_account_id(token) is None

    def test_returns_none_when_claim_namespace_not_a_dict(self) -> None:
        token = _make_jwt({"https://api.openai.com/auth": "not-a-dict"})
        assert extract_account_id(token) is None

    def test_returns_none_for_malformed_jwt_wrong_part_count(self) -> None:
        assert extract_account_id("only.two-parts") is None
        assert extract_account_id("one") is None

    def test_returns_none_for_unparseable_payload(self) -> None:
        # Valid 3-part shape but body isn't valid base64 JSON
        assert extract_account_id("header.invalid_base64_!!!.sig") is None

    @pytest.mark.parametrize("padding_chars", [0, 1, 2, 3])
    def test_handles_missing_base64_padding(self, padding_chars: int) -> None:
        # JWT spec strips '=' padding from base64url; verify we restore it
        # across all possible padding lengths.
        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "x" * (padding_chars or 1)}}
        token = _make_jwt(payload)
        # _make_jwt already strips padding; sanity check that decode still works
        assert extract_account_id(token) == "x" * (padding_chars or 1)
