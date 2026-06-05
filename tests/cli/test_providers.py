"""Tests for :mod:`calfcord.cli._providers`, the wizard provider helpers.

Every external boundary is monkeypatched: no real network, no real API keys,
and no real OAuth ever fire. The SDK clients (``openai.OpenAI`` /
``anthropic.Anthropic``), the Codex catalog resolver, and the ChatGPT OAuth
``OpenAISubscriptionAuth`` are each replaced with fakes so the filtering,
fallback, default-selection, and never-abort behaviours can be asserted in
isolation. A small :class:`FakePrompter` satisfies the full
:class:`~calfcord.cli._prompts.Prompter` Protocol.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import ClassVar

import pytest

from calfcord.cli import _providers
from calfcord.cli._envfile import read_env
from calfcord.cli._prompts import Choice, Prompter


class _FakeModel:
    """Stand-in for an SDK model object exposing ``.id`` (and optional display name)."""

    def __init__(self, model_id: str, display_name: str | None = None) -> None:
        self.id = model_id
        self.display_name = display_name


class _FakeCodexModel:
    """Stand-in for a :class:`CodexModel`, exposing only the ``.slug`` we read."""

    def __init__(self, slug: str) -> None:
        self.slug = slug


class _FakeResolver:
    """Fake Codex resolver: a real ``async def ensure_loaded`` + sync ``selectable_models``."""

    def __init__(self, slugs: list[str]) -> None:
        self._slugs = slugs
        self.loaded = False

    async def ensure_loaded(self) -> None:
        self.loaded = True

    def selectable_models(self) -> list[_FakeCodexModel]:
        return [_FakeCodexModel(s) for s in self._slugs]


class FakePrompter:
    """Scripted :class:`Prompter`: each method pops the next queued answer.

    ``select`` also records the ``default`` it was offered so tests can assert
    the recommended pre-selection without coupling to the returned value.
    """

    def __init__(
        self,
        *,
        selects: list[str] | None = None,
        texts: list[str] | None = None,
        secrets: list[str] | None = None,
        confirms: list[bool] | None = None,
        checkboxes: list[list[str]] | None = None,
    ) -> None:
        self._selects = deque(selects or [])
        self._texts = deque(texts or [])
        self._secrets = deque(secrets or [])
        self._confirms = deque(confirms or [])
        self._checkboxes = deque(checkboxes or [])
        self.last_select_default: str | None = None
        self.last_select_choices: list[Choice] = []

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        self.last_select_default = default
        self.last_select_choices = choices
        if not self._selects:
            raise AssertionError(f"unexpected select(): {message!r}")
        return self._selects.popleft()

    def text(self, message: str, *, default: str = "") -> str:
        if not self._texts:
            raise AssertionError(f"unexpected text(): {message!r}")
        return self._texts.popleft()

    def secret(self, message: str) -> str:
        if not self._secrets:
            raise AssertionError(f"unexpected secret(): {message!r}")
        return self._secrets.popleft()

    def confirm(self, message: str, *, default: bool = False) -> bool:
        if not self._confirms:
            raise AssertionError(f"unexpected confirm(): {message!r}")
        return self._confirms.popleft()

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        if not self._checkboxes:
            return []
        return self._checkboxes.popleft()


class _FakeOpenAI:
    """Fake ``openai.OpenAI`` whose ``.models.list()`` returns scripted models."""

    _models_to_return: ClassVar[list[_FakeModel]] = []

    def __init__(self, *, api_key: str | None = None) -> None:
        self.api_key = api_key

    class _Models:
        def __init__(self, models: list[_FakeModel]) -> None:
            self._models = models

        def list(self) -> list[_FakeModel]:
            return self._models

    @property
    def models(self) -> _FakeOpenAI._Models:
        return self._Models(type(self)._models_to_return)


class _FakeAnthropic:
    """Fake ``anthropic.Anthropic`` whose ``.models.list()`` returns scripted models."""

    _models_to_return: ClassVar[list[_FakeModel]] = []

    def __init__(self, *, api_key: str | None = None) -> None:
        self.api_key = api_key

    class _Models:
        def __init__(self, models: list[_FakeModel]) -> None:
            self._models = models

        def list(self) -> list[_FakeModel]:
            return self._models

    @property
    def models(self) -> _FakeAnthropic._Models:
        return self._Models(type(self)._models_to_return)


def test_fake_prompter_satisfies_protocol() -> None:
    """Guard that the test fake stays structurally compatible with the seam."""
    assert isinstance(FakePrompter(), Prompter)


def test_importing_module_pulls_in_no_provider_sdks() -> None:
    """Importing ``_providers`` must not eagerly load any provider SDK/catalog.

    The whole point of the module is to keep the OpenAI/Anthropic SDKs, the
    Codex catalog (httpx), and the OAuth machinery behind function-local
    imports so the argparse entry point and the wizard can import it on a host
    missing a given provider's SDK. We assert this in a *fresh* interpreter so
    SDKs already imported by the rest of the test session don't mask a
    regression.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import calfcord.cli._providers; "
        "heavy=[m for m in ('openai','anthropic','openhands','httpx') if m in sys.modules]; "
        "print(','.join(heavy))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", f"unexpected eager SDK imports: {result.stdout.strip()!r}"


# --- list_models: openai filtering -----------------------------------------


def test_list_models_openai_keeps_only_chat_models_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    mixed = [
        _FakeModel("gpt-5"),
        _FakeModel("o3-mini"),
        _FakeModel("text-embedding-3-large"),
        _FakeModel("tts-1"),
        _FakeModel("gpt-4o-realtime"),
        _FakeModel("whisper-1"),
    ]
    _FakeOpenAI._models_to_return = mixed
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    choices = _providers.list_models("openai", api_key="sk-test")
    ids = [c.value for c in choices]

    # Only the two real chat models survive, sorted by id; their value == label.
    assert ids == ["gpt-5", "o3-mini"]
    assert all(c.value == c.label for c in choices)
    # Non-chat families are all dropped.
    for dropped in ("text-embedding-3-large", "tts-1", "gpt-4o-realtime", "whisper-1"):
        assert dropped not in ids


def test_list_models_openai_falls_back_to_all_when_filter_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-empty catalog that the chat filter would empty must not yield [].
    _FakeOpenAI._models_to_return = [_FakeModel("text-embedding-3-large"), _FakeModel("tts-1")]
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    ids = [c.value for c in _providers.list_models("openai", api_key="sk-test")]
    assert ids == ["text-embedding-3-large", "tts-1"]


# --- list_models: anthropic -------------------------------------------------


def test_list_models_anthropic_uses_id_value_and_display_label(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAnthropic._models_to_return = [
        _FakeModel("claude-sonnet-4-5", "Claude Sonnet 4.5"),
        _FakeModel("claude-haiku-4-5", "Claude Haiku 4.5"),
    ]
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    choices = _providers.list_models("anthropic", api_key="sk-ant")
    # API order preserved; value is the id, label carries the display name.
    assert [c.value for c in choices] == ["claude-sonnet-4-5", "claude-haiku-4-5"]
    assert "Claude Sonnet 4.5" in choices[0].label
    assert choices[0].label.startswith("claude-sonnet-4-5")


# --- list_models: codex -----------------------------------------------------


def test_list_models_codex_returns_selectable_slugs(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``_load_codex_slugs`` imports the resolver from the prompts module at call
    # time, so the fake must be installed there (not on ``_providers``).
    import calfcord.providers.codex.prompts as prompts_mod

    resolver = _FakeResolver(["gpt-5.2-codex", "gpt-5-codex-mini"])
    monkeypatch.setattr(prompts_mod, "get_default_resolver", lambda *, cache=None: resolver)

    choices = _providers.list_models("openai-codex", api_key=None)
    assert [c.value for c in choices] == ["gpt-5.2-codex", "gpt-5-codex-mini"]
    assert all(c.value == c.label for c in choices)
    assert resolver.loaded  # the async ensure_loaded() actually ran


# --- list_models: error normalisation --------------------------------------


def test_list_models_raises_model_list_error_on_sdk_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __init__(self, *, api_key: str | None = None) -> None:
            raise RuntimeError("no network")

    import openai

    monkeypatch.setattr(openai, "OpenAI", _Boom)

    with pytest.raises(_providers.ModelListError) as excinfo:
        _providers.list_models("openai", api_key="sk-test")
    assert "no network" in str(excinfo.value)


def test_list_models_unknown_provider_raises_model_list_error() -> None:
    with pytest.raises(_providers.ModelListError):
        _providers.list_models("nope", api_key=None)


# --- list_models: auth-error distinction (ModelAuthError) -------------------


def _auth_error(provider: str) -> Exception:
    """Build a real provider-SDK ``AuthenticationError`` (needs an httpx response)."""
    import httpx

    request = httpx.Request("GET", "https://example.invalid/v1/models")
    response = httpx.Response(401, request=request)
    if provider == "openai":
        import openai

        return openai.AuthenticationError("invalid api key", response=response, body=None)
    import anthropic

    return anthropic.AuthenticationError("invalid api key", response=response, body=None)


def test_list_models_openai_auth_error_raises_model_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reject:
        def __init__(self, *, api_key: str | None = None) -> None:
            raise _auth_error("openai")

    import openai

    monkeypatch.setattr(openai, "OpenAI", _Reject)

    with pytest.raises(_providers.ModelAuthError):
        _providers.list_models("openai", api_key="sk-bad")
    # ModelAuthError is a ModelListError, so the existing fall-back handler still catches it.
    assert issubclass(_providers.ModelAuthError, _providers.ModelListError)


def test_list_models_anthropic_auth_error_raises_model_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reject:
        def __init__(self, *, api_key: str | None = None) -> None:
            raise _auth_error("anthropic")

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _Reject)

    with pytest.raises(_providers.ModelAuthError):
        _providers.list_models("anthropic", api_key="sk-bad")


def test_list_models_non_auth_failure_stays_generic_model_list_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-auth failure must remain a plain ModelListError, not a ModelAuthError."""

    class _Boom:
        def __init__(self, *, api_key: str | None = None) -> None:
            raise RuntimeError("no network")

    import openai

    monkeypatch.setattr(openai, "OpenAI", _Boom)

    with pytest.raises(_providers.ModelListError) as excinfo:
        _providers.list_models("openai", api_key="sk-test")
    assert not isinstance(excinfo.value, _providers.ModelAuthError)


def test_pick_model_auth_error_prints_loud_rejected_warning_then_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rejected key must surface the LOUD REJECTED line before the curated fallback."""

    def _raise(provider: str, *, api_key: str | None) -> list[Choice]:
        raise _providers.ModelAuthError("rejected")

    monkeypatch.setattr(_providers, "list_models", _raise)

    fallback = _providers.fallback_models()["anthropic"]
    prompter = FakePrompter(selects=[fallback[0]])
    chosen = _providers.pick_model(prompter, "anthropic", api_key="sk-bad")

    out = capsys.readouterr().out
    assert "the API key for anthropic was REJECTED" in out
    assert "re-run 'calfcord init'" in out
    # Falls back to the curated list so the operator can still finish setup.
    assert [c.value for c in prompter.last_select_choices] == fallback
    assert chosen == fallback[0]


# --- pick_model: fallback path ----------------------------------------------


def test_pick_model_falls_back_to_curated_list_on_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(provider: str, *, api_key: str | None) -> list[Choice]:
        raise _providers.ModelListError("boom")

    monkeypatch.setattr(_providers, "list_models", _raise)

    fallback = _providers.fallback_models()["anthropic"]
    prompter = FakePrompter(selects=[fallback[-1]])
    chosen = _providers.pick_model(prompter, "anthropic", api_key=None)

    out = capsys.readouterr().out
    assert "warning: couldn't fetch live models" in out
    # The curated fallback list is exactly what the prompter was offered.
    assert [c.value for c in prompter.last_select_choices] == fallback
    assert chosen == fallback[-1]


# --- pick_model: empty-but-successful live list -----------------------------


class _NoEmptyPrompter(FakePrompter):
    """A :class:`FakePrompter` that fails loudly if handed an empty choice list.

    The bug this guards: an empty-on-success live fetch must be treated like a
    failed fetch and replaced with the curated fallback *before* the prompter is
    called — never passed through as ``[]`` (which crashes InquirerPy with
    "choices cannot be empty" and aborts the wizard).
    """

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        assert choices, "prompter.select() must never receive an empty choices list"
        return super().select(message, choices, default=default)


@pytest.mark.parametrize("provider", ["openai-codex", "anthropic"])
def test_pick_model_empty_live_list_falls_back_to_curated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], provider: str
) -> None:
    """A live fetch that SUCCEEDS but returns ``[]`` degrades to the curated list."""
    monkeypatch.setattr(_providers, "list_models", lambda provider, *, api_key: [])

    fallback = _providers.fallback_models()[provider]
    prompter = _NoEmptyPrompter(selects=[fallback[0]])
    chosen = _providers.pick_model(prompter, provider, api_key=None)

    out = capsys.readouterr().out
    assert "warning:" in out
    assert f"no models returned for {provider}" in out
    # The curated fallback list is exactly what the (non-empty) prompter saw.
    assert [c.value for c in prompter.last_select_choices] == fallback
    assert chosen == fallback[0]


# --- pick_model: default selection -----------------------------------------


def test_pick_model_cheap_default_prefers_cheap_token(monkeypatch: pytest.MonkeyPatch) -> None:
    live = [Choice("claude-opus-4-5", "opus"), Choice("claude-haiku-4-5", "haiku")]
    monkeypatch.setattr(_providers, "list_models", lambda provider, *, api_key: live)

    prompter = FakePrompter(selects=["claude-haiku-4-5"])
    _providers.pick_model(prompter, "anthropic", api_key=None, cheap=True)
    # cheap=True pre-selects the model carrying a _CHEAP_HINTS token.
    assert prompter.last_select_default == "claude-haiku-4-5"


def test_pick_model_non_cheap_default_is_flagship(monkeypatch: pytest.MonkeyPatch) -> None:
    from calfcord.agents.factory import _PROVIDER_DEFAULT_MODELS

    flagship = _PROVIDER_DEFAULT_MODELS["anthropic"]
    assert flagship is not None
    live = [Choice("claude-haiku-4-5", "haiku"), Choice(flagship, "flagship")]
    monkeypatch.setattr(_providers, "list_models", lambda provider, *, api_key: live)

    prompter = FakePrompter(selects=[flagship])
    _providers.pick_model(prompter, "anthropic", api_key=None, cheap=False)
    # Non-cheap pre-selects the factory flagship when it is in the live list.
    assert prompter.last_select_default == flagship


def test_pick_model_default_falls_to_current_then_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # No flagship and no cheap token present → ``current`` (in the list) wins.
    live = [Choice("custom-a", "a"), Choice("custom-b", "b")]
    monkeypatch.setattr(_providers, "list_models", lambda provider, *, api_key: live)

    prompter = FakePrompter(selects=["custom-a"])
    _providers.pick_model(prompter, "openai", api_key=None, current="custom-b")
    assert prompter.last_select_default == "custom-b"

    prompter2 = FakePrompter(selects=["custom-a"])
    _providers.pick_model(prompter2, "openai", api_key=None, current="not-in-list")
    # ``current`` absent from the list → first id is the safe default.
    assert prompter2.last_select_default == "custom-a"


# --- ensure_credentials: key providers -------------------------------------


def test_ensure_credentials_anthropic_writes_key_when_entered(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    prompter = FakePrompter(secrets=["sk-ant-123"])
    _providers.ensure_credentials(prompter, "anthropic", env_path=env, current={})
    assert read_env(env)["ANTHROPIC_API_KEY"] == "sk-ant-123"


def test_ensure_credentials_openai_writes_key_when_entered(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    prompter = FakePrompter(secrets=["sk-openai-xyz"])
    _providers.ensure_credentials(prompter, "openai", env_path=env, current={})
    assert read_env(env)["OPENAI_API_KEY"] == "sk-openai-xyz"


def test_ensure_credentials_empty_answer_keeps_existing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    prompter = FakePrompter(secrets=[""])  # operator hits enter to keep
    _providers.ensure_credentials(
        prompter, "anthropic", env_path=env, current={"ANTHROPIC_API_KEY": "existing"}
    )
    # Empty answer must not write the file at all (keep-existing-on-empty).
    assert read_env(env) == {}


# --- ensure_credentials / _codex_login: never abort the wizard -------------


class _FakeAuth:
    """Fake ``OpenAISubscriptionAuth`` driving the three login outcomes.

    ``refresh_result`` is the value/exception ``refresh_if_needed`` yields;
    ``login_error`` (when set) is raised by ``login`` to exercise the
    never-abort path. ``login_calls`` records the ``auth_method`` used.
    """

    refresh_result: ClassVar[object] = None
    refresh_raises: ClassVar[BaseException | None] = None
    login_error: ClassVar[BaseException | None] = None
    login_calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, *, credential_store: object) -> None:
        self.credential_store = credential_store

    async def refresh_if_needed(self) -> object:
        if type(self).refresh_raises is not None:
            raise type(self).refresh_raises
        return type(self).refresh_result

    async def login(self, *, auth_method: str, open_browser: bool) -> None:
        type(self).login_calls.append({"auth_method": auth_method, "open_browser": open_browser})
        if type(self).login_error is not None:
            raise type(self).login_error


def _patch_codex_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch both lazily-imported codex-login dependencies at their source."""
    import openhands.sdk.llm.auth as auth_mod

    from calfcord.providers.codex import token_store

    _FakeAuth.login_calls = []
    monkeypatch.setattr(auth_mod, "OpenAISubscriptionAuth", _FakeAuth)
    monkeypatch.setattr(token_store, "get_credential_store", lambda: object())


def test_codex_login_already_authenticated_skips_login(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_codex_auth(monkeypatch)
    _FakeAuth.refresh_result = object()  # truthy creds → already logged in
    _FakeAuth.refresh_raises = None
    _FakeAuth.login_error = None

    _providers.ensure_credentials(FakePrompter(), "openai-codex", env_path=Path("/unused"), current={})

    assert "Already authenticated with ChatGPT." in capsys.readouterr().out
    assert _FakeAuth.login_calls == []  # login NOT attempted


def test_codex_login_uses_device_code_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Picking Codex is itself the consent — login goes straight to the
    # device-code flow (works locally and over SSH) with no yes/no prompt.
    _patch_codex_auth(monkeypatch)
    _FakeAuth.refresh_result = None  # not logged in → must log in
    _FakeAuth.refresh_raises = None
    _FakeAuth.login_error = None

    _providers.ensure_credentials(FakePrompter(), "openai-codex", env_path=Path("/unused"), current={})

    assert _FakeAuth.login_calls == [{"auth_method": "device_code", "open_browser": False}]
    assert "Logged in to ChatGPT." in capsys.readouterr().out


def test_codex_login_refresh_failure_treated_as_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_codex_auth(monkeypatch)
    _FakeAuth.refresh_result = None
    _FakeAuth.refresh_raises = RuntimeError("refresh boom")  # treated as not-logged-in
    _FakeAuth.login_error = None

    _providers.ensure_credentials(FakePrompter(), "openai-codex", env_path=Path("/unused"), current={})
    # Refresh exception did not abort; the device-code login still ran.
    assert _FakeAuth.login_calls == [{"auth_method": "device_code", "open_browser": False}]


def test_codex_login_failure_warns_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_codex_auth(monkeypatch)
    _FakeAuth.refresh_result = None
    _FakeAuth.refresh_raises = None
    _FakeAuth.login_error = RuntimeError("login boom")  # login fails

    # Must NOT raise — the wizard has to continue to write config.
    _providers.ensure_credentials(FakePrompter(), "openai-codex", env_path=Path("/unused"), current={})

    out = capsys.readouterr().out
    assert "warning: Codex login did not complete" in out
    # The resume hint must name the real command — the calfkit-auth CLI requires
    # the `codex` subcommand (`calfkit-auth codex login`), not a bare `login`.
    assert "calfcord calfkit-auth codex login" in out


# --- shared seam: PROVIDERS / ensure_credentials return / configure_provider ---

def test_providers_list_matches_provider_literal() -> None:
    """The wizard provider menu must stay in lockstep with the Provider Literal."""
    from typing import get_args

    from calfcord.agents.definition import Provider

    assert {c.value for c in _providers.PROVIDERS} == set(get_args(Provider))


def test_ensure_credentials_returns_effective_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=old\n")
    # Empty answer keeps the existing key and returns it.
    kept = _providers.ensure_credentials(
        FakePrompter(secrets=[""]), "anthropic", env_path=env, current=read_env(env)
    )
    assert kept == "old"
    # A new value is written and returned.
    new = _providers.ensure_credentials(
        FakePrompter(secrets=["sk-new"]), "anthropic", env_path=env, current=read_env(env)
    )
    assert new == "sk-new"
    assert read_env(env)["ANTHROPIC_API_KEY"] == "sk-new"


def test_configure_provider_selects_provider_creds_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(
        _providers,
        "list_models",
        lambda provider, *, api_key: [Choice("claude-sonnet-4-5", "x"), Choice("claude-haiku-4-5", "y")],
    )
    prompter = FakePrompter(selects=["anthropic", "claude-haiku-4-5"], secrets=["sk-key"])
    provider, model = _providers.configure_provider(prompter, env_path=env, current={}, cheap=True)
    assert provider == "anthropic"
    assert model == "claude-haiku-4-5"
    assert read_env(env)["ANTHROPIC_API_KEY"] == "sk-key"
    # cheap=True biased the model default to the haiku tier.
    assert prompter.last_select_default == "claude-haiku-4-5"


def test_configure_provider_returns_provider_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The return is a named ``ProviderModel`` (``.provider`` / ``.model``), still unpackable.

    Monkeypatch the two seams ``configure_provider`` drives — ``ensure_credentials``
    (no real key/OAuth) and ``list_models`` (no SDK/network) — so the test asserts
    only the return shape: a positional swap would be silent on a bare tuple, so the
    named pair is the contract callers (``create_agent`` / ``init``) rely on.
    """
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(
        _providers, "ensure_credentials", lambda *a, **k: "sk-key"
    )
    monkeypatch.setattr(
        _providers,
        "list_models",
        lambda provider, *, api_key: [Choice("claude-sonnet-4-5", "x")],
    )
    prompter = FakePrompter(selects=["anthropic", "claude-sonnet-4-5"])

    result = _providers.configure_provider(prompter, env_path=env, current={})

    assert isinstance(result, _providers.ProviderModel)
    assert result.provider == "anthropic"
    assert result.model == "claude-sonnet-4-5"
    # Tuple-unpacking still works for callers that don't read the attributes.
    provider, model = result
    assert (provider, model) == ("anthropic", "claude-sonnet-4-5")


def test_configure_provider_warns_when_no_key_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A key-based provider left without an effective key must warn loudly."""
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(
        _providers,
        "list_models",
        lambda provider, *, api_key: [Choice("claude-sonnet-4-5", "x")],
    )
    # Empty secret with no prior key → ensure_credentials returns no key.
    prompter = FakePrompter(selects=["anthropic", "claude-sonnet-4-5"], secrets=[""])
    _providers.configure_provider(prompter, env_path=env, current={})

    out = capsys.readouterr().out
    assert "warning: no ANTHROPIC_API_KEY is set" in out
    assert "anthropic won't work" in out
