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
    CodexModelError,
    CodexPromptsUnavailableError,
    DeprecatedCodexModelError,
    PromptResolver,
    UnknownCodexModelError,
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


# ---------------------------------------------------------------------------
# Catalog metadata: default selection + validation
# ---------------------------------------------------------------------------


# Mirrors the shape of the real openai/codex models.json: priority (0 = best),
# visibility ("list"/"hide"), and an "upgrade" pointer on retired models.
CATALOG_MODELS_JSON: bytes = json.dumps(
    {
        "models": [
            {
                "slug": "gpt-5.5",
                "base_instructions": "FLAGSHIP PROMPT",
                "display_name": "GPT-5.5",
                "priority": 0,
                "visibility": "list",
            },
            {
                "slug": "gpt-5.4",
                "base_instructions": "GPT-5.4 PROMPT",
                "priority": 2,
                "visibility": "list",
            },
            {
                "slug": "gpt-5.3-codex",
                "base_instructions": "RETIRED PROMPT",
                "priority": 6,
                "visibility": "list",
                "upgrade": {"model": "gpt-5.4", "migration_markdown": "..."},
            },
            {
                "slug": "codex-auto-review",
                "base_instructions": "INTERNAL PROMPT",
                "priority": 1,
                "visibility": "hide",
            },
        ]
    }
).encode()


def _catalog_resolver(tmp_path: Path) -> PromptResolver:
    handler, _ = _build_default_handler(models_body=CATALOG_MODELS_JSON)
    resolver, _ = _make_resolver(tmp_path, handler)
    return resolver


class TestCatalogMetadata:
    async def test_default_slug_is_highest_priority_active(self, tmp_path: Path) -> None:
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()
        # gpt-5.5 (priority 0) beats gpt-5.4; the hidden codex-auto-review
        # (priority 1) and deprecated gpt-5.3-codex are excluded entirely.
        assert resolver.default_slug() == "gpt-5.5"

    async def test_selectable_models_excludes_hidden_and_deprecated(
        self, tmp_path: Path
    ) -> None:
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()
        slugs = [m.slug for m in resolver.selectable_models()]
        assert slugs == ["gpt-5.5", "gpt-5.4"]  # priority order, no hide/deprecated

    async def test_validate_active_returns_entry(self, tmp_path: Path) -> None:
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()
        entry = resolver.validate("gpt-5.4")
        assert entry.slug == "gpt-5.4"
        assert entry.is_deprecated is False
        assert entry.is_selectable is True

    async def test_validate_deprecated_raises_with_upgrade(self, tmp_path: Path) -> None:
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()
        with pytest.raises(DeprecatedCodexModelError, match=r"gpt-5\.4") as exc:
            resolver.validate("gpt-5.3-codex")
        assert exc.value.upgrade_to == "gpt-5.4"

    async def test_validate_hidden_raises_unknown(self, tmp_path: Path) -> None:
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()
        with pytest.raises(UnknownCodexModelError, match="internal model"):
            resolver.validate("codex-auto-review")

    async def test_validate_unknown_raises(self, tmp_path: Path) -> None:
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()
        with pytest.raises(UnknownCodexModelError, match="not a selectable model"):
            resolver.validate("gpt-9-imaginary")

    async def test_validate_prefix_variant_inherits_active_entry(
        self, tmp_path: Path
    ) -> None:
        """A forward-compatible ``gpt-5.5-codex`` validates via the gpt-5.5
        prefix and inherits its (active) entry + prompt."""
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()
        entry = resolver.validate("gpt-5.5-codex")
        assert entry.slug == "gpt-5.5"
        assert resolver.resolve("gpt-5.5-codex") == "FLAGSHIP PROMPT"

    async def test_missing_priority_sorts_last(self, tmp_path: Path) -> None:
        """An entry without a priority field must not win the default by
        sorting as 0 — it should sort after explicitly-prioritized models."""
        body = json.dumps(
            {
                "models": [
                    {"slug": "gpt-no-prio", "base_instructions": "NO PRIO"},
                    {"slug": "gpt-best", "base_instructions": "BEST", "priority": 0},
                ]
            }
        ).encode()
        handler, _ = _build_default_handler(models_body=body)
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()
        assert resolver.default_slug() == "gpt-best"

    async def test_default_slug_raises_when_no_active_models(self, tmp_path: Path) -> None:
        body = json.dumps(
            {
                "models": [
                    {
                        "slug": "only-hidden",
                        "base_instructions": "X",
                        "visibility": "hide",
                    }
                ]
            }
        ).encode()
        handler, _ = _build_default_handler(models_body=body)
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()
        with pytest.raises(CodexModelError, match="No active Codex models"):
            resolver.default_slug()

    async def test_query_before_load_raises(self, tmp_path: Path) -> None:
        resolver = _catalog_resolver(tmp_path)
        for call in (resolver.default_slug, lambda: resolver.validate("gpt-5.5")):
            with pytest.raises(RuntimeError, match="before ensure_loaded"):
                call()

    async def test_validate_prefix_variant_of_deprecated_fails_with_matched_entry(
        self, tmp_path: Path
    ) -> None:
        """The longest-prefix + deprecation interaction: a configured string that
        only *prefix*-matches a deprecated entry must fail, and the message must
        name the matched catalog entry (the `via` branch of the error)."""
        body = json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.2",
                        "base_instructions": "X",
                        "priority": 4,
                        "upgrade": {"model": "gpt-5.4"},
                    },
                    {"slug": "gpt-5.4", "base_instructions": "Y", "priority": 2},
                ]
            }
        ).encode()
        handler, _ = _build_default_handler(models_body=body)
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()
        # "gpt-5.2-codex" is not itself a slug; it longest-prefix-matches the
        # deprecated "gpt-5.2" entry and must be rejected.
        with pytest.raises(DeprecatedCodexModelError, match="matched catalog entry") as exc:
            resolver.validate("gpt-5.2-codex")
        assert exc.value.matched_slug == "gpt-5.2"
        assert exc.value.upgrade_to == "gpt-5.4"

    async def test_unknown_vs_hidden_carry_matched_slug_discriminator(
        self, tmp_path: Path
    ) -> None:
        """UnknownCodexModelError distinguishes no-match (matched_slug=None) from
        a hidden match (matched_slug set) without parsing the message."""
        resolver = _catalog_resolver(tmp_path)
        await resolver.ensure_loaded()

        with pytest.raises(UnknownCodexModelError) as no_match:
            resolver.validate("gpt-9-imaginary")
        assert no_match.value.matched_slug is None

        with pytest.raises(UnknownCodexModelError) as hidden:
            resolver.validate("codex-auto-review")
        assert hidden.value.matched_slug == "codex-auto-review"

    async def test_bool_priority_is_rejected_and_sorts_last(self, tmp_path: Path) -> None:
        """A stray ``"priority": true`` must not serialise to 1 and win the
        default; it is treated as missing (sorts last)."""
        body = json.dumps(
            {
                "models": [
                    {"slug": "gpt-bool", "base_instructions": "BOOL", "priority": True},
                    {"slug": "gpt-best", "base_instructions": "BEST", "priority": 0},
                ]
            }
        ).encode()
        handler, _ = _build_default_handler(models_body=body)
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()
        assert resolver.default_slug() == "gpt-best"

    async def test_default_slug_tie_break_by_slug(self, tmp_path: Path) -> None:
        """Equal priority → alphabetical slug order decides (stable default)."""
        body = json.dumps(
            {
                "models": [
                    {"slug": "gpt-zeta", "base_instructions": "Z", "priority": 0},
                    {"slug": "gpt-alpha", "base_instructions": "A", "priority": 0},
                ]
            }
        ).encode()
        handler, _ = _build_default_handler(models_body=body)
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()
        assert resolver.default_slug() == "gpt-alpha"

    async def test_empty_slug_entry_is_skipped(self, tmp_path: Path) -> None:
        """An empty-string slug would prefix-match every model name; it must be
        dropped, not stored (else it shadows the unknown-model path)."""
        body = json.dumps(
            {
                "models": [
                    {"slug": "", "base_instructions": "EMPTY"},
                    {"slug": "gpt-real", "base_instructions": "REAL", "priority": 0},
                ]
            }
        ).encode()
        handler, _ = _build_default_handler(models_body=body)
        resolver, _ = _make_resolver(tmp_path, handler)
        await resolver.ensure_loaded()
        assert [m.slug for m in resolver.selectable_models()] == ["gpt-real"]
        # The empty slug must NOT have been stored as a catch-all match.
        with pytest.raises(UnknownCodexModelError):
            resolver.validate("anything-unknown")
