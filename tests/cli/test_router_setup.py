"""Tests for :mod:`calfcord.cli.router_setup`, the ambient-router wizard.

The provider sub-flow (provider menu, credentials, live model pick) is owned and
tested separately in :mod:`calfcord.cli._providers`, so here we monkeypatch
:func:`_providers.configure_provider` *where router_setup looks it up* to a fake
that returns a fixed ``(provider, model)`` — no network, no SDK, no OAuth. That
leaves three things this module is actually responsible for to assert: it writes
both ``CALFKIT_ROUTER_*`` vars, it returns ``0``, it explains the router is
optional/ambient, and it defaults the provider from
``CALFKIT_AGENT_DEFAULT_PROVIDER`` so the router inherits the agents' provider.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from calfcord.cli import _providers, router_setup
from calfcord.cli._envfile import read_env, upsert
from calfcord.cli._prompts import Choice


class FakePrompter:
    """Minimal :class:`~calfcord.cli._prompts.Prompter` for the wizard.

    router_setup never prompts directly (configure_provider is faked out), so
    every method just pops a scripted answer / raises if unexpectedly reached.
    """

    def __init__(self, *, selects: list[str] | None = None, secrets: list[str] | None = None) -> None:
        self._selects = deque(selects or [])
        self._secrets = deque(secrets or [])

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        return self._selects.popleft()

    def text(self, message: str, *, default: str = "") -> str:  # pragma: no cover - unused here
        raise AssertionError(f"unexpected text(): {message!r}")

    def secret(self, message: str) -> str:
        return self._secrets.popleft()

    def confirm(self, message: str, *, default: bool = False) -> bool:  # pragma: no cover - unused
        raise AssertionError(f"unexpected confirm(): {message!r}")

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        raise AssertionError(f"unexpected checkbox(): {message!r}")  # pragma: no cover - unused


def _stub_configure(provider: str, model: str):
    """Return a ``configure_provider`` replacement yielding a fixed (provider, model)."""

    def _configure(prompter, **kwargs):
        return provider, model

    return _configure


def test_router_setup_writes_router_env_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    # Patch where router_setup resolves the symbol, not the origin module.
    monkeypatch.setattr(
        router_setup._providers, "configure_provider", _stub_configure("anthropic", "claude-haiku-4-5")
    )

    rc = router_setup.run(FakePrompter(), env_path=env)

    assert rc == 0
    written = read_env(env)
    assert written["CALFKIT_ROUTER_PROVIDER"] == "anthropic"
    assert written["CALFKIT_ROUTER_MODEL"] == "claude-haiku-4-5"

    out = capsys.readouterr().out.lower()
    # The explanation must convey that the router is optional and ambient.
    assert "optional" in out
    assert "@mention" in out


def test_router_setup_confirms_chosen_provider_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr(
        router_setup._providers, "configure_provider", _stub_configure("openai", "gpt-5-nano")
    )

    router_setup.run(FakePrompter(), env_path=env)

    out = capsys.readouterr().out
    assert "openai/gpt-5-nano" in out
    assert "calfcord calfkit-router" in out


def test_router_setup_default_provider_follows_agent_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    # The agents' default provider is on disk but no router-specific override is.
    upsert(env, {"CALFKIT_AGENT_DEFAULT_PROVIDER": "openai"})

    captured: dict[str, object] = {}

    def _capture(prompter, **kwargs):
        captured.update(kwargs)
        return "openai", "gpt-5-nano"

    monkeypatch.setattr(router_setup._providers, "configure_provider", _capture)

    router_setup.run(FakePrompter(), env_path=env)

    assert captured["default_provider"] == "openai"
    # A per-message classifier should bias the model default to the cheap tier.
    assert captured["cheap"] is True


def test_router_setup_default_provider_prefers_existing_router_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    # On a re-run the router's own prior provider wins over the agent default.
    upsert(
        env,
        {"CALFKIT_AGENT_DEFAULT_PROVIDER": "openai", "CALFKIT_ROUTER_PROVIDER": "anthropic"},
    )

    captured: dict[str, object] = {}

    def _capture(prompter, **kwargs):
        captured.update(kwargs)
        return "anthropic", "claude-haiku-4-5"

    monkeypatch.setattr(router_setup._providers, "configure_provider", _capture)

    router_setup.run(FakePrompter(), env_path=env)

    assert captured["default_provider"] == "anthropic"
    assert captured["current_model"] is None


def test_router_setup_default_provider_falls_back_to_anthropic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"  # empty: no agent default, no router override

    captured: dict[str, object] = {}

    def _capture(prompter, **kwargs):
        captured.update(kwargs)
        return "anthropic", "claude-haiku-4-5"

    monkeypatch.setattr(router_setup._providers, "configure_provider", _capture)

    router_setup.run(FakePrompter(), env_path=env)

    assert captured["default_provider"] == "anthropic"


def test_configure_provider_symbol_is_the_real_one() -> None:
    """Guard: the wizard reaches configure_provider via the module it patches in tests."""
    assert router_setup._providers.configure_provider is _providers.configure_provider
