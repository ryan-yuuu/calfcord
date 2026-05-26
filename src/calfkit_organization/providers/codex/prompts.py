"""Live fetcher + resolver for the verbatim openai/codex system prompts.

OpenAI fingerprints the ``instructions`` string sent to
``chatgpt.com/backend-api/codex/responses`` (see ``openai/codex`` issue
#4433). To survive the check we must ship the exact ``base_instructions``
string for the requested model slug, sourced from the official
``codex-rs/models-manager/models.json``.

This module fetches that JSON (plus the legacy ``prompt.md`` fallback)
from the GitHub raw URLs on the ``main`` branch, caches them under
:class:`PromptCache`, and exposes a resolver keyed by model slug using a
longest-prefix match (so ``gpt-5.2-codex`` correctly inherits the
``gpt-5.2`` prompt when a more specific entry doesn't exist).

Lifecycle::

    resolver = get_default_resolver()
    await resolver.ensure_loaded()
    instructions = resolver.resolve("gpt-5.2")

``ensure_loaded`` is idempotent and asyncio-lock-guarded so concurrent
callers (e.g. tools + agent runners) coalesce into a single fetch.
Network errors are tolerated when a cache is present; a hard failure
(``CodexPromptsUnavailableError``) is raised only when both upstream is
unreachable AND no cached copy exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from typing import Final

import httpx

from calfkit_organization.providers.codex.prompt_cache import PromptCache

logger = logging.getLogger(__name__)

MODELS_JSON_URL: Final[str] = (
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json"
)
FALLBACK_PROMPT_URL: Final[str] = (
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/prompt.md"
)

_USER_AGENT: Final[str] = "calfcord-codex-fetcher/0.1"
_HTTP_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(30.0, connect=10.0)

_MODELS_CACHE_NAME: Final[str] = "models.json"
_FALLBACK_CACHE_NAME: Final[str] = "prompt.md"


class CodexPromptsUnavailableError(RuntimeError):
    """Raised when upstream fetch fails AND no usable cache exists.

    The fetcher tolerates network errors and non-200 responses whenever
    a cached body is present on disk. This exception only surfaces when
    we have neither a fresh response nor a fallback to fall back to —
    e.g. first-run on a host that's offline.
    """


class PromptResolver:
    """In-memory store of Codex CLI system prompts.

    Construct once per process (typically via :func:`get_default_resolver`),
    call ``await ensure_loaded()`` during startup, then call
    :meth:`resolve` synchronously from request paths.

    ``ensure_loaded`` is :class:`asyncio.Lock`-guarded so concurrent
    callers coalesce. :meth:`resolve` is lock-free; post-load state is
    treated as immutable for the lifetime of the resolver (call
    :meth:`reset` then ``ensure_loaded`` again to refresh).
    """

    def __init__(
        self,
        cache: PromptCache,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._cache = cache
        self._http_client_factory = http_client_factory or self._default_http_client
        self._lock = asyncio.Lock()
        self._loaded = False
        self._models: dict[str, str] = {}
        self._fallback_prompt: str = ""

    @staticmethod
    def _default_http_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def ensure_loaded(self) -> None:
        """Fetch + parse upstream prompts, populating the in-memory state.

        Idempotent: a second call after a successful load is a no-op.
        Concurrent callers coalesce on the internal asyncio.Lock so the
        upstream fetch happens at most once per resolver instance per
        reset cycle.

        Raises:
            CodexPromptsUnavailableError: when upstream is unreachable
                and no cached copy is on disk, or when upstream returns
                content we cannot parse.
        """
        async with self._lock:
            if self._loaded:
                return

            models_body = await self._fetch_one(MODELS_JSON_URL, _MODELS_CACHE_NAME)
            fallback_body = await self._fetch_one(FALLBACK_PROMPT_URL, _FALLBACK_CACHE_NAME)

            try:
                doc = json.loads(models_body)
            except json.JSONDecodeError as exc:
                raise CodexPromptsUnavailableError(
                    f"Upstream models.json is not valid JSON: {exc}"
                ) from exc

            if not isinstance(doc, dict):
                raise CodexPromptsUnavailableError(
                    "models.json: top-level value is not an object"
                )

            entries = doc.get("models", [])
            if not isinstance(entries, list):
                raise CodexPromptsUnavailableError("models.json: 'models' is not a list")

            parsed: dict[str, str] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                slug = entry.get("slug")
                instructions = entry.get("base_instructions")
                if isinstance(slug, str) and isinstance(instructions, str):
                    parsed[slug] = instructions

            if not parsed:
                raise CodexPromptsUnavailableError(
                    "models.json has no usable entries (slug + base_instructions)"
                )

            try:
                fallback_text = fallback_body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CodexPromptsUnavailableError(
                    f"Upstream prompt.md is not valid UTF-8: {exc}"
                ) from exc

            self._models = parsed
            self._fallback_prompt = fallback_text
            self._loaded = True
            logger.info(
                "Loaded %d Codex model prompts + %d-byte fallback from openai/codex",
                len(parsed),
                len(self._fallback_prompt),
            )

    def reset(self) -> None:
        """Drop in-memory state. Does NOT clear the disk cache.

        Next :meth:`ensure_loaded` call will re-fetch from upstream
        (with conditional GETs that may yield ``304`` and reuse the
        cached bodies).
        """
        self._loaded = False
        self._models = {}
        self._fallback_prompt = ""

    def resolve(self, model_name: str) -> str:
        """Return the verbatim Codex CLI prompt for ``model_name``.

        Matching is longest-prefix over the slugs in ``models.json``:
        ``gpt-5.2-codex`` will prefer a ``gpt-5.2-codex`` entry, fall back
        to ``gpt-5.2``, then ``gpt-5``, etc. When no slug matches, the
        contents of ``prompt.md`` are returned.
        """
        if not self._loaded:
            raise RuntimeError("PromptResolver.resolve() called before ensure_loaded()")
        matches = [slug for slug in self._models if model_name.startswith(slug)]
        if matches:
            return self._models[max(matches, key=len)]
        return self._fallback_prompt

    async def _fetch_one(self, url: str, cache_name: str) -> bytes:
        """Fetch ``url`` with ETag-conditional GET against the disk cache.

        Failure handling matrix:

        ============ ===== =====================================
        Cache state  HTTP  Behaviour
        ============ ===== =====================================
        present      200   save fresh body + etag, return body
        present      304   touch cache (refresh ``fetched_at``),
                           return cached body
        present      5xx   log + return cached body
        present      err   log + return cached body
        absent       200   save fresh body, return body
        absent       304   raise (impossible without prior cache)
        absent       5xx   raise CodexPromptsUnavailableError
        absent       err   raise CodexPromptsUnavailableError
        ============ ===== =====================================
        """
        cached = self._cache.load(cache_name)
        headers = {"User-Agent": _USER_AGENT}
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag

        try:
            async with self._http_client_factory() as client:
                resp = await client.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        except (httpx.HTTPError, OSError) as exc:
            if cached is not None:
                logger.warning(
                    "Network error fetching %s (%s); using cached body", url, exc
                )
                return cached.body
            raise CodexPromptsUnavailableError(
                f"Failed to fetch {url} and no cache present: {exc}"
            ) from exc

        if resp.status_code == 304:
            if cached is None:
                # We didn't send If-None-Match, so a 304 means the upstream
                # is misbehaving (or the cache was cleared mid-flight).
                raise CodexPromptsUnavailableError(
                    f"Got 304 from {url} but cache is empty (concurrent clear?)"
                )
            # Refresh ``fetched_at`` by re-saving the same body + etag.
            self._cache.save(cache_name, cached.body, cached.etag)
            return cached.body

        if resp.status_code != 200:
            if cached is not None:
                logger.warning(
                    "HTTP %d fetching %s; using cached body", resp.status_code, url
                )
                return cached.body
            raise CodexPromptsUnavailableError(
                f"HTTP {resp.status_code} fetching {url} and no cache present"
            )

        body = resp.content
        etag = resp.headers.get("etag")
        self._cache.save(cache_name, body, etag)
        return body


# ---------------------------------------------------------------------------
# Process-wide default singleton
# ---------------------------------------------------------------------------

_default_resolver: PromptResolver | None = None
_default_resolver_lock = threading.Lock()


def get_default_resolver(*, cache: PromptCache | None = None) -> PromptResolver:
    """Return the process singleton, constructing it on first call.

    ``cache`` is only honoured on first call; subsequent calls return
    the previously-constructed resolver regardless. Tests that need a
    fresh singleton should reset ``prompts._default_resolver`` to
    ``None`` between cases.
    """
    global _default_resolver
    with _default_resolver_lock:
        if _default_resolver is None:
            _default_resolver = PromptResolver(cache=cache or PromptCache())
        return _default_resolver


async def prewarm_codex_prompts() -> None:
    """Initialise the default resolver. Call from worker startup.

    Invoke once during ``_amain`` (runner/tools) before constructing any
    :class:`CodexSubscriptionModelClient` so the in-memory prompts are
    ready by the time the first inference request fires.
    """
    await get_default_resolver().ensure_loaded()
