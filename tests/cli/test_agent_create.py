"""Tests for ``calfcord agent create`` — the reusable agent-creation flow.

The flow is pure logic over an injected :class:`Prompter`, so these tests never
touch a TTY or InquirerPy. A scripted :class:`FakePrompter` dequeues one answer
per prompt kind in call order; the provider sub-flow is delegated to
:func:`calfcord.cli._providers.configure_provider`, which would reach a real SDK
/ model catalog, so every test monkeypatches it (in ``agent_create``'s
namespace) to a fixed ``(provider, model)`` — no network, key, or OAuth ever
fires. We assert on the written ``agents/<name>.md`` (via ``parse_agent_md``),
the printed guidance (via ``capsys``), and the exit code.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import _agents, agent_create
from calfcord.cli._prompts import Choice, Prompter

_FIXED_PROVIDER = ("anthropic", "claude-haiku-4-5")


class FakePrompter:
    """A scripted :class:`Prompter`: each method pops the next queued answer.

    Answers are queued per prompt kind so a test only scripts the kinds its path
    actually hits, in call order. Running a queue dry raises rather than hanging,
    so a miscounted script surfaces as a clear failure. ``checkbox`` records the
    choices it was offered (``last_checkbox_choices``) and, with no scripted
    result, returns every pre-checked row (mirrors InquirerPy's enter-on-default).
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
        self.last_checkbox_choices: list[Choice] = []

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
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
        self.last_checkbox_choices = choices
        if not self._checkboxes:
            return [c.value for c in choices if c.checked]
        return self._checkboxes.popleft()


@pytest.fixture(autouse=True)
def _stub_configure_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the provider sub-flow with a fixed ``(provider, model)``.

    ``configure_provider`` is imported into ``agent_create``'s namespace, so the
    stub is installed there. It consumes no prompts, so tests don't script
    provider answers — keeping every create-flow test free of any provider SDK /
    network.
    """

    def _fixed(prompter: object, **_: object) -> tuple[str, str]:
        return _FIXED_PROVIDER

    monkeypatch.setattr(agent_create, "configure_provider", _fixed)


def _prompter(
    *,
    name: str,
    description: str = "d",
    checkboxes: list[list[str]] | None = None,
    confirms: list[bool] | None = None,
) -> FakePrompter:
    """Script one create pass: text(name), text(description), checkbox(tools).

    The provider sub-flow is stubbed (consumes no prompts). ``run`` offers an
    "edit prompt now?" confirm after the write; supply ``confirms=[False]`` for
    that path (the default below declines it so no ``$EDITOR`` is ever launched).
    """
    return FakePrompter(
        texts=[name, description],
        checkboxes=checkboxes,
        confirms=confirms if confirms is not None else [False],
    )


def test_run_creates_agent_md(tmp_path: Path) -> None:
    """A full create pass writes a re-parseable ``<name>.md`` with the chosen fields."""
    agents_dir = tmp_path / "agents"
    env_path = tmp_path / ".env"
    prompter = _prompter(name="scribe", description="Takes notes", checkboxes=[["read_file", "web_search"]])

    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=env_path, name=None)
    assert rc == 0

    md = agents_dir / "scribe.md"
    assert md.is_file()
    agent = parse_agent_md(md)
    assert agent.agent_id == "scribe"
    assert agent.description == "Takes notes"
    assert agent.provider == "anthropic"
    assert agent.model == "claude-haiku-4-5"
    assert set(agent.tools) == {"read_file", "web_search"}


def test_run_prints_created_and_restart_banner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Success prints the 'Created agent' line naming the restart commands + slash."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None) == 0

    out = capsys.readouterr().out
    assert "Created agent 'scribe'." in out
    assert "calfcord calfkit-agent" in out
    assert "calfcord calfkit-bridge" in out
    assert "/scribe" in out


def test_run_passes_name_default_through(tmp_path: Path) -> None:
    """A given ``name`` pre-fills the prompt; the fake returns it, so it's used."""
    agents_dir = tmp_path / "agents"
    # The name prompt returns the default that ``run`` passed as name_default.
    prompter = FakePrompter(texts=["scout", "d"], confirms=[False])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scout") == 0
    assert (agents_dir / "scout.md").is_file()


def test_run_slugifies_typed_name(tmp_path: Path) -> None:
    """A typed friendly name is slugified into a valid stem before write."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="My Helper!")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None) == 0
    assert (agents_dir / "my_helper.md").is_file()
    assert parse_agent_md(agents_dir / "my_helper.md").agent_id == "my_helper"


def test_run_blank_description_uses_default(tmp_path: Path) -> None:
    """A blank description falls back to the seed default."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe", description="")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None) == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == _agents.DEFAULT_DESCRIPTION


def test_run_tricky_description_roundtrips(tmp_path: Path) -> None:
    """A YAML-significant description ('Has: colon') survives the create path verbatim."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe", description="Has: colon")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None) == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == "Has: colon"


def test_run_offers_prompt_edit_when_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirming the optional prompt step calls ``edit_system_prompt`` on the new file."""
    agents_dir = tmp_path / "agents"
    seen: list[Path] = []

    def _spy(md_path: Path) -> None:
        seen.append(md_path)

    # The lazy import in create_agent resolves ``edit_system_prompt`` from the
    # agent_edit module, so patch it there.
    from calfcord.cli import agent_edit

    monkeypatch.setattr(agent_edit, "edit_system_prompt", _spy)

    prompter = _prompter(name="scribe", confirms=[True])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None) == 0
    assert seen == [agents_dir / "scribe.md"]


def test_run_declining_prompt_edit_does_not_launch_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Declining the optional prompt step never touches ``edit_system_prompt``."""
    agents_dir = tmp_path / "agents"

    def _boom(md_path: Path) -> None:
        raise AssertionError("edit_system_prompt must not run when the operator declines")

    from calfcord.cli import agent_edit

    monkeypatch.setattr(agent_edit, "edit_system_prompt", _boom)

    prompter = _prompter(name="scribe", confirms=[False])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None) == 0


def test_run_write_failure_returns_1_without_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced write failure returns 1, prints 'error:', and never prints the banner.

    The create path validates before writing, so to force a *write* failure we
    monkeypatch the atomic-write helper to raise ``OSError`` — ``run`` must
    surface it and stop, leaving no half-created agent and no success banner.
    """

    def _boom(path: Path, payload: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_agents, "atomic_write", _boom)

    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scribe")

    out = capsys.readouterr().out
    assert rc == 1
    assert "error: could not create agent 'scribe'" in out
    assert "Created agent" not in out
    assert not (agents_dir / "scribe.md").exists()


def test_create_agent_returns_name_and_provider(tmp_path: Path) -> None:
    """The extracted flow returns ``(name, provider)`` for the caller's guidance."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    name, provider = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
    )
    assert name == "scribe"
    assert provider == "anthropic"


def test_create_agent_prune_seed_false_keeps_pristine_assistant(tmp_path: Path) -> None:
    """With ``prune_seed=False`` (the ``agent create`` default) a pristine seed survives.

    Adding a second agent must never delete the operator's starter — only
    ``init``'s first-run opt-in prunes a pristine seed.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    seed = agents_dir / "assistant.md"
    seed.write_text(
        "---\n"
        "name: assistant\n"
        "display_name: Assistant\n"
        f"description: {_agents.DEFAULT_DESCRIPTION}\n"
        "tools: []\n"
        "---\n\n"
        "You are Assistant, a helpful general-purpose AI teammate. Answer clearly.\n",
        encoding="utf-8",
    )

    prompter = _prompter(name="scribe")
    agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
    )

    assert (agents_dir / "scribe.md").is_file()
    # The pristine starter is left intact (byte-for-byte unchanged is not
    # required, but it must still parse as the seeded assistant).
    assert seed.is_file()
    assert parse_agent_md(seed).description == _agents.DEFAULT_DESCRIPTION


def test_create_agent_does_not_write_default_provider_env(tmp_path: Path) -> None:
    """``create_agent`` must not touch ``CALFKIT_AGENT_DEFAULT_PROVIDER`` (init's concern)."""
    from calfcord.cli._envfile import read_env

    agents_dir = tmp_path / "agents"
    env_path = tmp_path / ".env"
    prompter = _prompter(name="scribe")
    agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=env_path,
        prune_seed=False,
        offer_prompt=False,
    )
    # The agent carries an explicit provider/model; the install-wide default
    # provider env var is never written by this flow.
    assert "CALFKIT_AGENT_DEFAULT_PROVIDER" not in read_env(env_path)


def test_fake_prompter_satisfies_protocol() -> None:
    """Guard that the test fake stays structurally compatible with the seam."""
    assert isinstance(FakePrompter(), Prompter)
