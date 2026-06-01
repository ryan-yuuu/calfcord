"""Tests for the Codex prompt fetcher + resolver."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from calfkit_organization.providers.codex import prompts as prompts_module
from calfkit_organization.providers.codex.prompt_cache import PromptCache
from calfkit_organization.providers.codex.prompts import (
    FALLBACK_PROMPT_URL,
    MODELS_JSON_URL,
    CodexPromptsUnavailableError,
    PromptResolver,
    get_default_resolver,
    prewarm_codex_prompts,
)

SAMPLE_MODELS_JSON: bytes = json.dumps(
    {
        "models": [
            {"slug": "gpt-5.2", "base_instructions": "GPT-5.2 PROMPT"},
            {"slug": "gpt-5.3-codex", "base_instructions": "GPT-5.3-CODEX PROMPT"},
        ]
    }
).encode()
SAMPLE_PROMPT_MD: bytes = b"FALLBACK PROMPT"


# ---------------------------------------------------------------------------
# Handler builders
# ---------------------------------------------------------------------------


def _build_default_handler(
    *,
    models_body: bytes = SAMPLE_MODELS_JSON,
    prompt_body: bytes = SAMPLE_PROMPT_MD,
    models_etag: str | None = 'W/"models-v1"',
    prompt_etag: str | None = 'W/"prompt-v1"',
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """Return a happy-path 200 handler plus a list capturing every request."""
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if str(request.url) == MODELS_JSON_URL:
            headers = {}
            if models_etag is not None:
                headers["etag"] = models_etag
            return httpx.Response(200, content=models_body, headers=headers)
        if str(request.url) == FALLBACK_PROMPT_URL:
            headers = {}
            if prompt_etag is not None:
                headers["etag"] = prompt_etag
            return httpx.Response(200, content=prompt_body, headers=headers)
        return httpx.Response(404, content=b"unexpected url")

    return handler, received


def _make_resolver(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[PromptResolver, PromptCache]:
    cache = PromptCache(base_dir=tmp_path / "cache")

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return PromptResolver(cache=cache, http_client_factory=factory), cache


@pytest.fixture(autouse=True)
def _reset_default_resolver():
    """Wipe the process singleton before and after every test."""
    prompts_module._default_resolver = None
    yield
    prompts_module._default_resolver = None


# ---------------------------------------------------------------------------
# Core resolver behaviour
# ---------------------------------------------------------------------------


class TestPromptResolver:
    async def test_resolve_exact_match(self, tmp_path: Path) -> None:
        handler, _ = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()

        assert resolver.resolve("gpt-5.2") == "GPT-5.2 PROMPT"
        assert resolver.resolve("gpt-5.3-codex") == "GPT-5.3-CODEX PROMPT"

    async def test_resolve_longest_prefix(self, tmp_path: Path) -> None:
        handler, _ = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()

        # "gpt-5.2-codex" doesn't have its own entry, but "gpt-5.2" prefix wins.
        assert resolver.resolve("gpt-5.2-codex") == "GPT-5.2 PROMPT"

    async def test_resolve_falls_back_to_prompt_md(self, tmp_path: Path) -> None:
        handler, _ = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()

        assert resolver.resolve("nonexistent-model") == "FALLBACK PROMPT"

    async def test_ensure_loaded_idempotent(self, tmp_path: Path) -> None:
        handler, received = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)

        await resolver.ensure_loaded()
        first_call_count = len(received)
        await resolver.ensure_loaded()

        assert len(received) == first_call_count

    async def test_concurrent_ensure_loaded_coalesces(self, tmp_path: Path) -> None:
        handler, received = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)

        await asyncio.gather(*(resolver.ensure_loaded() for _ in range(5)))

        # Exactly one request per upstream URL — the asyncio.Lock coalesces.
        urls = [str(r.url) for r in received]
        assert urls.count(MODELS_JSON_URL) == 1
        assert urls.count(FALLBACK_PROMPT_URL) == 1

    async def test_reset_clears_in_memory_state(self, tmp_path: Path) -> None:
        handler, _ = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)

        await resolver.ensure_loaded()
        assert resolver.is_loaded is True

        resolver.reset()
        assert resolver.is_loaded is False
        with pytest.raises(RuntimeError, match="before ensure_loaded"):
            resolver.resolve("gpt-5.2")

    async def test_resolve_before_load_raises(self, tmp_path: Path) -> None:
        handler, _ = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)

        with pytest.raises(RuntimeError, match="before ensure_loaded"):
            resolver.resolve("gpt-5.2")


# ---------------------------------------------------------------------------
# ETag / conditional GET
# ---------------------------------------------------------------------------


class TestEtagAndConditionalGet:
    async def test_uses_etag_for_conditional_get(self, tmp_path: Path) -> None:
        # First resolver: warm the cache with a 200 + etag.
        handler, _ = _build_default_handler()
        resolver_a, cache = _make_resolver(tmp_path, handler)
        await resolver_a.ensure_loaded()
        assert cache.load("models.json") is not None
        assert cache.load("models.json").etag == 'W/"models-v1"'

        # Second resolver shares the cache. Mock now always returns 304 to
        # asserts the conditional GET flow.
        received: list[httpx.Request] = []

        def handler_b(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return httpx.Response(304)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler_b))

        resolver_b = PromptResolver(cache=cache, http_client_factory=factory)
        await resolver_b.ensure_loaded()

        # Both upstream URLs should have been hit with the cached etag.
        for req in received:
            assert "If-None-Match" in req.headers, f"missing for {req.url}"
        models_req = next(r for r in received if str(r.url) == MODELS_JSON_URL)
        assert models_req.headers["If-None-Match"] == 'W/"models-v1"'

        # Resolver should still be able to serve prompts from the cache.
        assert resolver_b.resolve("gpt-5.2") == "GPT-5.2 PROMPT"

    async def test_304_uses_cached_body(self, tmp_path: Path) -> None:
        # Warm the cache first.
        handler, _ = _build_default_handler()
        resolver_a, cache = _make_resolver(tmp_path, handler)
        await resolver_a.ensure_loaded()

        def handler_304(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(304)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler_304))

        resolver_b = PromptResolver(cache=cache, http_client_factory=factory)
        await resolver_b.ensure_loaded()
        assert resolver_b.resolve("gpt-5.2") == "GPT-5.2 PROMPT"
        assert resolver_b.resolve("nonexistent") == "FALLBACK PROMPT"

    async def test_if_none_match_header_sent_when_cache_has_etag(self, tmp_path: Path) -> None:
        cache = PromptCache(base_dir=tmp_path / "cache")
        cache.save("models.json", SAMPLE_MODELS_JSON, etag="prepop-etag")
        cache.save("prompt.md", SAMPLE_PROMPT_MD, etag="prepop-fallback")

        received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received.append(request)
            return httpx.Response(304)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(cache=cache, http_client_factory=factory)
        await resolver.ensure_loaded()

        by_url = {str(r.url): r for r in received}
        assert by_url[MODELS_JSON_URL].headers["If-None-Match"] == "prepop-etag"
        assert by_url[FALLBACK_PROMPT_URL].headers["If-None-Match"] == "prepop-fallback"

    async def test_default_user_agent_header_sent(self, tmp_path: Path) -> None:
        handler, received = _build_default_handler()
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()

        assert received, "no requests captured"
        for req in received:
            assert "calfcord-codex-fetcher" in req.headers.get("User-Agent", "")


# ---------------------------------------------------------------------------
# Network + HTTP error fallbacks
# ---------------------------------------------------------------------------


class TestFailureFallback:
    async def test_network_error_falls_back_to_cache(self, tmp_path: Path) -> None:
        cache = PromptCache(base_dir=tmp_path / "cache")
        cache.save("models.json", SAMPLE_MODELS_JSON, etag="cached-etag")
        cache.save("prompt.md", SAMPLE_PROMPT_MD, etag="cached-fallback")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down", request=request)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(cache=cache, http_client_factory=factory)
        await resolver.ensure_loaded()

        assert resolver.is_loaded
        assert resolver.resolve("gpt-5.2") == "GPT-5.2 PROMPT"
        assert resolver.resolve("nonexistent") == "FALLBACK PROMPT"

    async def test_network_error_no_cache_raises(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down", request=request)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )

        with pytest.raises(CodexPromptsUnavailableError, match="no cache present"):
            await resolver.ensure_loaded()

    async def test_5xx_falls_back_to_cache(self, tmp_path: Path) -> None:
        cache = PromptCache(base_dir=tmp_path / "cache")
        cache.save("models.json", SAMPLE_MODELS_JSON, etag="cached-etag")
        cache.save("prompt.md", SAMPLE_PROMPT_MD, etag="cached-fallback")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"oops")

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(cache=cache, http_client_factory=factory)
        await resolver.ensure_loaded()
        assert resolver.resolve("gpt-5.2") == "GPT-5.2 PROMPT"

    async def test_5xx_no_cache_raises(self, tmp_path: Path) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"oops")

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        with pytest.raises(CodexPromptsUnavailableError, match="HTTP 503"):
            await resolver.ensure_loaded()


# ---------------------------------------------------------------------------
# Upstream parse failures
# ---------------------------------------------------------------------------


class TestUpstreamParseFailures:
    async def test_malformed_models_json_raises(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == MODELS_JSON_URL:
                return httpx.Response(200, content=b"not json")
            return httpx.Response(200, content=SAMPLE_PROMPT_MD)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        with pytest.raises(CodexPromptsUnavailableError, match="not valid JSON"):
            await resolver.ensure_loaded()

    async def test_empty_models_list_raises(self, tmp_path: Path) -> None:
        body = json.dumps({"models": []}).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == MODELS_JSON_URL:
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=SAMPLE_PROMPT_MD)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        with pytest.raises(CodexPromptsUnavailableError, match="no usable entries"):
            await resolver.ensure_loaded()

    async def test_models_field_not_a_list_raises(self, tmp_path: Path) -> None:
        body = json.dumps({"models": "not a list"}).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == MODELS_JSON_URL:
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=SAMPLE_PROMPT_MD)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        with pytest.raises(CodexPromptsUnavailableError, match="not a list"):
            await resolver.ensure_loaded()

    async def test_200_with_garbage_does_not_poison_existing_cache(
        self, tmp_path: Path
    ) -> None:
        """C2 regression: if upstream serves a 200 with malformed body
        (transient bad deploy, content-type mismatch, partial response),
        the fetcher must NOT overwrite the on-disk cache. Without the
        validator-before-save guard, the next ensure_loaded would send
        If-None-Match against the bad ETag, get 304, and re-raise — permanent
        breakage until manual ``calfkit-auth codex clear-prompts``.
        """
        cache = PromptCache(base_dir=tmp_path / "cache")
        # Pre-populate with a valid, parseable body + ETag.
        good_body = json.dumps(
            {"models": [{"slug": "gpt-5.2", "base_instructions": "GOOD CACHED"}]}
        ).encode()
        cache.save("models.json", good_body, etag="good-etag")
        cache.save("prompt.md", SAMPLE_PROMPT_MD, etag="prompt-etag")

        # Upstream returns 200 + garbage that would silently corrupt the cache.
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == MODELS_JSON_URL:
                return httpx.Response(
                    200, content=b"not json at all", headers={"etag": "evil-etag"}
                )
            return httpx.Response(304)  # prompt.md cache hit

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(cache=cache, http_client_factory=factory)
        # Should succeed by falling back to the prior cached body.
        await resolver.ensure_loaded()
        assert resolver.resolve("gpt-5.2") == "GOOD CACHED"

        # The disk cache must still hold the OLD body and OLD etag — the
        # garbage 200 must not have been written.
        reread = cache.load("models.json")
        assert reread is not None
        assert reread.body == good_body
        assert reread.etag == "good-etag"

    async def test_200_with_garbage_no_cache_raises(self, tmp_path: Path) -> None:
        """C2 regression: when no cache exists, a malformed upstream 200 must
        hard-fail at bootstrap rather than silently caching the garbage and
        raising at parse time (which would still leave the cache poisoned)."""
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == MODELS_JSON_URL:
                return httpx.Response(
                    200, content=b"<html>error page</html>", headers={"etag": "junk"}
                )
            return httpx.Response(200, content=SAMPLE_PROMPT_MD)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        cache = PromptCache(base_dir=tmp_path / "cache")
        resolver = PromptResolver(cache=cache, http_client_factory=factory)
        with pytest.raises(CodexPromptsUnavailableError, match="unusable body"):
            await resolver.ensure_loaded()
        # Cache must remain empty — no garbage written
        assert cache.load("models.json") is None

    async def test_models_entry_missing_fields_skipped(self, tmp_path: Path) -> None:
        body = json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.2", "base_instructions": "GOOD"},
                    {"slug": "gpt-5.3"},  # missing base_instructions
                    {"base_instructions": "ORPHAN"},  # missing slug
                    "not even a dict",
                ]
            }
        ).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == MODELS_JSON_URL:
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=SAMPLE_PROMPT_MD)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        await resolver.ensure_loaded()

        assert resolver.resolve("gpt-5.2") == "GOOD"
        assert resolver.resolve("gpt-5.3") == "FALLBACK PROMPT"


# ---------------------------------------------------------------------------
# Longest-prefix match table
# ---------------------------------------------------------------------------


class TestLongestPrefixMatch:
    @pytest.mark.parametrize(
        "model_name, slugs, expected",
        [
            (
                "gpt-5.2-codex",
                [("gpt-5.2", "GPT-5.2 PROMPT"), ("gpt-5", "GPT-5 PROMPT")],
                "GPT-5.2 PROMPT",
            ),
            (
                "gpt-5.3-codex",
                [("gpt-5.3-codex", "GPT-5.3-CODEX PROMPT"), ("gpt-5", "GPT-5 PROMPT")],
                "GPT-5.3-CODEX PROMPT",
            ),
            (
                "unknown-model",
                [("gpt-5.2", "GPT-5.2 PROMPT")],
                "FALLBACK PROMPT",
            ),
            (
                "gpt-5.2",
                [("gpt-5.2", "EXACT"), ("gpt-5.2-codex", "LONGER-BUT-NOT-PREFIX")],
                "EXACT",
            ),
        ],
    )
    async def test_longest_prefix(
        self,
        tmp_path: Path,
        model_name: str,
        slugs: list[tuple[str, str]],
        expected: str,
    ) -> None:
        body = json.dumps(
            {"models": [{"slug": s, "base_instructions": instr} for s, instr in slugs]}
        ).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == MODELS_JSON_URL:
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=SAMPLE_PROMPT_MD)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        await resolver.ensure_loaded()
        assert resolver.resolve(model_name) == expected


# ---------------------------------------------------------------------------
# prewarm + default singleton
# ---------------------------------------------------------------------------


class TestPrewarmCodexPrompts:
    async def test_loads_default_singleton(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler, _ = _build_default_handler()

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        # Pre-construct the singleton with our injected factory so prewarm
        # uses our mock rather than the real network.
        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        prompts_module._default_resolver = resolver

        await prewarm_codex_prompts()

        assert get_default_resolver().is_loaded is True
        assert get_default_resolver() is resolver

    async def test_raises_when_unavailable(
        self,
        tmp_path: Path,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resolver = PromptResolver(
            cache=PromptCache(base_dir=tmp_path / "cache"),
            http_client_factory=factory,
        )
        prompts_module._default_resolver = resolver

        with pytest.raises(CodexPromptsUnavailableError):
            await prewarm_codex_prompts()

    def test_get_default_resolver_caches(self) -> None:
        first = get_default_resolver()
        second = get_default_resolver()
        assert first is second
