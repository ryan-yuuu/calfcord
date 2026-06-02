"""Tests for CodexSubscriptionModelClient construction and impersonation.

These tests verify the constructed model client carries the headers, body
fields, and settings that the Codex backend requires — without making
real network calls.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any

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
    """Build a PromptResolver loaded with synthetic prompts, no network.

    ``models`` is a ``slug -> base_instructions`` map; each entry becomes an
    active (selectable, non-deprecated) :class:`CodexModel`. For richer
    catalogs (priorities, hidden/deprecated entries) use
    :func:`_loaded_catalog_resolver`.
    """
    instr = models or {
        "gpt-5.2": "GPT-5.2 OFFICIAL PROMPT",
        "gpt-5.3-codex": "GPT-5.3 OFFICIAL PROMPT",
    }
    from calfkit_organization.providers.codex.prompts import CodexModel

    return _loaded_catalog_resolver(
        tmp_path,
        [CodexModel(slug=slug, base_instructions=text) for slug, text in instr.items()],
        fallback=fallback,
    )


def _loaded_catalog_resolver(
    tmp_path: Path,
    entries: list,
    fallback: str = "FALLBACK PROMPT",
):
    """Build a PromptResolver hydrated with explicit :class:`CodexModel` entries."""
    from calfkit_organization.providers.codex.prompt_cache import PromptCache
    from calfkit_organization.providers.codex.prompts import PromptResolver

    resolver = PromptResolver(cache=PromptCache(base_dir=tmp_path / "prompts"))
    # Bypass network: hydrate the resolver's private state directly for tests.
    resolver._catalog = {m.slug: m for m in entries}
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

    @pytest.mark.parametrize("field", ["temperature", "max_tokens", "parallel_tool_calls"])
    def test_codex_rejected_fields_are_absent_not_none(self, tmp_path: Path, field: str) -> None:
        """Regression guard for ``400 Unsupported parameter: <field>``.

        pydantic-ai's ``_responses_create`` reads these settings via
        ``model_settings.get(field, OMIT)``. If the field is present with
        ``None``, it serializes to ``null`` on the wire and the Codex
        backend rejects the entire request. The field must be absent so
        ``get(..., OMIT)`` returns the omit sentinel and pydantic-ai
        drops it from the request body entirely.
        """
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        # ``in`` on a TypedDict tests for key presence — what we care about.
        assert field not in client.model_settings, (
            f"{field!r} must not be a key in model_settings (even with value "
            f"None) — Codex backend rejects the field in the request body"
        )

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


class TestForcedStreaming:
    """The Codex backend requires ``stream=True`` on every request; the
    pydantic-ai ``request()`` path asks for non-streaming. We override
    ``_responses_create`` to always stream and reconstruct a Response from
    the stream events. If this stops working the backend returns
    ``400 'Stream must be set to true'``."""

    @pytest.mark.asyncio
    async def test_non_streaming_call_forces_stream_and_reconstructs(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        # Mock super()._responses_create to capture how it's called and
        # return a fake stream of events. We intercept at the
        # OpenAIResponsesModel level (the immediate parent of our override).
        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        captured_stream_arg: list[bool] = []

        class _FakeEvent:
            def __init__(self, type_: str, item: Any = None, response: Any = None):
                self.type = type_
                self.item = item
                self.response = response

        class _FakeResponse:
            def __init__(self):
                self.output: list[Any] = []  # Codex quirk: arrives empty

        fake_item_a = object()
        fake_item_b = object()
        completed_response = _FakeResponse()

        async def _fake_super_create(self, *, messages, stream, model_settings, model_request_parameters):
            captured_stream_arg.append(stream)

            async def _gen():
                yield _FakeEvent("response.output_item.done", item=fake_item_a)
                yield _FakeEvent("response.output_text.delta")  # ignored
                yield _FakeEvent("response.output_item.done", item=fake_item_b)
                yield _FakeEvent("response.completed", response=completed_response)

            return _gen()

        # Patch the parent class's method so our override delegates to the fake.
        original = OpenAIResponsesModel._responses_create
        OpenAIResponsesModel._responses_create = _fake_super_create
        try:
            result = await client._responses_create(
                messages=[],
                stream=False,  # caller wants non-streaming
                model_settings=client.model_settings,
                model_request_parameters=None,
            )
        finally:
            OpenAIResponsesModel._responses_create = original

        # Even though the caller asked stream=False, we forced True on the wire.
        assert captured_stream_arg == [True]
        # Returned object is the reconstructed Response with output patched in.
        assert result is completed_response
        assert result.output == [fake_item_a, fake_item_b]

    @pytest.mark.asyncio
    async def test_streaming_call_passes_through_unchanged(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        sentinel_stream = object()

        async def _fake_super_create(self, *, messages, stream, model_settings, model_request_parameters):
            assert stream is True
            return sentinel_stream

        original = OpenAIResponsesModel._responses_create
        OpenAIResponsesModel._responses_create = _fake_super_create
        try:
            result = await client._responses_create(
                messages=[],
                stream=True,  # caller already wants a stream
                model_settings=client.model_settings,
                model_request_parameters=None,
            )
        finally:
            OpenAIResponsesModel._responses_create = original

        assert result is sentinel_stream

    @pytest.mark.asyncio
    async def test_missing_completed_event_raises(self, tmp_path: Path) -> None:
        """If the stream ends without a ``response.completed`` event, we'd
        have no Response to return — surface that loudly rather than
        returning None which would crash downstream pydantic-ai code with
        a less clear error."""
        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex",
            store=store,
            resolver=_loaded_resolver(tmp_path),
        )

        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        async def _fake_super_create(self, *, messages, stream, model_settings, model_request_parameters):
            async def _gen():
                if False:  # empty stream
                    yield None

            return _gen()

        original = OpenAIResponsesModel._responses_create
        OpenAIResponsesModel._responses_create = _fake_super_create  # pyright: ignore[reportAttributeAccessIssue]
        try:
            with pytest.raises(RuntimeError, match="response.completed"):  # noqa: RUF043
                await client._responses_create(
                    messages=[],
                    stream=False,
                    model_settings=client.model_settings,
                    model_request_parameters=None,
                )
        finally:
            OpenAIResponsesModel._responses_create = original


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
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()

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


# --- Model resolution + validation -------------------------------------------


def _model(slug, instr="PROMPT", *, priority=0, visibility="list", upgrade_to=None):
    from calfkit_organization.providers.codex.prompts import CodexModel

    return CodexModel(
        slug=slug,
        base_instructions=instr,
        priority=priority,
        visibility=visibility,
        upgrade_to=upgrade_to,
    )


def _unsupported_raiser(model: str = "gpt-5.2-codex"):
    """A fake ``_responses_create`` that raises the backend's ChatGPT-unsupported 400."""
    from calfkit._vendor.pydantic_ai.exceptions import ModelHTTPError

    async def _raise(self, *, messages, stream, model_settings, model_request_parameters):
        raise ModelHTTPError(
            status_code=400,
            model_name=model,
            body={
                "detail": f"The '{model}' model is not supported when using "
                "Codex with a ChatGPT account."
            },
        )

    return _raise


class TestModelResolutionAndValidation:
    """Construction-time default selection + fail-fast validation against the
    live catalog (the fix for pinning a retired ``gpt-5.3-codex``)."""

    def test_unset_model_defaults_to_highest_priority(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_catalog_resolver(
            tmp_path,
            [
                _model("gpt-5.5", "FLAGSHIP", priority=0),
                _model("gpt-5.4", "MID", priority=2),
                _model("gpt-5.4-mini", "MINI", priority=4),
            ],
        )
        client = CodexSubscriptionModelClient(model_name=None, store=store, resolver=resolver)
        # Lowest priority number wins → gpt-5.5 goes on the wire with its prompt.
        assert client.model_name == "gpt-5.5"
        assert client._codex_instructions == "FLAGSHIP"

    def test_unset_model_skips_deprecated_and_hidden_for_default(self, tmp_path: Path) -> None:
        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_catalog_resolver(
            tmp_path,
            [
                # Lowest number but hidden → must be ignored for the default.
                _model("codex-auto-review", priority=0, visibility="hide"),
                # Lower number than gpt-5.4 but deprecated → ignored.
                _model("gpt-5.3-codex", priority=1, upgrade_to="gpt-5.4"),
                _model("gpt-5.4", "PICK ME", priority=2),
            ],
        )
        client = CodexSubscriptionModelClient(model_name=None, store=store, resolver=resolver)
        assert client.model_name == "gpt-5.4"

    def test_deprecated_model_fails_fast(self, tmp_path: Path) -> None:
        """The reported bug: a configured, retired model must raise at
        construction (before any request) and name the replacement."""
        from calfkit_organization.providers.codex.prompts import DeprecatedCodexModelError

        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_catalog_resolver(
            tmp_path,
            [
                _model("gpt-5.3-codex", priority=6, upgrade_to="gpt-5.4"),
                _model("gpt-5.4", priority=2),
            ],
        )
        with pytest.raises(DeprecatedCodexModelError, match=r"gpt-5\.4"):
            CodexSubscriptionModelClient(
                model_name="gpt-5.3-codex", store=store, resolver=resolver
            )

    def test_unknown_model_fails_fast(self, tmp_path: Path) -> None:
        from calfkit_organization.providers.codex.prompts import UnknownCodexModelError

        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_catalog_resolver(tmp_path, [_model("gpt-5.4", priority=2)])
        with pytest.raises(UnknownCodexModelError, match="not a selectable model"):
            CodexSubscriptionModelClient(
                model_name="totally-made-up", store=store, resolver=resolver
            )

    def test_hidden_model_fails_fast(self, tmp_path: Path) -> None:
        """Fork B: an explicitly-configured internal model is not selectable."""
        from calfkit_organization.providers.codex.prompts import UnknownCodexModelError

        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_catalog_resolver(
            tmp_path,
            [
                _model("codex-auto-review", priority=0, visibility="hide"),
                _model("gpt-5.4", priority=2),
            ],
        )
        with pytest.raises(UnknownCodexModelError, match="internal model"):
            CodexSubscriptionModelClient(
                model_name="codex-auto-review", store=store, resolver=resolver
            )

    def test_forward_compatible_variant_prefix_matches_active(self, tmp_path: Path) -> None:
        """A configured ``gpt-5.5-codex`` not in the catalog still validates via
        the ``gpt-5.5`` prefix, sends that prompt, and keeps the configured
        slug on the wire (Fork C: prefix match preserved)."""
        store = _seed(tmp_path, account_id="x")
        resolver = _loaded_catalog_resolver(
            tmp_path, [_model("gpt-5.5", "FLAGSHIP PROMPT", priority=0)]
        )
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.5-codex", store=store, resolver=resolver
        )
        assert client.model_name == "gpt-5.5-codex"
        assert client._codex_instructions == "FLAGSHIP PROMPT"


class TestChatGPTUnsupportedGuard:
    """The request-time 400 backstop: a model the catalog still advertises but
    the backend has retired surfaces as ``CodexModelNotSupportedError``."""

    @pytest.mark.asyncio
    async def test_400_translated_to_not_supported_error(self, tmp_path: Path) -> None:
        from calfkit._vendor.pydantic_ai.exceptions import ModelHTTPError
        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        from calfkit_organization.providers.codex.model_client import (
            CodexModelNotSupportedError,
        )

        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex", store=store, resolver=_loaded_resolver(tmp_path)
        )

        async def _raise_unsupported(self, *, messages, stream, model_settings, model_request_parameters):
            raise ModelHTTPError(
                status_code=400,
                model_name="gpt-5.2-codex",
                body={
                    "detail": "The 'gpt-5.2-codex' model is not supported when "
                    "using Codex with a ChatGPT account."
                },
            )

        original = OpenAIResponsesModel._responses_create
        OpenAIResponsesModel._responses_create = _raise_unsupported
        try:
            with pytest.raises(CodexModelNotSupportedError, match="not available"):
                await client._responses_create(
                    messages=[],
                    stream=False,
                    model_settings=client.model_settings,
                    model_request_parameters=None,
                )
        finally:
            OpenAIResponsesModel._responses_create = original

    @pytest.mark.asyncio
    async def test_unrelated_400_propagates_unchanged(self, tmp_path: Path) -> None:
        """A different 400 (e.g. a real parameter error) must not be masked."""
        from calfkit._vendor.pydantic_ai.exceptions import ModelHTTPError
        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex", store=store, resolver=_loaded_resolver(tmp_path)
        )

        async def _raise_other(self, *, messages, stream, model_settings, model_request_parameters):
            raise ModelHTTPError(
                status_code=400,
                model_name="gpt-5.2-codex",
                body={"detail": "Unsupported parameter: max_output_tokens"},
            )

        original = OpenAIResponsesModel._responses_create
        OpenAIResponsesModel._responses_create = _raise_other
        try:
            with pytest.raises(ModelHTTPError, match="max_output_tokens"):
                await client._responses_create(
                    messages=[],
                    stream=True,
                    model_settings=client.model_settings,
                    model_request_parameters=None,
                )
        finally:
            OpenAIResponsesModel._responses_create = original

    @pytest.mark.asyncio
    async def test_400_translated_on_streaming_path(self, tmp_path: Path, monkeypatch) -> None:
        """The streaming caller path (request_stream) also gets the translated
        error, not the raw 400."""
        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        from calfkit_organization.providers.codex.model_client import (
            CodexModelNotSupportedError,
        )

        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex", store=store, resolver=_loaded_resolver(tmp_path)
        )
        monkeypatch.setattr(OpenAIResponsesModel, "_responses_create", _unsupported_raiser())
        with pytest.raises(CodexModelNotSupportedError):
            await client._responses_create(
                messages=[],
                stream=True,
                model_settings=client.model_settings,
                model_request_parameters=None,
            )

    @pytest.mark.asyncio
    async def test_error_message_names_catalog_default(self, tmp_path: Path, monkeypatch) -> None:
        """The translated error points the operator at the current default."""
        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        from calfkit_organization.providers.codex.model_client import (
            CodexModelNotSupportedError,
        )

        store = _seed(tmp_path, account_id="x")
        # gpt-5.5 is the highest-priority active model; gpt-5.2 is what we pin.
        resolver = _loaded_catalog_resolver(
            tmp_path, [_model("gpt-5.5", priority=0), _model("gpt-5.2", priority=5)]
        )
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex", store=store, resolver=resolver
        )
        monkeypatch.setattr(OpenAIResponsesModel, "_responses_create", _unsupported_raiser())
        with pytest.raises(CodexModelNotSupportedError, match=r"gpt-5\.5"):
            await client._responses_create(
                messages=[],
                stream=False,
                model_settings=client.model_settings,
                model_request_parameters=None,
            )

    @pytest.mark.asyncio
    async def test_error_message_falls_back_when_default_unavailable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If the catalog can't yield a default at error-build time (the
        `except (CodexModelError, RuntimeError)` branch), the message degrades
        gracefully to a generic phrase rather than masking the 400."""
        from calfkit._vendor.pydantic_ai.models.openai import OpenAIResponsesModel

        from calfkit_organization.providers.codex.model_client import (
            CodexModelNotSupportedError,
        )
        from calfkit_organization.providers.codex.prompt_cache import PromptCache
        from calfkit_organization.providers.codex.prompts import PromptResolver

        store = _seed(tmp_path, account_id="x")
        client = CodexSubscriptionModelClient(
            model_name="gpt-5.2-codex", store=store, resolver=_loaded_resolver(tmp_path)
        )
        # Swap in an UNLOADED resolver: default_slug() now raises RuntimeError,
        # which the guard swallows in favour of the generic phrase.
        client._resolver = PromptResolver(cache=PromptCache(base_dir=tmp_path / "empty"))
        monkeypatch.setattr(OpenAIResponsesModel, "_responses_create", _unsupported_raiser())
        with pytest.raises(CodexModelNotSupportedError, match="the current default"):
            await client._responses_create(
                messages=[],
                stream=False,
                model_settings=client.model_settings,
                model_request_parameters=None,
            )
