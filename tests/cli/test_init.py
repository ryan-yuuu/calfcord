"""Tests for ``calfcord init``'s **agent-creation** phase + ``write_agent`` branches.

The reworked ``init`` is a *composer*: its Discord sub-flow, broker step, and
ends-live finish are exercised end to end in ``test_init_wizard.py`` (which
injects the Discord / supervisor / first-reply seams). This module stays focused
on the one phase that survived the rework unchanged — the shared
``agent_create.create_agent`` flow ``init`` runs first (name → describe →
provider/model → tools → write) — plus the ``_agents.write_agent`` pruning /
in-place-update branches.

These drive ``init.run`` in **dev mode** (``home=None``) with **no Discord token**
(an empty token skips discovery), so the run reaches the agent write and the
honest dev-mode degrade without touching Discord, a broker, or the supervisor.
The provider sub-flow is delegated to ``configure_provider`` (real SDK / catalog);
every test monkeypatches it to a fixed ``(provider, model)`` so no network, key,
or OAuth fires. Assertions target the written ``agents/<name>.md`` (via
``parse_agent_md``) and the install default provider in ``.env``.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import _agents, agent_create, init
from calfcord.cli._envfile import read_env
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

    ``init`` now delegates agent creation to ``agent_create.create_agent``, which
    is where ``configure_provider`` is called, so the stub is installed there. It
    consumes no prompts, so tests don't script provider answers — keeping every
    wizard test free of any provider SDK / network.
    """

    def _fixed(prompter: object, **_: object) -> tuple[str, str]:
        return _FIXED_PROVIDER

    monkeypatch.setattr(agent_create, "configure_provider", _fixed)


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
    broker: str = "native",
    checkboxes: list[list[str]] | None = None,
) -> FakePrompter:
    """Script the prompts the agent-creation path consumes, then degrade in dev mode.

    With no Discord token (the secret prompt answered empty) the Discord step is
    skipped entirely — no invite/app-id/guild/channel prompts fire. So after the
    provider sub-flow is stubbed away, the only prompts are: text(name),
    text(description), checkbox(tools), secret(token="" → skip Discord),
    select(broker). Dev mode (``home=None`` in :func:`_run`) then degrades the
    finish to printed next-steps, consuming no further prompts.
    """
    return FakePrompter(
        selects=[broker],
        texts=[name, description],
        secrets=[""],  # empty token → Discord discovery skipped
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
    assert parse_agent_md(agents_dir / "scribe.md").description == _agents.DEFAULT_DESCRIPTION


# --- tools checkbox ---------------------------------------------------------


def test_tools_checkbox_offers_all_builtins_prechecked(tmp_path: Path) -> None:
    from calfcord.tools import TOOL_REGISTRY

    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0

    builtin_rows = {c.value: c.checked for c in prompter.last_checkbox_choices}
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


def test_security_caution_prints_when_dangerous_tool_selected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agents_dir = tmp_path / "agents"
    # execute_code runs arbitrary code on the tools host — selecting it (even
    # without a write tool) must trigger the caution.
    prompter = _fresh_run_prompter(
        name="scribe", description="d", checkboxes=[["execute_code", "read_file"]]
    )
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    out = capsys.readouterr().out
    assert "code execution + file write access" in out
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


# --- broker step (agent-creation-adjacent; full broker/discord flow lives in
#     test_init_wizard.py) ------------------------------------------------------


def test_broker_native_sets_local_url(tmp_path: Path) -> None:
    """The native broker choice still seeds ``CALF_HOST_URL`` (the live finish
    starts the broker, so — unlike the old flow — no command is printed here)."""
    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d", broker="native")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0
    assert read_env(tmp_path / ".env")["CALF_HOST_URL"] == "localhost:9092"


# --- write_agent: branch-level unit tests -----------------------------------


def _write(
    agents_dir: Path,
    *,
    name: str,
    tools: list[str] | None = None,
    description: str = "desc",
    prune: bool = False,
) -> Path:
    """Invoke ``_agents.write_agent`` with sensible fixed provider/model for brevity."""
    return _agents.write_agent(
        agents_dir,
        name=name,
        description=description,
        provider="anthropic",
        model="claude-haiku-4-5",
        tools=tools if tools is not None else ["read_file"],
        prune_seed=prune,
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

    monkeypatch.setattr(_agents, "atomic_write", _boom)

    agents_dir = tmp_path / "agents"
    prompter = _fresh_run_prompter(name="scribe", description="d")
    rc = _run(prompter, tmp_path, agents_dir=agents_dir)

    out = capsys.readouterr().out
    assert rc != 0
    assert "error: could not create agent" in out
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
    """With ``prune_seed`` (init's first-run), naming a new agent deletes a *pristine* seed."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    seed = agents_dir / "assistant.md"
    seed.write_text(
        "---\n"
        "name: assistant\n"
        f"description: {_agents.DEFAULT_DESCRIPTION}\n"
        "tools: []\n"
        "---\n\n"
        "You are Assistant, a helpful general-purpose AI teammate. Answer clearly.\n"
    )

    _write(agents_dir, name="scribe", prune=True)

    assert (agents_dir / "scribe.md").is_file()
    assert not seed.exists()  # pristine seed pruned


def test_write_agent_create_keeps_seed_without_prune_opt_in(tmp_path: Path) -> None:
    """Without ``prune_seed`` (e.g. ``agent create``), a pristine seed is left untouched."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    seed = agents_dir / "assistant.md"
    seed.write_text(
        "---\n"
        "name: assistant\n"
        f"description: {_agents.DEFAULT_DESCRIPTION}\n"
        "tools: []\n"
        "---\n\n"
        "You are Assistant, a helpful general-purpose AI teammate. Answer clearly.\n"
    )

    _write(agents_dir, name="scribe")  # prune defaults off

    assert (agents_dir / "scribe.md").is_file()
    assert seed.exists()  # starter preserved when not opting in


def test_write_agent_create_keeps_customized_seed(tmp_path: Path) -> None:
    """A *customized* assistant.md (changed description) is preserved."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    seed = agents_dir / "assistant.md"
    seed.write_text(
        "---\n"
        "name: assistant\n"
        "description: My custom assistant for our team workflow.\n"
        "tools: []\n"
        "---\n\n"
        "You are Assistant, customized. Answer clearly.\n"
    )

    _write(agents_dir, name="scribe", prune=True)  # prune requested, but seed is customized

    assert (agents_dir / "scribe.md").is_file()
    assert seed.exists()  # customized seed kept even when pruning is requested
    assert parse_agent_md(seed).description == "My custom assistant for our team workflow."


def test_write_agent_create_keeps_malformed_seed(tmp_path: Path) -> None:
    """A malformed assistant.md is never deleted on a guess."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    seed = agents_dir / "assistant.md"
    seed.write_text("not valid frontmatter at all\n")

    _write(agents_dir, name="scribe", prune=True)  # prune requested, but seed won't parse

    assert (agents_dir / "scribe.md").is_file()
    assert seed.exists()  # malformed → never deleted on a guess


def test_write_agent_update_in_place_preserves_body(tmp_path: Path) -> None:
    """Updating an existing agent rewrites fields but preserves its body."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    target = agents_dir / "scribe.md"
    body = "You are Scribe, the dedicated note-taker. Keep meticulous records."
    target.write_text(
        "---\n"
        "name: scribe\n"
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
    # The body is preserved across the in-place update.
    assert body in agent.system_prompt


def test_run_updates_existing_agent_in_place(tmp_path: Path) -> None:
    """A full wizard pass naming an existing agent updates it without pruning."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    target = agents_dir / "scribe.md"
    body = "You are Scribe, the note-taker."
    target.write_text(
        "---\n"
        "name: scribe\n"
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


def test_blank_name_with_one_non_assistant_agent_edits_it_in_place(tmp_path: Path) -> None:
    """A blank name on an install carrying exactly one non-``assistant`` agent edits
    that lone agent in place rather than spawning a new ``assistant.md``.

    The create flow defaults the name to the sole existing agent (a re-run editing
    it), and a blank answer keeps that default — so the operator who just presses
    enter ends with the same single agent, updated, not a second starter.
    """
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    target = agents_dir / "scribe.md"
    body = "You are Scribe, the note-taker."
    target.write_text(
        "---\n"
        "name: scribe\n"
        "description: old\n"
        "provider: openai\n"
        "model: gpt-5\n"
        "tools: [read_file]\n"
        "---\n\n"
        f"{body}\n"
    )

    # Blank name → keeps the lone-agent default ("scribe"); edits it in place.
    prompter = _fresh_run_prompter(name="   ", description="updated desc")
    assert _run(prompter, tmp_path, agents_dir=agents_dir) == 0

    # No new assistant.md; the one agent is still "scribe", now updated.
    assert not (agents_dir / "assistant.md").exists()
    assert {p.stem for p in agents_dir.glob("*.md")} == {"scribe"}
    agent = parse_agent_md(target)
    assert agent.agent_id == "scribe"
    assert agent.description == "updated desc"
    assert body in agent.system_prompt
