"""Tests for :mod:`calfcord.cli.router_config` — the router's first-class config
surface (``show`` / ``set`` / ``edit``) plus its lifecycle (``start`` / ``stop``).

The router holds an LLM connection like an agent, so it gets an *editable* config
surface that mirrors the agent config ergonomics (design §12.0): ``show`` renders
the current provider/model, ``set`` writes them non-interactively (validated), and
``edit`` runs the interactive provider sub-flow — reconciling the old one-shot
``router setup`` wizard into ONE editable path. Config persists to the two
``CALFKIT_ROUTER_*`` env vars the router runner already reads
(:func:`calfcord.router.definition.build_router_definition`).

The provider sub-flow (provider menu, credentials, live model pick) is owned and
tested in :mod:`calfcord.cli._providers`, so ``edit`` is exercised by
monkeypatching :func:`_providers.configure_provider` *where router_config looks it
up* to a fake returning a fixed ``(provider, model)`` — no network, no SDK, no
OAuth, no TTY.

Lifecycle is exercised with an injected REST client (no real process-compose
binary, no broker): ``router_start`` FAILS FAST when unconfigured — before any
supervisor call — and otherwise delegates to the generic
:func:`calfcord.supervisor.component.component_start`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.cli import _providers, router_config
from calfcord.cli._envfile import read_env, upsert

_PROVIDER_VAR = "CALFKIT_ROUTER_PROVIDER"
_MODEL_VAR = "CALFKIT_ROUTER_MODEL"


class _FakePrompter:
    """Minimal :class:`~calfcord.cli._prompts.Prompter`.

    ``edit`` faked-out via ``configure_provider``, so no method is reached in the
    happy path; each raises if unexpectedly called.
    """

    def select(self, message, choices, *, default=None):  # pragma: no cover - unused
        raise AssertionError(f"unexpected select(): {message!r}")

    def text(self, message, *, default=""):  # pragma: no cover - unused
        raise AssertionError(f"unexpected text(): {message!r}")

    def secret(self, message):  # pragma: no cover - unused
        raise AssertionError(f"unexpected secret(): {message!r}")

    def confirm(self, message, *, default=False):  # pragma: no cover - unused
        raise AssertionError(f"unexpected confirm(): {message!r}")

    def checkbox(self, message, choices, *, instruction=""):  # pragma: no cover - unused
        raise AssertionError(f"unexpected checkbox(): {message!r}")


def _stub_configure(provider: str, model: str):
    """A ``configure_provider`` replacement yielding a fixed (provider, model)."""

    def _configure(prompter, **kwargs):
        return provider, model

    return _configure


class _StubClient:
    """A scriptable ProcessComposeClient for the lifecycle delegation tests.

    ``running`` backs ``list_processes`` (the physical-liveness read
    ``component_start`` consults for its already-running-here restart decision,
    behavior #2): an empty default means the router slot is NOT running, so a
    ``start`` is a genuine clock-in (the start-path the delegation tests assert),
    not a restart.
    """

    def __init__(self, *, workspace_up: bool = True, running: list[str] | None = None) -> None:
        self._workspace_up = workspace_up
        self._running = list(running or [])
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []
        self.restart_calls: list[str] = []

    async def project_state(self):
        if not self._workspace_up:
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}

    async def list_processes(self):
        return [{"name": n, "status": "Running"} for n in self._running]

    async def start_process(self, name: str):
        self.start_calls.append(name)
        return {}

    async def stop_process(self, name: str):
        self.stop_calls.append(name)
        return {}

    async def restart_process(self, name: str):
        self.restart_calls.append(name)
        return {}


def _home(tmp_path) -> str:
    return str(tmp_path)


# --- show -------------------------------------------------------------------


def test_show_renders_configured_provider_and_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    upsert(env, {_PROVIDER_VAR: "openai", _MODEL_VAR: "gpt-5-nano"})

    rc = router_config.show(env_path=env)

    assert rc == 0
    out = capsys.readouterr().out
    assert "openai" in out
    assert "gpt-5-nano" in out


def test_show_unconfigured_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfigured router renders an explicit not-configured state, not blanks."""
    env = tmp_path / ".env"  # empty: no router vars

    rc = router_config.show(env_path=env)

    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "not configured" in out


def test_show_never_prints_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``show`` must never echo any API key sitting in the same .env."""
    env = tmp_path / ".env"
    upsert(
        env,
        {
            _PROVIDER_VAR: "openai",
            _MODEL_VAR: "gpt-5-nano",
            "OPENAI_API_KEY": "sk-super-secret-value",
        },
    )

    router_config.show(env_path=env)

    out = capsys.readouterr().out
    assert "sk-super-secret-value" not in out
    assert "OPENAI_API_KEY" not in out


# --- set --------------------------------------------------------------------


def test_set_writes_both_vars_when_both_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"

    rc = router_config.set_config(env_path=env, provider="anthropic", model="claude-haiku-4-5")

    assert rc == 0
    written = read_env(env)
    assert written[_PROVIDER_VAR] == "anthropic"
    assert written[_MODEL_VAR] == "claude-haiku-4-5"


def test_set_success_prints_restart_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful ``router set`` reports the change, then the EXACT terse
    next-step block (behavior #3): the restart sentence, a blank line, the
    two-space-indented `router restart` command."""
    env = tmp_path / ".env"

    rc = router_config.set_config(env_path=env, provider="anthropic", model="claude-haiku-4-5")

    assert rc == 0
    out = capsys.readouterr().out
    assert "Restart the router to apply:\n\n  calfcord router restart" in out


def test_set_accepts_non_anthropic_valid_provider(tmp_path: Path) -> None:
    """``set`` validates against the FULL ``Provider`` literal, not just "anthropic".

    Pins that validation tracks every tag in
    :data:`~calfcord.agents.definition.Provider` (here ``openai-codex``) — a guard
    so a future provider can't silently fail validation while ``anthropic`` keeps
    the test green.
    """
    env = tmp_path / ".env"

    rc = router_config.set_config(env_path=env, provider="openai-codex", model="gpt-5-codex")

    assert rc == 0
    written = read_env(env)
    assert written[_PROVIDER_VAR] == "openai-codex"
    assert written[_MODEL_VAR] == "gpt-5-codex"


def test_set_only_provider_preserves_existing_model(tmp_path: Path) -> None:
    """A partial ``set`` writes only what was given; the other var is untouched."""
    env = tmp_path / ".env"
    upsert(env, {_PROVIDER_VAR: "openai", _MODEL_VAR: "gpt-5-nano"})

    rc = router_config.set_config(env_path=env, provider="anthropic", model=None)

    assert rc == 0
    written = read_env(env)
    assert written[_PROVIDER_VAR] == "anthropic"
    assert written[_MODEL_VAR] == "gpt-5-nano"  # preserved


def test_set_only_model_preserves_existing_provider(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    upsert(env, {_PROVIDER_VAR: "openai", _MODEL_VAR: "gpt-5-nano"})

    rc = router_config.set_config(env_path=env, provider=None, model="gpt-5-mini")

    assert rc == 0
    written = read_env(env)
    assert written[_PROVIDER_VAR] == "openai"  # preserved
    assert written[_MODEL_VAR] == "gpt-5-mini"


def test_set_rejects_unknown_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad provider is rejected (validated like an agent's), nothing written."""
    env = tmp_path / ".env"

    rc = router_config.set_config(env_path=env, provider="not-a-provider", model="x")

    assert rc == 1
    assert read_env(env) == {}  # nothing persisted on a rejected value
    out = capsys.readouterr().out.lower()
    assert "provider" in out


def test_set_rejects_empty_model(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An explicitly blank model is rejected rather than persisting an empty value."""
    env = tmp_path / ".env"

    rc = router_config.set_config(env_path=env, provider=None, model="   ")

    assert rc == 1
    assert read_env(env) == {}
    out = capsys.readouterr().out.lower()
    assert "model" in out


def test_set_no_args_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``set`` with neither flag is a usage error, not a silent no-op."""
    env = tmp_path / ".env"

    rc = router_config.set_config(env_path=env, provider=None, model=None)

    assert rc == 1
    out = capsys.readouterr().out.lower()
    assert "provider" in out or "model" in out


# --- edit (reconciles the old `router setup`) -------------------------------


def test_edit_writes_router_env_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr(
        router_config._providers,
        "configure_provider",
        _stub_configure("anthropic", "claude-haiku-4-5"),
    )

    rc = router_config.edit(_FakePrompter(), env_path=env)

    assert rc == 0
    written = read_env(env)
    assert written[_PROVIDER_VAR] == "anthropic"
    assert written[_MODEL_VAR] == "claude-haiku-4-5"

    out = capsys.readouterr().out
    out_lower = out.lower()
    # The explanation must still convey the router is optional and ambient,
    # preserving what the old `router setup` taught.
    assert "optional" in out_lower
    assert "@mention" in out_lower
    # And it ends with the EXACT terse next-step block (behavior #3).
    assert "Restart the router to apply:\n\n  calfcord router restart" in out


def test_edit_default_provider_prefers_existing_router_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On re-run the router's own prior provider/model pre-seed the sub-flow."""
    env = tmp_path / ".env"
    upsert(
        env,
        {
            "CALFKIT_AGENT_DEFAULT_PROVIDER": "openai",
            _PROVIDER_VAR: "anthropic",
            _MODEL_VAR: "claude-haiku-4-5",
        },
    )

    captured: dict[str, object] = {}

    def _capture(prompter, **kwargs):
        captured.update(kwargs)
        return "anthropic", "claude-haiku-4-5"

    monkeypatch.setattr(router_config._providers, "configure_provider", _capture)

    router_config.edit(_FakePrompter(), env_path=env)

    assert captured["default_provider"] == "anthropic"
    assert captured["current_model"] == "claude-haiku-4-5"
    # A per-message classifier biases the model default to the cheap tier.
    assert captured["cheap"] is True


def test_edit_default_provider_follows_agent_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First run inherits the agents' provider so init's choice isn't surprised."""
    env = tmp_path / ".env"
    upsert(env, {"CALFKIT_AGENT_DEFAULT_PROVIDER": "openai"})

    captured: dict[str, object] = {}

    def _capture(prompter, **kwargs):
        captured.update(kwargs)
        return "openai", "gpt-5-nano"

    monkeypatch.setattr(router_config._providers, "configure_provider", _capture)

    router_config.edit(_FakePrompter(), env_path=env)

    assert captured["default_provider"] == "openai"


def test_edit_default_provider_falls_back_to_anthropic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"  # empty: no agent default, no router override

    captured: dict[str, object] = {}

    def _capture(prompter, **kwargs):
        captured.update(kwargs)
        return "anthropic", "claude-haiku-4-5"

    monkeypatch.setattr(router_config._providers, "configure_provider", _capture)

    router_config.edit(_FakePrompter(), env_path=env)

    assert captured["default_provider"] == "anthropic"


def test_configure_provider_symbol_is_the_real_one() -> None:
    """Guard: ``edit`` reaches configure_provider via the module it patches in tests."""
    assert router_config._providers.configure_provider is _providers.configure_provider


# --- router_start: fail-fast when unconfigured ------------------------------


async def test_router_start_fails_fast_when_unconfigured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unconfigured → return 1 with a hint, BEFORE any supervisor call.

    The whole point of the router's first-class config surface: ``start`` must not
    launch an unconfigured router. The guard fires before the REST client is even
    touched, so an injected client records no call.
    """
    env = tmp_path / ".env"  # no CALFKIT_ROUTER_* vars
    client = _StubClient()

    rc = await router_config.router_start(_home(tmp_path), env_path=env, client=client)

    assert rc == 1
    assert client.start_calls == []  # fail-fast: no supervisor call attempted
    out = capsys.readouterr().out.lower()
    assert "router" in out
    # The message must steer to configuration.
    assert "router set" in out or "router edit" in out


async def test_router_start_fails_fast_when_only_provider_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A half-configured router (provider but no model) is still unconfigured."""
    env = tmp_path / ".env"
    upsert(env, {_PROVIDER_VAR: "openai"})  # model missing
    client = _StubClient()

    rc = await router_config.router_start(_home(tmp_path), env_path=env, client=client)

    assert rc == 1
    assert client.start_calls == []


async def test_router_start_ignores_blank_values(
    tmp_path: Path
) -> None:
    """An empty-string value is treated as unset (matches the runner's ``or`` chain)."""
    env = tmp_path / ".env"
    upsert(env, {_PROVIDER_VAR: "openai", _MODEL_VAR: ""})
    client = _StubClient()

    rc = await router_config.router_start(_home(tmp_path), env_path=env, client=client)

    assert rc == 1
    assert client.start_calls == []


# --- router_start: configured → delegates to component_start -----------------


async def test_router_start_configured_delegates_to_component(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configured + workspace up → start the ``router`` slot, report online, exit 0."""
    env = tmp_path / ".env"
    upsert(env, {_PROVIDER_VAR: "openai", _MODEL_VAR: "gpt-5-nano"})
    client = _StubClient()

    rc = await router_config.router_start(_home(tmp_path), env_path=env, client=client)

    assert rc == 0
    assert client.start_calls == ["router"]
    out = capsys.readouterr().out
    assert "router" in out
    assert "online" in out


async def test_router_start_configured_but_workspace_down(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configured but supervisor unreachable → the component's not-running hint."""
    env = tmp_path / ".env"
    upsert(env, {_PROVIDER_VAR: "openai", _MODEL_VAR: "gpt-5-nano"})
    client = _StubClient(workspace_up=False)

    rc = await router_config.router_start(_home(tmp_path), env_path=env, client=client)

    assert rc == 1
    assert client.start_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- router_stop: always delegates (config-agnostic) ------------------------


async def test_router_stop_delegates_to_component(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``stop`` needs no config check — you can always clock a component out."""
    client = _StubClient()

    rc = await router_config.router_stop(_home(tmp_path), client=client)

    assert rc == 0
    assert client.stop_calls == ["router"]
    out = capsys.readouterr().out
    assert "router" in out
    assert "stopped" in out


async def test_router_stop_workspace_down(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _StubClient(workspace_up=False)

    rc = await router_config.router_stop(_home(tmp_path), client=client)

    assert rc == 1
    assert client.stop_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out


# --- router_restart: always delegates (config-agnostic, the apply mechanism) -


async def test_router_restart_delegates_to_component(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``restart`` is the apply mechanism after a config edit — like ``stop`` it
    needs no config precheck (the running router already had config), so it
    delegates straight to the generic ``component_restart`` for the router slot."""
    client = _StubClient()

    rc = await router_config.router_restart(_home(tmp_path), client=client)

    assert rc == 0
    assert client.restart_calls == ["router"]
    out = capsys.readouterr().out
    assert "router" in out
    assert "restarted" in out


async def test_router_restart_workspace_down(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _StubClient(workspace_up=False)

    rc = await router_config.router_restart(_home(tmp_path), client=client)

    assert rc == 1
    assert client.restart_calls == []
    out = capsys.readouterr().out
    assert "workspace not running" in out
