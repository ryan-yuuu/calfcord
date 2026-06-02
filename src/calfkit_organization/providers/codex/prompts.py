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
from dataclasses import dataclass
from typing import Final, Literal

import httpx

from calfkit_organization.providers.codex.prompt_cache import PromptCache

logger = logging.getLogger(__name__)

# Visibility domain from models.json. Only "list" (user-selectable) and "hide"
# (internal, e.g. codex-auto-review) are meaningful; the parser maps any
# unrecognised upstream value to "hide" so a new value fails closed.
Visibility = Literal["list", "hide"]

# Sentinel priority for catalog entries whose upstream JSON omits ``priority``.
# Real ``models.json`` entries always carry a small integer (0 = flagship);
# a missing value sorts the model *last* in the ascending ``(priority, slug)``
# order used by ``selectable_models``/``default_slug`` rather than tying at 0
# and beating real flagships.
_MISSING_PRIORITY: Final[int] = 1_000_000

# Marker returned by ``_safe_default_slug`` (log-only) when no active model
# exists, so the load path can detect and WARN on the degraded state.
_NO_DEFAULT_MARKER: Final[str] = "<none>"

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


class _BodyValidationError(RuntimeError):
    """Internal: raised by ``_validate_body`` when an upstream 200 body is
    structurally unusable. Never escapes ``_fetch_one`` — it's translated to
    either a cache fallback (when cache is present) or
    ``CodexPromptsUnavailableError`` (when cache is empty).
    """


@dataclass(frozen=True)
class CodexModel:
    """A single Codex model parsed from ``models.json``.

    Carries only the fields we act on: ``base_instructions`` (the
    fingerprinted system prompt), ``priority`` (lower = preferred; 0 is the
    flagship), ``visibility`` (``"list"`` = user-selectable, ``"hide"`` =
    internal), and ``upgrade_to`` (the slug that supersedes this one, or
    ``None`` when the model is current). The remaining ~30 fields in the
    upstream JSON are intentionally dropped — we re-fetch live, so there's
    no value in mirroring the full schema.
    """

    slug: str
    base_instructions: str
    priority: int = _MISSING_PRIORITY
    visibility: Visibility = "list"
    upgrade_to: str | None = None

    @property
    def is_selectable(self) -> bool:
        """True when the model is meant for user selection (``visibility == "list"``).

        Hidden models (e.g. ``codex-auto-review``) are internal and not valid
        targets for an agent's ``model:`` field.
        """
        return self.visibility == "list"

    @property
    def is_deprecated(self) -> bool:
        """True when upstream marks the model superseded (``upgrade`` set).

        A deprecated model carries an ``upgrade.model`` pointer to its
        replacement; the Codex backend rejects such models for
        ChatGPT-account auth, which is the exact failure this catalog guards
        against.
        """
        return self.upgrade_to is not None


class CodexModelError(RuntimeError):
    """Base class for catalog-driven model validation failures.

    Raised from :meth:`PromptResolver.validate` / :meth:`default_slug` at
    model-client construction time — before any request reaches the Codex
    backend — so a misconfigured ``model:`` fails fast with an actionable
    message instead of a raw ``400`` deep in the request path.
    """


class UnknownCodexModelError(CodexModelError):
    """The configured model is not a selectable entry in the live catalog.

    Covers two distinct cases, distinguishable via ``matched_slug`` so a caller
    can branch without parsing the message:

    * ``matched_slug is None`` — no catalog slug is a prefix of the configured
      model (a genuine typo / made-up model);
    * ``matched_slug`` set — the model matched a real but hidden (internal)
      entry, e.g. ``codex-auto-review`` (``visibility != "list"``).
    """

    def __init__(self, model_name: str, selectable: list[str], *, matched_slug: str | None = None):
        self.model_name = model_name
        self.selectable = selectable
        self.matched_slug = matched_slug
        detail = (
            f" (matched internal model {matched_slug!r}, which is not user-selectable)"
            if matched_slug is not None
            else ""
        )
        super().__init__(
            f"Codex model {model_name!r} is not a selectable model in the live "
            f"catalog{detail}. Selectable models: {selectable or '<none>'}. Set the "
            f"agent's `model:` to one of these, or unset `model:` to use the "
            f"highest-priority default."
        )


class DeprecatedCodexModelError(CodexModelError):
    """The configured model has been retired upstream (superseded).

    The Codex backend rejects retired models for ChatGPT-account auth, so we
    refuse them at construction and point the operator at the replacement.
    """

    def __init__(self, model_name: str, matched_slug: str, upgrade_to: str):
        self.model_name = model_name
        self.matched_slug = matched_slug
        self.upgrade_to = upgrade_to
        # When the configured string isn't itself a catalog slug (prefix
        # match), name the entry we matched so the cause is unambiguous.
        via = "" if model_name == matched_slug else f" (matched catalog entry {matched_slug!r})"
        super().__init__(
            f"Codex model {model_name!r}{via} has been retired by OpenAI and "
            f"superseded by {upgrade_to!r}; the Codex backend rejects it for "
            f"ChatGPT-account auth. Update the agent's `model:` to {upgrade_to!r}, "
            f"or unset `model:` to use the current highest-priority default."
        )


def _validate_body(cache_name: str, body: bytes) -> None:
    """Verify a freshly fetched upstream body is structurally usable.

    For ``models.json`` we parse and assert the top-level shape we depend on —
    so a 200 response carrying garbage (caching layer mishap, content-type
    mismatch, partial body) is rejected before it can poison the disk cache.
    For ``prompt.md`` we only check it decodes as UTF-8 and isn't empty.
    """
    if cache_name == _MODELS_CACHE_NAME:
        try:
            doc = json.loads(body)
        except json.JSONDecodeError as exc:
            raise _BodyValidationError(f"not valid JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise _BodyValidationError("top-level JSON is not an object")
        if not isinstance(doc.get("models"), list):
            raise _BodyValidationError("'models' field is missing or not a list")
        return
    if cache_name == _FALLBACK_CACHE_NAME:
        if not body:
            raise _BodyValidationError("body is empty")
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _BodyValidationError(f"not valid UTF-8: {exc}") from exc
        return


def _parse_model_entry(entry: dict) -> CodexModel | None:
    """Build a :class:`CodexModel` from one ``models.json`` entry, or ``None``.

    A usable entry must carry a non-empty ``slug`` and ``base_instructions``
    (same requirement as before catalog metadata existed); entries missing
    either are skipped with a WARNING. An empty ``slug`` is rejected too: it
    would prefix-match *every* model name in :meth:`PromptResolver._match`
    (``"x".startswith("")`` is always true) and silently shadow the
    unknown-model path.

    All other fields are optional and fall back to the :class:`CodexModel`
    defaults; a value present but of the wrong shape is coerced to the default
    and logged, so a parse regression is visible in startup logs rather than
    silently dropping a model or flipping its selectability.
    """
    slug = entry.get("slug")
    instructions = entry.get("base_instructions")
    if not (isinstance(slug, str) and slug and isinstance(instructions, str) and instructions):
        logger.warning(
            "Skipping models.json entry missing/empty slug or base_instructions: keys=%s",
            sorted(entry),
        )
        return None

    # ``bool`` is an ``int`` subclass, so ``isinstance(True, int)`` is True;
    # ``type(x) is int`` rejects a stray ``true`` that would serialise to 1 and
    # skew default selection. ``json.loads`` only ever yields plain ``int``.
    raw_priority = entry.get("priority")
    priority = raw_priority if type(raw_priority) is int else _MISSING_PRIORITY
    if raw_priority is not None and priority is _MISSING_PRIORITY:
        logger.warning("Model %r: non-int priority %r; sorting last", slug, raw_priority)

    # Absent visibility defaults to "list" (selectable), matching the
    # CodexModel default. A *present* but unrecognised value (a future upstream
    # value, or a non-string) fails closed to "hide" so it can't accidentally
    # become user-selectable.
    raw_visibility = entry.get("visibility")
    if raw_visibility is None:
        visibility: Visibility = "list"
    elif raw_visibility in ("list", "hide"):
        visibility = raw_visibility
    else:
        visibility = "hide"
        logger.warning("Model %r: unrecognised visibility %r; treating as 'hide'", slug, raw_visibility)

    upgrade = entry.get("upgrade")
    upgrade_model = upgrade.get("model") if isinstance(upgrade, dict) else None
    upgrade_to = upgrade_model if isinstance(upgrade_model, str) and upgrade_model else None

    return CodexModel(
        slug=slug,
        base_instructions=instructions,
        priority=priority,
        visibility=visibility,
        upgrade_to=upgrade_to,
    )


class PromptResolver:
    """In-memory catalog of Codex models parsed from ``models.json``.

    Despite the name (kept for API stability), this owns the full per-model
    catalog, not just prompts. Construct once per process (typically via
    :func:`get_default_resolver`), call ``await ensure_loaded()`` during
    startup, then call the synchronous query methods from request/construction
    paths:

    * :meth:`resolve` — the verbatim (longest-prefix) Codex CLI prompt;
    * :meth:`validate` — fail-fast check that a configured model is selectable
      and not deprecated, returning its :class:`CodexModel`;
    * :meth:`default_slug` — the highest-priority selectable model, used when
      an agent leaves ``model:`` unset.

    ``ensure_loaded`` is :class:`asyncio.Lock`-guarded so concurrent callers
    coalesce. The query methods are lock-free; post-load state is treated as
    immutable for the lifetime of the resolver (call :meth:`reset` then
    ``ensure_loaded`` again to refresh).
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
        self._catalog: dict[str, CodexModel] = {}
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

            parsed: dict[str, CodexModel] = {}
            skipped = 0
            for entry in entries:
                model = _parse_model_entry(entry) if isinstance(entry, dict) else None
                if model is None:
                    skipped += 1
                    continue
                parsed[model.slug] = model

            if skipped:
                # Per-entry detail is logged in _parse_model_entry; this is the
                # aggregate so a parse regression is visible at a glance.
                logger.warning("models.json: skipped %d of %d entries", skipped, len(entries))

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

            self._catalog = parsed
            self._fallback_prompt = fallback_text
            self._loaded = True
            default = self._safe_default_slug()
            logger.info(
                "Loaded %d Codex models + %d-byte fallback prompt from openai/codex "
                "(default=%s)",
                len(parsed),
                len(self._fallback_prompt),
                default,
            )
            if default == _NO_DEFAULT_MARKER:
                # Loaded fine, but every model is hidden or deprecated — an agent
                # with ``model:`` unset will hard-fail later in default_slug().
                # Surface the degraded state loudly at load, not at next use.
                logger.warning(
                    "Codex catalog has no selectable, non-deprecated model; "
                    "agents with `model:` unset will fail at construction."
                )

    def reset(self) -> None:
        """Drop in-memory state. Does NOT clear the disk cache.

        Next :meth:`ensure_loaded` call will re-fetch from upstream
        (with conditional GETs that may yield ``304`` and reuse the
        cached bodies).
        """
        self._loaded = False
        self._catalog = {}
        self._fallback_prompt = ""

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("PromptResolver used before ensure_loaded()")

    def _match(self, model_name: str) -> CodexModel | None:
        """Longest-prefix match of ``model_name`` against catalog slugs.

        ``gpt-5.2-codex`` prefers a ``gpt-5.2-codex`` entry, falling back to
        ``gpt-5.2`` then ``gpt-5``. Returns ``None`` when no slug is a prefix
        of ``model_name``.
        """
        matches = [slug for slug in self._catalog if model_name.startswith(slug)]
        if not matches:
            return None
        return self._catalog[max(matches, key=len)]

    def resolve(self, model_name: str) -> str:
        """Return the verbatim Codex CLI prompt for ``model_name``.

        Matching is longest-prefix over the slugs in ``models.json``:
        ``gpt-5.2-codex`` will prefer a ``gpt-5.2-codex`` entry, fall back
        to ``gpt-5.2``, then ``gpt-5``, etc. When no slug matches, the
        contents of ``prompt.md`` are returned.

        OpenAI fingerprints ``instructions`` against the official strings, so
        the prefix-matched prompt of a *close* model is preferable to a
        branded short string even when the exact slug isn't in the catalog —
        which is why this stays a prefix match rather than an exact lookup.
        """
        self._require_loaded()
        entry = self._match(model_name)
        return entry.base_instructions if entry is not None else self._fallback_prompt

    def selectable_models(self) -> list[CodexModel]:
        """Return the user-selectable, non-deprecated models, best-first.

        Sorted by ``priority`` (ascending; 0 = flagship) then ``slug`` for a
        stable order. This is the set an operator may legitimately pin an
        agent's ``model:`` to, and the pool :meth:`default_slug` chooses from.
        """
        self._require_loaded()
        active = [m for m in self._catalog.values() if m.is_selectable and not m.is_deprecated]
        return sorted(active, key=lambda m: (m.priority, m.slug))

    def default_slug(self) -> str:
        """Return the highest-priority selectable, non-deprecated model slug.

        Used when an agent leaves ``model:`` unset for the Codex provider:
        rather than hard-coding a default that rots when OpenAI retires a
        model, we pick the current flagship from the live catalog.

        Raises:
            CodexModelError: when the catalog has no active models (every
                entry is hidden or deprecated) — there is nothing safe to
                default to.
        """
        active = self.selectable_models()
        if not active:
            raise CodexModelError(
                "No active Codex models in the live catalog (all hidden or "
                "deprecated); cannot select a default model."
            )
        return active[0].slug

    def validate(self, model_name: str) -> CodexModel:
        """Resolve + validate ``model_name`` against the live catalog.

        Returns the matched :class:`CodexModel` when the model is usable.
        Raises (fail-fast at construction, before any request) when it is not:

        * :class:`UnknownCodexModelError` — no catalog slug is a prefix of
          ``model_name``, or the matched entry is hidden (internal).
        * :class:`DeprecatedCodexModelError` — the matched entry has been
          superseded upstream (the Codex backend would reject it).

        Matching is the same longest-prefix logic :meth:`resolve` uses, so a
        forward-compatible ``gpt-5.5-codex`` validates against (and inherits
        the instructions of) a ``gpt-5.5`` catalog entry.
        """
        self._require_loaded()
        entry = self._match(model_name)
        # Order matters: an unknown/hidden model is reported as "not selectable"
        # before the deprecation check, so a hidden+deprecated entry surfaces as
        # UnknownCodexModelError (hidden is the more fundamental disqualifier).
        if entry is None or not entry.is_selectable:
            raise UnknownCodexModelError(
                model_name,
                [m.slug for m in self.selectable_models()],
                matched_slug=entry.slug if entry is not None else None,
            )
        if entry.upgrade_to is not None:  # i.e. is_deprecated — superseded upstream
            raise DeprecatedCodexModelError(model_name, entry.slug, entry.upgrade_to)
        return entry

    def _safe_default_slug(self) -> str:
        """``default_slug`` for log lines: never raises (returns a marker)."""
        try:
            return self.default_slug()
        except CodexModelError:
            return _NO_DEFAULT_MARKER

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
        # Validate body shape BEFORE writing to disk. Otherwise an upstream blip
        # that returns 200 with malformed content (transient bad deploy, content
        # type mismatch, edge cache serving stale fragments) would poison the
        # cache, and the next ensure_loaded would 304 against the bad ETag and
        # re-raise — permanent breakage until `calfkit-auth codex clear-prompts`.
        try:
            _validate_body(cache_name, body)
        except _BodyValidationError as exc:
            if cached is not None:
                logger.error(
                    "Upstream %s returned 200 but body is unusable (%s); "
                    "keeping prior cached body, NOT overwriting cache",
                    url, exc,
                )
                return cached.body
            raise CodexPromptsUnavailableError(
                f"Upstream {url} returned 200 with unusable body and no cache: {exc}"
            ) from exc
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
