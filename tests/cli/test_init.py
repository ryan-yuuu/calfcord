"""Tests for the ``calfcord init`` flow, driven by a scripted fake Prompter.

The flow is pure logic over an injected :class:`Prompter`, so these tests never
touch a TTY or InquirerPy (they must run headless in CI). A :class:`FakePrompter`
dequeues scripted answers per prompt kind; each test supplies only the answers
its path consumes. We assert on the resulting ``.env`` (via ``read_env``) and on
printed guidance (via ``capsys``), and we verify the keep-existing-on-empty
behaviour that makes re-runs safe.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from calfcord.cli import init
from calfcord.cli._envfile import read_env, upsert
from calfcord.cli._prompts import Prompter


class FakePrompter:
    """A scripted :class:`Prompter`: each method pops the next queued answer.

    Answers are queued per prompt kind so a test only scripts the kinds its
    path actually hits, in call order. Running dry raises rather than hanging,
    which surfaces a miscounted script as a clear test failure.
    """

    def __init__(
        self,
        *,
        selects: list[str] | None = None,
        texts: list[str] | None = None,
        secrets: list[str] | None = None,
        confirms: list[bool] | None = None,
    ) -> None:
        self._selects = deque(selects or [])
        self._texts = deque(texts or [])
        self._secrets = deque(secrets or [])
        self._confirms = deque(confirms or [])

    def select(self, message: str, choices: list[tuple[str, str]], *, default: str | None = None) -> str:
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

    def checkbox(
        self, message: str, choices: list[tuple[str, str, bool]], *, instruction: str = ""
    ) -> list[str]:
        # The init flow never multi-selects; this exists only so the fake stays
        # structurally compatible with the (now checkbox-bearing) Prompter
        # Protocol — see the agent-tools tests for the driven version.
        return []


def test_fake_prompter_satisfies_protocol() -> None:
    """Guard that the test fake stays structurally compatible with the seam."""
    assert isinstance(FakePrompter(), Prompter)


def _run(prompter: FakePrompter, tmp_path: Path) -> int:
    """Drive ``init.run`` with the env file and agents dir both under ``tmp_path``."""
    env = tmp_path / ".env"
    return init.run(prompter, env_path=env, agents_dir=tmp_path)


def test_anthropic_writes_provider_and_key(tmp_path: Path) -> None:
    prompter = FakePrompter(
        selects=["anthropic", "url"],
        secrets=["sk-ant-123", ""],  # provider key, then empty discord token
        texts=["", "", "", "broker:9092"],  # app_id, guild, channel, broker url
    )
    assert _run(prompter, tmp_path) == 0

    env = read_env(tmp_path / ".env")
    assert env["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "anthropic"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-123"
    assert "OPENAI_API_KEY" not in env


def test_openai_writes_openai_key(tmp_path: Path) -> None:
    prompter = FakePrompter(
        selects=["openai", "url"],
        secrets=["sk-openai-xyz", ""],
        texts=["", "", "", "broker:9092"],
    )
    assert _run(prompter, tmp_path) == 0

    env = read_env(tmp_path / ".env")
    assert env["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == "sk-openai-xyz"
    assert "ANTHROPIC_API_KEY" not in env


def test_openai_codex_writes_no_key_and_prints_auth_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    prompter = FakePrompter(
        selects=["openai-codex", "url"],
        secrets=[""],  # only the discord token secret; no provider key prompt for codex
        texts=["", "", "", "broker:9092"],
    )
    assert _run(prompter, tmp_path) == 0

    env = read_env(tmp_path / ".env")
    assert env["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "openai-codex"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env

    out = capsys.readouterr().out
    assert "calfcord calfkit-auth login" in out


def test_broker_docker_sets_local_url_and_prints_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompter = FakePrompter(
        selects=["anthropic", "docker"],
        secrets=["sk-ant", ""],
        texts=["", "", ""],  # app_id, guild, channel — no broker-url prompt on docker path
    )
    assert _run(prompter, tmp_path) == 0

    env = read_env(tmp_path / ".env")
    assert env["CALF_HOST_URL"] == "localhost:19092"

    out = capsys.readouterr().out
    assert "docker run -d --name calfcord-redpanda" in out
    assert "redpanda start --mode dev-container" in out


def test_broker_url_sets_given_url(tmp_path: Path) -> None:
    prompter = FakePrompter(
        selects=["anthropic", "url"],
        secrets=["sk-ant", ""],
        texts=["", "", "", "my-broker.example.com:9092"],
    )
    assert _run(prompter, tmp_path) == 0
    assert read_env(tmp_path / ".env")["CALF_HOST_URL"] == "my-broker.example.com:9092"


def test_discord_fields_written_when_provided(tmp_path: Path) -> None:
    prompter = FakePrompter(
        selects=["anthropic", "url"],
        secrets=["sk-ant", "bot-token-abc"],
        texts=["12345", "67890", "11111", "broker:9092"],  # app_id, guild, channel, broker url
    )
    assert _run(prompter, tmp_path) == 0

    env = read_env(tmp_path / ".env")
    assert env["DISCORD_BOT_TOKEN"] == "bot-token-abc"
    assert env["DISCORD_APPLICATION_ID"] == "12345"
    assert env["DISCORD_GUILD_ID"] == "67890"
    assert env["DISCORD_DEFAULT_CHANNEL_ID"] == "11111"


def test_empty_answers_keep_prior_values(tmp_path: Path) -> None:
    """A re-run with empty secret/text answers must not clobber existing values."""
    env_path = tmp_path / ".env"
    # Pre-seed as if a prior init (and an operator-written comment) had run.
    upsert(
        env_path,
        {
            "ANTHROPIC_API_KEY": "sk-original",
            "DISCORD_BOT_TOKEN": "tok-original",
            "DISCORD_APPLICATION_ID": "app-original",
            "CALF_HOST_URL": "orig-broker:9092",
        },
    )

    # Re-run: keep provider anthropic, supply NO new secrets/text (all empty),
    # choose the "url" broker but leave the URL empty too.
    prompter = FakePrompter(
        selects=["anthropic", "url"],
        secrets=["", ""],  # provider key empty, discord token empty
        texts=["", "", "", ""],  # app_id, guild, channel, broker url all empty
    )
    assert init.run(prompter, env_path=env_path, agents_dir=tmp_path) == 0

    env = read_env(env_path)
    assert env["ANTHROPIC_API_KEY"] == "sk-original"
    assert env["DISCORD_BOT_TOKEN"] == "tok-original"
    assert env["DISCORD_APPLICATION_ID"] == "app-original"
    assert env["CALF_HOST_URL"] == "orig-broker:9092"
    # Provider was (re)written — that one is always set from the select.
    assert env["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "anthropic"


def test_agent_detection_reports_assistant(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "assistant.md").write_text("---\nname: assistant\n---\nhello\n")
    # Skipped files must NOT be reported.
    (agents_dir / "agent.template.md").write_text("---\nname: agent\n---\ntemplate\n")
    (agents_dir / ".hidden.md").write_text("---\nname: hidden\n---\nx\n")

    prompter = FakePrompter(
        selects=["anthropic", "url"],
        secrets=["sk-ant", ""],
        texts=["", "", "", "broker:9092"],
    )
    env_path = tmp_path / ".env"
    assert init.run(prompter, env_path=env_path, agents_dir=agents_dir) == 0

    out = capsys.readouterr().out
    assert "assistant" in out
    assert "agent.template.md" not in out
    assert "hidden" not in out


def test_no_agents_explains_starter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty_agents = tmp_path / "agents"
    empty_agents.mkdir()
    prompter = FakePrompter(
        selects=["anthropic", "url"],
        secrets=["sk-ant", ""],
        texts=["", "", "", "broker:9092"],
    )
    assert init.run(prompter, env_path=tmp_path / ".env", agents_dir=empty_agents) == 0
    out = capsys.readouterr().out
    assert "assistant" in out  # the starter is named and explained
