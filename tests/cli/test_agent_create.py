"""Tests for ``disco agent create`` — the reusable agent-creation flow.

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
from calfcord.cli._agents import STARTER_AGENT_NAME
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

    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=env_path, name=None, home=None)
    assert rc == 0

    md = agents_dir / "scribe.md"
    assert md.is_file()
    agent = parse_agent_md(md)
    assert agent.agent_id == "scribe"
    assert agent.description == "Takes notes"
    assert agent.provider == "anthropic"
    assert agent.model == "claude-haiku-4-5"
    assert set(agent.tools) == {"read_file", "web_search"}


def test_run_prints_created_and_next_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Success names the agent, then (on a dev run with no supervisor home) degrades to
    the honest manual bring-online sequence — a brand-new agent needs the workspace to
    (re)render with its ``.md`` declared, so ``disco start`` + ``disco agent start``."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0

    out = capsys.readouterr().out
    assert "Created agent 'scribe'." in out
    assert "Bring scribe online:" in out
    assert "disco start" in out
    assert "disco agent start scribe" in out


def test_run_passes_name_default_through(tmp_path: Path) -> None:
    """A given ``name`` pre-fills the prompt; the fake returns it, so it's used."""
    agents_dir = tmp_path / "agents"
    # The name prompt returns the default that ``run`` passed as name_default.
    prompter = FakePrompter(texts=["scout", "d"], confirms=[False])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scout", home=None) == 0
    assert (agents_dir / "scout.md").is_file()


def test_run_slugifies_typed_name(tmp_path: Path) -> None:
    """A typed friendly name is slugified into a valid stem before write."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="My Helper!")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
    assert (agents_dir / "my_helper.md").is_file()
    assert parse_agent_md(agents_dir / "my_helper.md").agent_id == "my_helper"


def test_run_blank_description_uses_default(tmp_path: Path) -> None:
    """A blank description falls back to the seed default."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe", description="")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == _agents.DEFAULT_DESCRIPTION


def test_run_tricky_description_roundtrips(tmp_path: Path) -> None:
    """A YAML-significant description ('Has: colon') survives the create path verbatim."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe", description="Has: colon")
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
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
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0
    assert seen == [agents_dir / "scribe.md"]


def test_run_declining_prompt_edit_does_not_launch_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Declining the optional prompt step never touches ``edit_system_prompt``."""
    agents_dir = tmp_path / "agents"

    def _boom(md_path: Path) -> None:
        raise AssertionError("edit_system_prompt must not run when the operator declines")

    from calfcord.cli import agent_edit

    monkeypatch.setattr(agent_edit, "edit_system_prompt", _boom)

    prompter = _prompter(name="scribe", confirms=[False])
    assert agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None) == 0


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
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scribe", home=None)

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


def test_create_agent_returns_created_agent_with_named_fields(tmp_path: Path) -> None:
    """The result is a ``CreatedAgent`` exposing ``.name``/``.provider`` so callers
    can't transpose the two same-typed strings (``init`` reads ``.provider``,
    ``agent create`` reads ``.name``)."""
    agents_dir = tmp_path / "agents"
    prompter = _prompter(name="scribe")
    created = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        prune_seed=False,
        offer_prompt=False,
    )
    assert isinstance(created, agent_create.CreatedAgent)
    assert created.name == "scribe"
    assert created.provider == "anthropic"


def test_create_agent_blank_name_with_one_non_assistant_edits_it_in_place(tmp_path: Path) -> None:
    """With ``name_default=None`` and exactly one existing non-``assistant`` agent, a
    blank typed name keeps the lone agent as the default — so the flow edits that
    agent in place and returns its name (it does not fall back to ``assistant``)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "scribe.md").write_text(
        "---\n"
        "name: scribe\n"
        "description: old\n"
        "provider: openai\n"
        "model: gpt-5\n"
        "tools: [read_file]\n"
        "---\n\n"
        "You are Scribe, the note-taker.\n",
        encoding="utf-8",
    )

    # Blank typed name → keeps the lone-agent default ("scribe").
    prompter = FakePrompter(texts=["   ", "updated desc"], confirms=[False])
    created = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        name_default=None,
        prune_seed=False,
        offer_prompt=False,
    )

    assert created.name == "scribe"
    assert not (agents_dir / "assistant.md").exists()
    assert {p.stem for p in agents_dir.glob("*.md")} == {"scribe"}
    assert parse_agent_md(agents_dir / "scribe.md").description == "updated desc"


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


def test_pick_tools_offers_unchecked_mcp_rows(monkeypatch) -> None:
    """The create wizard's tool checkbox includes ``mcp/<server>`` (and live
    per-tool) rows alongside the pre-checked builtins — unchecked, because
    MCP is an explicit grant that never rides the all-builtins default."""
    from calfcord.cli import _agents

    prompter = FakePrompter(checkboxes=[["terminal"]])
    selected = _agents.pick_tools(
        prompter,
        "helper",
        mcp_servers_fn=lambda: ["github"],
        live_tools_fn=lambda: {"github": ["search"]},
    )
    assert selected == ["terminal"]
    by_value = {c.value: c for c in prompter.last_checkbox_choices}
    assert by_value["mcp/github"].checked is False
    assert by_value["mcp/github/search"].checked is False
    # Builtins are still the pre-checked default.
    assert by_value["terminal"].checked is True


# ---------------------------------------------------------------------------
# Change B — standalone create requires an explicit name (no silent default,
# no silent overwrite of an existing agent).
# ---------------------------------------------------------------------------


def _seed_agent(agents_dir: Path, name: str, *, description: str = "old") -> None:
    """Write a minimal valid ``<name>.md`` so existing-name gating has a target."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "provider: openai\n"
        "model: gpt-5\n"
        "tools: [read_file]\n"
        "---\n\n"
        f"You are {name}.\n",
        encoding="utf-8",
    )


def test_run_blank_name_reprompts_no_silent_default(tmp_path: Path) -> None:
    """Standalone create has NO name default: a blank answer re-prompts (keep-asking)
    rather than silently falling back to an existing agent or the starter name."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "assistant", description="lone-existing")
    # First name answer is blank (must re-ask), then a real name is supplied.
    prompter = FakePrompter(texts=["", "scribe", "d"], confirms=[False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None)
    assert rc == 0
    # The blank was NOT taken as "edit the lone existing agent": a fresh scribe
    # landed and the pre-existing assistant is untouched.
    assert (agents_dir / "scribe.md").is_file()
    assert parse_agent_md(agents_dir / "assistant.md").description == "lone-existing"


def test_run_existing_name_gate_declined_reprompts_for_different_name(tmp_path: Path) -> None:
    """Naming an existing agent triggers the explicit 'update it?' gate; declining
    (default No) re-prompts for a different name — never a silent overwrite."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe", description="original")
    # Type the existing name -> gate confirm=False -> re-prompt -> type a new name.
    prompter = FakePrompter(texts=["scribe", "scout", "d"], confirms=[False, False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None)
    assert rc == 0
    # The declined existing agent is untouched; the fresh different-named one exists.
    assert parse_agent_md(agents_dir / "scribe.md").description == "original"
    assert (agents_dir / "scout.md").is_file()


def test_run_existing_name_gate_accepted_updates_in_place(tmp_path: Path) -> None:
    """Accepting the 'update it?' gate edits the existing agent in place."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe", description="original")
    # Type the existing name -> gate confirm=True -> proceed to update; new desc.
    prompter = FakePrompter(texts=["scribe", "updated desc"], confirms=[True, False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name=None, home=None)
    assert rc == 0
    assert parse_agent_md(agents_dir / "scribe.md").description == "updated desc"
    assert {p.stem for p in agents_dir.glob("*.md")} == {"scribe"}


def test_run_positional_name_pre_answers_the_prompt(tmp_path: Path) -> None:
    """A positional ``disco agent create scribe`` pre-answers the name prompt even
    under the required-name policy (no blank re-ask when the CLI supplied a name)."""
    agents_dir = tmp_path / "agents"
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False])
    rc = agent_create.run(prompter, agents_dir=agents_dir, env_path=tmp_path / ".env", name="scribe", home=None)
    assert rc == 0
    assert (agents_dir / "scribe.md").is_file()


def test_create_agent_init_path_keeps_starter_default(tmp_path: Path) -> None:
    """``init``'s path (``require_name=False``, ``name_default=None``) is UNCHANGED:
    a blank name enter-through still defaults to the seeded 'assistant'."""
    agents_dir = tmp_path / "agents"
    # Blank name -> init path keeps the STARTER default ('assistant'); no gate confirm.
    prompter = FakePrompter(texts=["", "d"], confirms=[False])
    created = agent_create.create_agent(
        prompter,
        agents_dir=agents_dir,
        env_path=tmp_path / ".env",
        name_default=None,
        prune_seed=True,
        offer_prompt=False,
        require_name=False,
    )
    assert created.name == STARTER_AGENT_NAME
    assert (agents_dir / f"{STARTER_AGENT_NAME}.md").is_file()


# ---------------------------------------------------------------------------
# Change A — standalone create ends LIVE: offer to start the agent, opening or
# reloading the workspace as its brand-new-slot status requires, then confirming
# presence on the mesh. All world-touching calls are injected seams.
# ---------------------------------------------------------------------------


class _FinishRecorder:
    """Records the live-finish orchestration seams so tests assert what ran.

    Every seam is an async stub returning a scripted exit code / presence result;
    ``pc_binary`` reports the supervisor as available so the native path (not the
    dev degrade) is exercised.
    """

    def __init__(
        self,
        *,
        running: bool = False,
        start_rc: int = 0,
        stop_rc: int = 0,
        agent_rc: int = 0,
        present: bool = True,
    ) -> None:
        self._running = running
        self._start_rc = start_rc
        self._stop_rc = stop_rc
        self._agent_rc = agent_rc
        self._present = present
        self.calls: list[str] = []
        self.start_kwargs: list[dict] = []
        self.agent_kwargs: list[dict] = []
        self.presence_kwargs: list[dict] = []

    async def workspace_running(self, home: Path) -> bool:
        self.calls.append("workspace_running")
        return self._running

    async def start(self, home, **kwargs) -> int:
        self.calls.append("start")
        self.start_kwargs.append({"home": home, **kwargs})
        return self._start_rc

    async def stop(self, home, **kwargs) -> int:
        self.calls.append("stop")
        return self._stop_rc

    async def agent_start(self, home, **kwargs) -> int:
        self.calls.append("agent_start")
        self.agent_kwargs.append({"home": home, **kwargs})
        return self._agent_rc

    async def presence(self, server_urls, **kwargs) -> bool:
        self.calls.append("presence")
        self.presence_kwargs.append({"server_urls": server_urls, **kwargs})
        return self._present

    def pc_binary(self) -> str:
        return "process-compose"


def _run_live(
    prompter: FakePrompter,
    tmp_path: Path,
    finish: _FinishRecorder,
    *,
    name: str | None = None,
) -> int:
    """Drive ``agent_create.run`` with every orchestration seam stubbed."""
    return agent_create.run(
        prompter,
        agents_dir=tmp_path / "agents",
        env_path=tmp_path / ".env",
        name=name,
        home=tmp_path,
        server_urls="localhost:9092",
        start_fn=finish.start,
        stop_fn=finish.stop,
        agent_start_fn=finish.agent_start,
        presence_fn=finish.presence,
        workspace_running_fn=finish.workspace_running,
        pc_binary_fn=finish.pc_binary,
    )


def test_run_start_now_yes_workspace_not_running_opens_and_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Start-now yes with the workspace DOWN: open the workspace (no reload), then
    bring the agent online; presence seen prints the exact online line."""
    finish = _FinishRecorder(running=False, present=True)
    # confirms: [edit-prompt=No, Start now?=Yes]
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    # No stop (workspace wasn't running) — just start -> agent_start -> presence.
    assert finish.calls == ["workspace_running", "start", "agent_start", "presence"]
    assert finish.agent_kwargs[0]["name"] == "scribe"
    out = capsys.readouterr().out
    assert "scribe is online — say @scribe hello in Discord" in out


def test_run_start_now_yes_presence_timeout_degrades(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Agent starts but presence is not seen in time: the honest 'try it yourself /
    disco doctor' downgrade prints instead of a green light that lies."""
    finish = _FinishRecorder(running=False, present=False)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    out = capsys.readouterr().out
    assert "scribe is online — say" not in out
    assert "disco doctor" in out


def test_run_start_now_yes_workspace_running_reload_confirmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Start-now yes with the workspace UP: the brand-new agent needs the one-time
    reload — confirmed, it stops then restarts the workspace before agent_start."""
    finish = _FinishRecorder(running=True, present=True)
    # confirms: [edit-prompt=No, Start now?=Yes, reload?=Yes]
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert finish.calls == ["workspace_running", "stop", "start", "agent_start", "presence"]


def test_run_start_now_yes_workspace_running_reload_declined(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining the reload does NOT touch the running workspace; it prints the
    manual reload sequence (disco stop / start / agent start) and stops."""
    finish = _FinishRecorder(running=True)
    # confirms: [edit-prompt=No, Start now?=Yes, reload?=No]
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True, False])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    # Probed state, then bailed — no stop/start/agent_start.
    assert finish.calls == ["workspace_running"]
    out = capsys.readouterr().out
    assert "disco stop" in out
    assert "disco agent start scribe" in out


def test_run_start_now_no_running_prints_reload_manual(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining 'Start now?' with the workspace UP prints the reload manual (stop
    first), and orchestrates nothing."""
    finish = _FinishRecorder(running=True)
    # confirms: [edit-prompt=No, Start now?=No]
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, False])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert finish.calls == ["workspace_running"]
    out = capsys.readouterr().out
    assert "disco stop" in out
    assert "disco agent start scribe" in out


def test_run_start_now_no_not_running_prints_plain_manual(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining 'Start now?' with the workspace DOWN prints the plain manual (no
    stop line), and orchestrates nothing."""
    finish = _FinishRecorder(running=False)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, False])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 0
    assert finish.calls == ["workspace_running"]
    out = capsys.readouterr().out
    assert "disco stop" not in out
    assert "disco start" in out
    assert "disco agent start scribe" in out


def test_run_start_now_workspace_open_failure_propagates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If opening the workspace fails, the non-zero code propagates and the agent is
    never started (no false 'online')."""
    finish = _FinishRecorder(running=False, start_rc=1)
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False, True])
    rc = _run_live(prompter, tmp_path, finish, name="scribe")
    assert rc == 1
    assert "agent_start" not in finish.calls
    assert "presence" not in finish.calls


def test_run_dev_run_degrades_without_prompting_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dev run (home=None) never prompts 'Start now?' (it can't orchestrate the
    install-scoped supervisor); it prints the honest manual next-steps instead."""
    # Only the edit-prompt confirm is scripted; a 'Start now?' prompt here would
    # dequeue-empty and raise, proving the degrade path never prompts to start.
    prompter = FakePrompter(texts=["scribe", "d"], confirms=[False])
    rc = agent_create.run(
        prompter, agents_dir=tmp_path / "agents", env_path=tmp_path / ".env", name="scribe", home=None
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Bring scribe online:" in out
    assert "disco start" in out
