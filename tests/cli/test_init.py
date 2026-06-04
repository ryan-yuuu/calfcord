"""Tests for the ``calfcord init`` setup wizard, driven by a scripted fake Prompter.

The flow is pure logic over an injected :class:`Prompter`, so these tests never
touch a TTY or InquirerPy (they must run headless in CI). A :class:`FakePrompter`
dequeues scripted answers per prompt kind; each test supplies only the answers
its path consumes. The provider sub-flow is delegated to
:func:`calfcord.cli._providers.configure_provider`, which reaches a real SDK /
model catalog; every test monkeypatches it to a fixed ``(provider, model)`` so
no network, key, or OAuth ever fires. We assert on the resulting ``.env`` (via
``read_env``), the written ``agents/<name>.md`` (via ``parse_agent_md``), and
printed guidance (via ``capsys``).
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import init
from calfcord.cli._envfile import read_env, upsert
from calfcord.cli._prompts import Choice, Prompter

_FIXED_PROVIDER = ("anthropic", "claude-haiku-4-5")


class FakePrompter:
    """A scripted :class:`Prompter`: each method pops the next queued answer.

    Answers are queued per prompt kind so a test only scripts the kinds its
    path actually hits, in call order. Running dry raises rather than hanging,
    which surfaces a miscounted script as a clear test failure. ``checkbox``
    records the choices it was offered (``last_checkbox_choices``) so tests can
    assert the pre-checked default without coupling to the returned selection.
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
            # An empty script means "keep every pre-checked row" — mirrors the
            # InquirerPy default of returning the enabled set on enter.
            return [c.value for c in choices if c.checked]
        return self._checkboxes.popleft()


@pytest.fixture(autouse=True)
def _stub_configure_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the provider sub-flow with a fixed ``(provider, model)``.

    ``configure_provider`` is imported into ``init``'s namespace, so the stub is
    installed there. It consumes no prompts, so tests don't script provider
    answers — keeping every wizard test free of any provider SDK / network.
    """

    def _fixed(prompter: object, **_: object) -> tuple[str, str]:
        return _FIXED_PROVIDER

    monkeypatch.setattr(init, "configure_provider", _fixed)


def test_fake_prompter_satisfies_protocol() -> None:
    """Guard that the test fake stays structurally compatible with the seam."""
    assert isinstance(FakePrompter(), Prompter)


def _run(
    prompter: FakePrompter,
    tmp_path: Path,
    *,
    agents_dir: Path | None = None,
) -> int:
    """Drive ``init.run`` with the env file under ``tmp_path`` and a chosen agents dir."""
    env = tmp_path / ".env"
    return init.run(prompter, env_path=env, agents_dir=agents_dir or tmp_path)


def _fresh_run_prompter(
    *,
    name: str = "assistant",
    description: str = "",
    discord_token: str = "",
    app_id: str = "",
    guild: str = "",
    channel: str = "",
    broker: str = "url",
    broker_url: str = "broker:9092",
    checkboxes: list[list[str]] | None = None,
) -> FakePrompter:
    """Build a prompter scripting one full wizard pass (provider sub-flow stubbed).

    Order of consumed prompts after the autouse stub removes the provider
    sub-flow: text(name), text(description), checkbox(tools), secret(discord
    token), text(app_id), text(guild), text(channel), select(broker)
    [+ text(broker_url) on the ``url`` branch].
    """
    texts = [name, description, app_id, guild, channel]
    if broker == "url":
        texts.append(broker_url)
    return FakePrompter(
        selects=[broker],
        texts=texts,
        secrets=[discord_token],
        checkboxes=checkboxes,
    )


# --- agent file: fresh creation --------------------------------------------


def test_fresh_run_creates_agent_md_and_writes_default_provider(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="Takes notes")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0

    md = agents_dir / "scribe.md"
    assert md.is_file()
    agent = parse_agent_md(md)
    assert agent.agent_id == "scribe"
    assert agent.display_name == "Scribe"
    assert agent.description == "Takes notes"
    assert agent.provider == "anthropic"
    assert agent.model == "claude-haiku-4-5"

    # The install default provider is persisted from the (stubbed) pick.
    assert read_env(tmp_path / ".env")["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "anthropic"


def test_blank_name_falls_back_to_assistant(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="   ", description="d")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert (agents_dir / "assistant.md").is_file()


def test_typed_name_is_slugified(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="My Helper!", description="d")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    md = agents_dir / "my_helper.md"
    assert md.is_file()
    assert parse_agent_md(md).agent_id == "my_helper"


def test_blank_description_uses_default(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == init._DEFAULT_DESCRIPTION


# --- tools checkbox ---------------------------------------------------------


def test_tools_checkbox_offers_all_builtins_prechecked(tmp_path: Path) -> None:
    from calfcord.tools import TOOL_REGISTRY

    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0

    builtin_rows = {c.value: c.checked for c in prompter.last_checkbox_choices if not c.value.startswith("mcp/")}
    # Every builtin is offered, and every builtin row is pre-checked.
    assert set(builtin_rows) == set(TOOL_REGISTRY)
    assert all(builtin_rows.values())


def test_keeping_all_tools_writes_full_builtin_list(tmp_path: Path) -> None:
    from calfcord.tools import TOOL_REGISTRY

    agents_dir = tmp_path / "agents"
    # No checkbox script → fake returns every pre-checked (all builtin) row.
    prompter = _fresh_run_prompter(name="scribe", description="d", checkboxes=None)
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert set(parse_agent_md(agents_dir / "scribe.md").tools) == set(TOOL_REGISTRY)


def test_selecting_a_subset_writes_that_subset(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(
        name="scribe",
        description="d",
        checkboxes=[["read_file", "web_search"]],
    )
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert set(parse_agent_md(agents_dir / "scribe.md").tools) == {"read_file", "web_search"}


def test_empty_tool_selection_writes_explicit_empty_list(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d", checkboxes=[[]])
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    # ``tools: []`` parses to an empty tuple (explicit "no tools"), not None.
    assert parse_agent_md(agents_dir / "scribe.md").tools == ()


def test_security_caution_prints_when_shell_or_write_selected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(
        name="scribe", description="d", checkboxes=[["shell", "write_file"]]
    )
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    out = capsys.readouterr().out
    assert "shell + file write access" in out
    assert "docs/security.md §3.4" in out


def test_security_caution_silent_for_readonly_tools(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(
        name="scribe", description="d", checkboxes=[["read_file", "web_search"]]
    )
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert "file write access" not in capsys.readouterr().out


# --- provider key / .env side effects (via the real provider sub-flow) ------


def test_default_provider_persisted_from_configure_provider(tmp_path: Path) -> None:
    """The provider returned by the sub-flow is persisted as the install default."""
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert read_env(tmp_path / ".env")["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "anthropic"


# --- discord + broker .env writes ------------------------------------------


def test_broker_docker_sets_local_url_and_prints_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d", broker="docker")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0

    assert read_env(tmp_path / ".env")["CALF_HOST_URL"] == "localhost:19092"
    out = capsys.readouterr().out
    assert "docker run -d --name calfcord-redpanda" in out
    assert "redpanda start --mode dev-container" in out


def test_broker_url_sets_given_url(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(
        name="scribe", description="d", broker="url", broker_url="my-broker.example.com:9092"
    )
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert read_env(tmp_path / ".env")["CALF_HOST_URL"] == "my-broker.example.com:9092"


def test_discord_fields_written_when_provided(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(
        name="scribe",
        description="d",
        discord_token="bot-token-abc",
        app_id="12345",
        guild="67890",
        channel="11111",
    )
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0

    env = read_env(tmp_path / ".env")
    assert env["DISCORD_BOT_TOKEN"] == "bot-token-abc"
    assert env["DISCORD_APPLICATION_ID"] == "12345"
    assert env["DISCORD_GUILD_ID"] == "67890"
    assert env["DISCORD_DEFAULT_CHANNEL_ID"] == "11111"


def test_empty_answers_keep_prior_env_values(tmp_path: Path) -> None:
    """A re-run with empty secret/text answers must not clobber existing .env values."""
    env_path = tmp_path / ".env"
    agents_dir = tmp_path / "agents"
    upsert(
        env_path,
        {
            "DISCORD_BOT_TOKEN": "tok-original",
            "DISCORD_APPLICATION_ID": "app-original",
            "CALF_HOST_URL": "orig-broker:9092",
        },
    )

    prompter = _fresh_run_prompter(
        name="scribe",
        description="d",
        discord_token="",  # keep token
        app_id="",  # keep app id
        guild="",
        channel="",
        broker="url",
        broker_url="",  # keep broker url
    )
    assert init.run(prompter, env_path=env_path, agents_dir=agents_dir) == 0

    env = read_env(env_path)
    assert env["DISCORD_BOT_TOKEN"] == "tok-original"
    assert env["DISCORD_APPLICATION_ID"] == "app-original"
    assert env["CALF_HOST_URL"] == "orig-broker:9092"
    assert env["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "anthropic"


# --- next-steps guidance ----------------------------------------------------


def test_next_steps_name_agent_and_mention_router_setup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    out = capsys.readouterr().out
    assert f"Set up agent 'scribe' in {agents_dir}." in out
    assert "@scribe hello" in out
    assert "calfcord router setup" in out


# --- _write_agent: branch-level unit tests ----------------------------------


def _write(agents_dir: Path, *, name: str, tools: list[str] | None = None, description: str = "desc") -> Path:
    """Invoke ``_write_agent`` with sensible fixed provider/model for brevity."""
    return init._write_agent(
        agents_dir,
        name=name,
        description=description,
        provider="anthropic",
        model="claude-haiku-4-5",
        tools=tools if tools is not None else ["read_file"],
    )


# Free-text descriptions that broke the old string-interpolated create path:
# a colon-space pair starts a YAML mapping, leading ``-`` a sequence, ``"``/``#``
# inject quoting/comment syntax. ``frontmatter.dumps`` must quote them so the
# file round-trips with the description preserved verbatim.
_TRICKY_DESCRIPTIONS = [
    "Calendar: book and prep meetings",
    'has "quotes" and #hash',
    "leading: colon",
    "- dashy",
]


@pytest.mark.parametrize("description", _TRICKY_DESCRIPTIONS)
def test_write_agent_create_roundtrips_tricky_descriptions(tmp_path: Path, description: str) -> None:
    """A free-text description with YAML-significant chars must survive the create path."""
    agents_dir = tmp_path / "agents"
    target = _write(agents_dir, name="scribe", description=description)
    # The file the create path wrote must re-parse with the exact input.
    assert parse_agent_md(target).description == description


@pytest.mark.parametrize("description", _TRICKY_DESCRIPTIONS)
def test_run_create_roundtrips_tricky_descriptions(tmp_path: Path, description: str) -> None:
    """The full wizard create flow must also round-trip a tricky description."""
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description=description)
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == description


def test_run_aborts_without_success_banner_when_write_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed agent write must return non-zero and never print the success banner.

    The create path validates before writing, so to force a *write* failure we
    monkeypatch the atomic-write helper to raise ``OSError`` (e.g. permission
    denied / no space). ``run`` must surface the error and stop — printing the
    "Set up agent ..." banner / next-steps on a half-configured install would
    send the operator off to boot processes against an agent that won't load.
    """

    def _boom(path: Path, payload: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(init, "_atomic_write", _boom)

    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d")
    rc = _run(prompter, tmp_path, agents_dir=agents_dir)

    out = capsys.readouterr().out
    assert rc != 0
    assert "error: could not create agent 'scribe'" in out
    assert "Set up agent" not in out
    assert not (agents_dir / "scribe.md").exists()


def test_write_agent_create_assistant_keeps_everything(tmp_path: Path) -> None:
    """Creating ``assistant.md`` itself never prunes anything (no different agent)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    path = _write(agents_dir, name="assistant")
    assert path == agents_dir / "assistant.md"
    assert path.is_file()
    assert parse_agent_md(path).agent_id == "assistant"


def test_write_agent_create_prunes_pristine_seed(tmp_path: Path) -> None:
    """Naming a new agent deletes a *pristine* seeded assistant.md."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    seed = agents_dir / "assistant.md"
    seed.write_text(
        "---\n"
        "name: assistant\n"
        "display_name: Assistant\n"
        f"description: {init._DEFAULT_DESCRIPTION}\n"
        "tools: []\n"
        "---\n\n"
        "You are Assistant, a helpful general-purpose AI teammate. Answer clearly.\n"
    )

    _write(agents_dir, name="scribe")

    assert (agents_dir / "scribe.md").is_file()
    assert not seed.exists()  # pristine seed pruned


def test_write_agent_create_keeps_customized_seed(tmp_path: Path) -> None:
    """A *customized* assistant.md (changed description) is preserved."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    seed = agents_dir / "assistant.md"
    seed.write_text(
        "---\n"
        "name: assistant\n"
        "display_name: Assistant\n"
        "description: My custom assistant for our team workflow.\n"
        "tools: []\n"
        "---\n\n"
        "You are Assistant, customized. Answer clearly.\n"
    )

    _write(agents_dir, name="scribe")

    assert (agents_dir / "scribe.md").is_file()
    assert seed.exists()  # customized seed kept
    assert parse_agent_md(seed).description == "My custom assistant for our team workflow."


def test_write_agent_create_keeps_malformed_seed(tmp_path: Path) -> None:
    """A malformed assistant.md is never deleted on a guess."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    seed = agents_dir / "assistant.md"
    seed.write_text("not valid frontmatter at all\n")

    _write(agents_dir, name="scribe")

    assert (agents_dir / "scribe.md").is_file()
    assert seed.exists()  # malformed → left untouched


def test_write_agent_update_in_place_preserves_body_and_display_name(tmp_path: Path) -> None:
    """Updating an existing agent rewrites fields but preserves body + display_name."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    target = agents_dir / "scribe.md"
    body = "You are Scribe, the dedicated note-taker. Keep meticulous records."
    target.write_text(
        "---\n"
        "name: scribe\n"
        "display_name: Aksel (Scribe)\n"
        "description: old description\n"
        "provider: openai\n"
        "model: gpt-5\n"
        "tools: [read_file]\n"
        "---\n\n"
        f"{body}\n"
    )

    _write(
        agents_dir,
        name="scribe",
        description="new description",
        tools=["read_file", "write_file"],
    )

    agent = parse_agent_md(target)
    assert agent.description == "new description"
    assert agent.provider == "anthropic"
    assert agent.model == "claude-haiku-4-5"
    assert set(agent.tools) == {"read_file", "write_file"}
    # Body and display_name are preserved across the in-place update.
    assert body in agent.system_prompt
    assert agent.display_name == "Aksel (Scribe)"


def test_run_updates_existing_agent_in_place(tmp_path: Path) -> None:
    """A full wizard pass naming an existing agent updates it without pruning."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    target = agents_dir / "scribe.md"
    body = "You are Scribe, the note-taker."
    target.write_text(
        "---\n"
        "name: scribe\n"
        "display_name: Scribe\n"
        "description: old\n"
        "provider: openai\n"
        "model: gpt-5\n"
        "tools: [read_file]\n"
        "---\n\n"
        f"{body}\n"
    )

    prompter = _fresh_run_prompter(name="scribe", description="updated desc")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0

    agent = parse_agent_md(target)
    assert agent.description == "updated desc"
    assert agent.provider == "anthropic"
    assert body in agent.system_prompt
