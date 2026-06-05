"""Provider helpers shared by the interactive setup wizards.

The ``calfcord init`` flow (and the Round-2 per-agent wizards built on top of
it) need three provider-specific operations that all touch heavy, optional
dependencies — the OpenAI / Anthropic SDKs, the live Codex catalog, and the
ChatGPT-subscription OAuth machinery. Putting them here, behind a small
synchronous API, keeps every one of those imports *lazy and function-local* so:

* merely importing this module (which the argparse entry point and the test
  suite both do at startup) never pulls in an SDK, opens a network socket, or
  needs a TTY;
* the wizard callers depend only on :class:`~calfcord.cli._prompts.Prompter`
  and :class:`~calfcord.cli._prompts.Choice` — they never ``import openai`` /
  ``anthropic`` / ``openhands`` themselves, so a host missing one provider's
  SDK can still run the wizard for another provider.

The two design rules the wizards rely on:

* **Always select, never free-text a model.** A live, filtered model list is
  fetched per provider; a curated fallback is offered only when the live fetch
  fails, so an operator can never type a slug that the provider will reject.
* **Never abort the wizard on an auth/network hiccup.** A failed live fetch
  degrades to the fallback list with a warning; a failed Codex login prints a
  resume hint and returns. The wizard must always reach the point where it
  writes config.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NamedTuple

from calfcord.cli._envfile import upsert
from calfcord.cli._prompts import Choice, Prompter


class ProviderModel(NamedTuple):
    """A resolved ``(provider, model)`` pair returned by :func:`configure_provider`.

    A named pair, not a bare ``tuple[str, str]``: both elements are strings, so a
    positional swap (``model, provider = ...``) would be silent — callers read
    ``.provider`` / ``.model``. Mirrors the :class:`~calfcord.cli.agent_create.CreatedAgent`
    precedent. Unpacking (``provider, model = configure_provider(...)``) still works.
    """

    provider: str
    model: str

# Substrings that mark a model as the cheap/fast tier of its family. Used to
# pick a sensible default when a wizard step is flagged ``cheap=True`` (e.g. a
# router or a summariser, where the flagship is overkill). Order is irrelevant —
# the first *available* model id containing any of these wins.
_CHEAP_HINTS = ("haiku", "nano", "mini", "flash", "lite")

# Provider env-var conventions. ``openai-codex`` is intentionally absent: it
# authenticates via the OAuth flow in :func:`_codex_login`, not a key in ``.env``.
_PROVIDER_KEY_VAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# The selectable providers, with operator-facing labels, shared by every wizard
# (`calfcord init`, `calfcord router setup`). Values are the ``provider:`` /
# ``CALFKIT_AGENT_DEFAULT_PROVIDER`` literals; the order is the menu order.
PROVIDERS: list[Choice] = [
    Choice("anthropic", "Anthropic"),
    Choice("openai", "OpenAI"),
    Choice("openai-codex", "Codex subscription"),
]

# OpenAI's ``models.list()`` returns the *entire* account catalog — embeddings,
# TTS/STT, image, moderation, realtime, and legacy completion models alongside
# the chat models. There is no "kind" field to filter on, so we gate on the id:
# an allowed family prefix AND no disqualifying substring. Kept as data (not
# inline) so the test can assert against the exact same set the filter uses.
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_OPENAI_NON_CHAT_MARKERS = (
    "embedding",
    "tts",
    "whisper",
    "audio",
    "realtime",
    "transcribe",
    "image",
    "dall-e",
    "moderation",
    "search",
    "instruct",
    "codex",
    "babbage",
    "davinci",
)


class ModelListError(RuntimeError):
    """A live model fetch failed (network, auth, or SDK error).

    One exception type for every provider branch so callers can ``except
    ModelListError`` once and fall back to the curated list without caring
    which SDK raised underneath.
    """


class ModelAuthError(ModelListError):
    """The live model fetch failed *specifically* because the API key was rejected.

    A subclass of :class:`ModelListError` so the existing fall-back-to-curated
    handler still catches it, but distinct enough that :func:`pick_model` can
    first print a LOUD, actionable warning: a rejected key means the agent will
    not work at runtime, which a generic "couldn't fetch live models" line (the
    same line an offline box prints) would dangerously understate.
    """


def _is_openai_chat_model(model_id: str) -> bool:
    """True when an OpenAI model id is a chat/completions model worth offering.

    The id must start with a known chat family prefix *and* carry none of the
    non-chat markers — both halves are needed because ``gpt-4o-realtime`` and
    ``gpt-image-1`` share the ``gpt-`` prefix yet are not chat models.
    """
    if not model_id.startswith(_OPENAI_CHAT_PREFIXES):
        return False
    return not any(marker in model_id for marker in _OPENAI_NON_CHAT_MARKERS)


async def _load_codex_slugs() -> list[str]:
    """Load the user-selectable Codex model slugs from the live catalog.

    Runs the async resolver to completion so :func:`list_models` can stay
    synchronous (it wraps this in :func:`asyncio.run`). The catalog is public —
    no API key is involved.
    """
    from calfcord.providers.codex.prompts import get_default_resolver

    resolver = get_default_resolver()
    await resolver.ensure_loaded()
    return [m.slug for m in resolver.selectable_models()]


def list_models(provider: str, *, api_key: str | None) -> list[Choice]:
    """Return the live, selectable models for ``provider`` as :class:`Choice` rows.

    Each row's ``value`` is the model id the agent's ``model:`` field will carry;
    ``label`` is what the operator sees. The SDK / catalog for each provider is
    imported lazily inside its branch so importing this module needs no SDK, TTY,
    or network.

    Filtering / ordering per provider:

    * ``anthropic`` — every model the SDK lists (all are Claude chat models), in
      API order, labelled ``"<id>  (<display name>)"``.
    * ``openai`` — only chat models (see :func:`_is_openai_chat_model`), sorted
      by id. If the filter removes everything (an unexpected catalog shape) but
      the API *did* return models, fall back to all ids rather than an empty
      list, so the wizard still offers a choice.
    * ``openai-codex`` — the user-selectable slugs from the live ``models.json``
      catalog (no API key needed).

    Raises:
        ModelAuthError: when the provider SDK rejected the API key, so callers
            can warn the operator loudly that the agent won't work as configured.
        ModelListError: on any other underlying fetch failure (network, SDK), so
            callers handle one base exception type regardless of provider.
    """
    try:
        if provider == "anthropic":
            import anthropic

            page = anthropic.Anthropic(api_key=api_key).models.list()
            return [Choice(m.id, f"{m.id}  ({m.display_name})") for m in page]

        if provider == "openai":
            import openai

            ids = [m.id for m in openai.OpenAI(api_key=api_key).models.list()]
            chat_ids = sorted(i for i in ids if _is_openai_chat_model(i))
            # An empty result after filtering a non-empty catalog means the
            # account's model namespace doesn't match our heuristics — offer
            # everything rather than block the wizard with no choices.
            if not chat_ids and ids:
                chat_ids = sorted(ids)
            return [Choice(i, i) for i in chat_ids]

        if provider == "openai-codex":
            slugs = asyncio.run(_load_codex_slugs())
            return [Choice(s, s) for s in slugs]
    except Exception as exc:
        # Broad on purpose: each provider branch can fail in a different way
        # (httpx errors, auth errors, SDK-internal errors) and the callers want
        # one exception type to fall back on. A *rejected key* is singled out
        # first (as ModelAuthError) because it means the agent won't work at
        # runtime — the caller must warn louder than for a transient network
        # blip. The SDK imports here are lazy and per-provider so a host missing
        # one provider's SDK never trips over it when failing on another.
        if _is_auth_error(provider, exc):
            raise ModelAuthError(f"API key for {provider!r} was rejected: {exc}") from exc
        raise ModelListError(f"could not fetch models for {provider!r}: {exc}") from exc

    raise ModelListError(f"unknown provider {provider!r}")


def _is_auth_error(provider: str, exc: BaseException) -> bool:
    """True when ``exc`` is the provider SDK's authentication-rejected error.

    Detection is provider-specific with lazy, guarded imports inside the caller's
    ``except`` so this never forces a provider SDK to be installed (a host can run
    the wizard for one provider without the other's SDK). ``ImportError`` from a
    missing SDK simply means "can't be that SDK's auth error" → ``False``.
    """
    if provider == "anthropic":
        try:
            import anthropic
        except ImportError:
            return False
        return isinstance(exc, anthropic.AuthenticationError)
    if provider == "openai":
        try:
            import openai
        except ImportError:
            return False
        return isinstance(exc, openai.AuthenticationError)
    return False


def _build_fallback_models() -> dict[str, list[str]]:
    """Curated last-resort model lists, used only when :func:`list_models` raises.

    The flagship of each key-based provider is derived from
    :data:`calfcord.agents.factory._PROVIDER_DEFAULT_MODELS` so it tracks the
    factory's default and can't silently drift. Deliberately minimal — a
    flagship plus a cheap tier per provider — because this list is a degraded
    fallback, not the catalog. ``openai-codex`` has no static flagship in the
    factory (it resolves live), so it carries a single known-good mini slug.

    The ``factory`` import is function-local on purpose: it transitively pulls
    in calfkit's provider machinery (and the OpenAI/Anthropic SDK modules), so
    deferring it keeps merely importing *this* module SDK-free. Callers reach the
    mapping through :func:`fallback_models`, which caches the result of this
    builder in :data:`_FALLBACK_MODELS_CACHE`.

    Each ``or "<slug>"`` arm is an unreachable belt-and-suspenders default: the
    factory constants are non-empty for the key-based providers today, so the
    left operand always wins. Keeping the literals means a future ``None`` in the
    factory still degrades to a known-good slug rather than crashing the wizard.
    """
    from calfcord.agents.factory import _PROVIDER_DEFAULT_MODELS

    return {
        "anthropic": [_PROVIDER_DEFAULT_MODELS["anthropic"] or "claude-sonnet-4-5", "claude-haiku-4-5"],
        "openai": [_PROVIDER_DEFAULT_MODELS["openai"] or "gpt-5-mini", "gpt-5-nano"],
        "openai-codex": ["gpt-5-codex-mini"],
    }


# Lazily-populated cache for :func:`fallback_models`. ``None`` until first use so
# importing this module never imports the factory (which would eagerly load the
# provider SDKs). Callers/tests that need the mapping go through
# :func:`fallback_models`; the name below is kept populated as a convenience view.
_FALLBACK_MODELS_CACHE: dict[str, list[str]] | None = None


def fallback_models() -> dict[str, list[str]]:
    """Return the curated fallback mapping, building (and caching) it on first call.

    Lazy so that importing :mod:`calfcord.cli._providers` does not import
    :mod:`calfcord.agents.factory` (and through it the provider SDKs). The result
    is cached for the process lifetime — the factory defaults are constants.
    """
    global _FALLBACK_MODELS_CACHE
    if _FALLBACK_MODELS_CACHE is None:
        _FALLBACK_MODELS_CACHE = _build_fallback_models()
    return _FALLBACK_MODELS_CACHE


def _recommended_default(provider: str, ids: list[str], *, cheap: bool, current: str | None) -> str:
    """Pick the model id to pre-select, guaranteed to be one of ``ids``.

    Precedence:

    * ``cheap`` → the first id containing a :data:`_CHEAP_HINTS` token;
    * otherwise the provider's flagship default
      (:data:`~calfcord.agents.factory._PROVIDER_DEFAULT_MODELS`) if it is in
      ``ids``;
    * else ``current`` if it is in ``ids`` (preserve the operator's existing
      choice on re-run);
    * else the first id.

    ``ids`` is assumed non-empty (the only callers pass a populated list). The
    return value is always a member of ``ids`` so the prompter's ``default`` can
    never reference an absent row.
    """
    if cheap:
        for model_id in ids:
            if any(hint in model_id for hint in _CHEAP_HINTS):
                return model_id

    from calfcord.agents.factory import _PROVIDER_DEFAULT_MODELS

    flagship = _PROVIDER_DEFAULT_MODELS.get(provider)  # type: ignore[arg-type]
    if flagship and flagship in ids:
        return flagship
    if current and current in ids:
        return current
    return ids[0]


def pick_model(
    prompter: Prompter,
    provider: str,
    *,
    api_key: str | None,
    cheap: bool = False,
    current: str | None = None,
) -> str:
    """Prompt the operator to *select* (never free-text) a model for ``provider``.

    Tries :func:`list_models`; on failure it falls back to the curated
    :func:`fallback_models` list for the provider so the wizard keeps working
    offline / with a bad key. A :class:`ModelAuthError` (rejected key) is caught
    *first* and surfaced LOUDLY — a curated fallback still lets the operator
    finish setup, but the agent won't actually work until the key is fixed, so
    the generic offline-ish warning would understate it. The pre-selected default
    is chosen by :func:`_recommended_default` (cheap tier when ``cheap`` is set,
    otherwise the flagship, then the operator's ``current`` value, then the
    first row) and is always one of the offered values.

    Returns the selected model id.
    """
    try:
        choices = list_models(provider, api_key=api_key)
    except ModelAuthError:
        print(
            f"warning: the API key for {provider} was REJECTED — the agent won't work "
            f"until you fix it; re-run 'calfcord init' to re-enter it."
        )
        choices = _fallback_choices(provider)
    except ModelListError as exc:
        print(f"warning: couldn't fetch live models for {provider} ({exc}); choose from known models")
        choices = _fallback_choices(provider)

    if not choices:
        # A fetch that SUCCEEDS but returns nothing (a Codex catalog with only
        # hidden models, an account that lists zero) would otherwise hand the
        # prompter an empty choice list and crash it ("choices cannot be empty"),
        # aborting the wizard — the one thing this module promises never to do.
        # Treat empty-on-success like a failed fetch and offer the curated list.
        print(f"warning: no models returned for {provider}; choose from known models")
        choices = _fallback_choices(provider)

    ids = [c.value for c in choices]
    if not ids:
        # Curated fallback is empty too — only reachable for an unknown provider
        # (the wizard never produces one). Fail with a clear message rather than
        # crash the prompter on an empty list.
        raise ModelListError(f"no models available for {provider!r} and no curated fallback")
    default = _recommended_default(provider, ids, cheap=cheap, current=current)
    return prompter.select(f"Model for {provider}?", choices, default=default)


def _fallback_choices(provider: str) -> list[Choice]:
    """The curated fallback model list for ``provider`` as :class:`Choice` rows."""
    return [Choice(model_id, model_id) for model_id in fallback_models().get(provider, [])]


def ensure_credentials(
    prompter: Prompter, provider: str, *, env_path: Path, current: dict[str, str]
) -> str | None:
    """Ensure the chosen ``provider`` can authenticate, writing keys to ``env_path``.

    For key-based providers (``anthropic`` / ``openai``) it prompts for the
    provider's API-key var, masking the input and writing it only when a value
    was entered — an empty answer keeps whatever is already on disk, the
    keep-existing-on-empty contract that makes re-runs safe. For ``openai-codex``
    it runs the inline OAuth flow in :func:`_codex_login`. Unknown providers are
    a no-op.

    Returns the API key now in effect for key-based providers (the freshly
    entered value, or the existing one when the operator kept it), or ``None``
    for ``openai-codex`` / unknown providers. Callers feed the returned key to
    :func:`pick_model` so the live model list can be fetched with it.
    """
    key_var = _PROVIDER_KEY_VAR.get(provider)
    if key_var is not None:
        label = "(currently set)" if current.get(key_var) else "(not set)"
        value = prompter.secret(f"{key_var} {label} — paste to set, enter to keep:")
        if value:
            upsert(env_path, {key_var: value})
            return value
        return current.get(key_var)

    if provider == "openai-codex":
        _codex_login()
    return None


def configure_provider(
    prompter: Prompter,
    *,
    env_path: Path,
    current: dict[str, str],
    default_provider: str | None = None,
    cheap: bool = False,
    current_model: str | None = None,
) -> ProviderModel:
    """Run the provider sub-flow shared by every wizard and return ``(provider, model)``.

    Selects a provider (``default_provider`` pre-selects the menu — e.g. the
    router wizard defaults to the agent provider), ensures its credentials
    (key prompt or inline Codex OAuth), then selects a model from the live list.
    ``cheap`` biases the model default toward a fast/cheap model (the router);
    ``current_model`` pre-selects the operator's existing choice on a re-run.
    The caller decides where to persist the result (an agent ``.md`` vs the
    ``CALFKIT_ROUTER_*`` env vars), so this never writes the provider/model
    itself — only the credential side effect of :func:`ensure_credentials`.
    """
    provider = prompter.select("Model provider?", PROVIDERS, default=default_provider or "anthropic")
    api_key = ensure_credentials(prompter, provider, env_path=env_path, current=current)
    # A key-based provider with no effective key (operator skipped a never-set
    # field) will fail at runtime, so say so now rather than letting the curated
    # fallback model list imply a working setup.
    if provider in _PROVIDER_KEY_VAR and not api_key:
        print(
            f"warning: no {_PROVIDER_KEY_VAR[provider]} is set — {provider} won't work "
            f"until you add one (re-run 'calfcord init')."
        )
    model = pick_model(prompter, provider, api_key=api_key, cheap=cheap, current=current_model)
    return ProviderModel(provider, model)


def _codex_login() -> None:
    """Run the inline ChatGPT-subscription (Codex) OAuth flow.

    Steps:

    1. If cached credentials are still valid (or can be silently refreshed),
       report that and stop — re-running the wizard must not force a re-login.
    2. Otherwise log in straight away — picking Codex is itself the consent, so
       there is no extra yes/no prompt. We always use the **device-code** flow:
       it prints a URL + one-time code to open on any device and polls for
       completion, with no localhost OAuth callback, so it works identically on a
       local desktop and over SSH / a headless VM (the browser flow binds a
       localhost callback the operator's machine can't reach over SSH).

    Any failure (network, OAuth error) is *caught*, surfaced as a warning with a
    resume hint (``calfcord calfkit-auth codex login``), and swallowed — the wizard
    must still proceed to write the rest of the config. Auth is never the thing
    that aborts setup. The OAuth machinery is imported lazily so this module
    stays SDK-free at import time.
    """
    from openhands.sdk.llm.auth import OpenAISubscriptionAuth

    from calfcord.providers.codex.token_store import get_credential_store

    store = get_credential_store()
    auth = OpenAISubscriptionAuth(credential_store=store)

    try:
        creds = asyncio.run(auth.refresh_if_needed())
    except Exception:
        # Any refresh failure (expired token, no cache, network) just means the
        # operator isn't logged in yet — fall through to the interactive login.
        creds = None
    if creds is not None:
        print("Already authenticated with ChatGPT.")
        return

    print("Logging in to ChatGPT — open the URL below and enter the code:")
    try:
        asyncio.run(auth.login(auth_method="device_code", open_browser=False))
    except Exception as exc:
        # Broad on purpose: a declined/failed/aborted OAuth login must never
        # tear down the wizard — warn with a resume hint and let setup finish.
        print(
            f"warning: Codex login did not complete ({exc}). "
            "You can finish it later with: calfcord calfkit-auth codex login"
        )
        return
    print("Logged in to ChatGPT.")
