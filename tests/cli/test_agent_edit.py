"""Tests for ``disco agent edit`` — the interactive field-menu editor.

The menu is pure logic over an injected :class:`Prompter`, so these tests never
touch a TTY, InquirerPy, or a real ``$EDITOR`` subprocess. A scripted
:class:`FakePrompter` drives the loop: the ``select`` queue answers both the
menu ("which field?") and any ``select``-kind field, in call order, ending with
the ``__done__`` sentinel to exit; ``text`` / ``confirm`` queues supply the new
value for the chosen field. We seed real ``.md`` files and re-parse them with
``parse_agent_md`` to verify the on-disk effect, and assert error lines via
``capsys``.

``edit_system_prompt`` is exercised without launching an editor: ``subprocess.run``
is monkeypatched to a fake that writes the desired body into the temp file the
helper hands it (and ``$EDITOR`` is set so the no-launch path is deterministic),
so the validated save path runs end-to-end with no process spawned.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import frontmatter
import pytest

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import agent_edit
from calfcord.cli._prompts import Choice, Prompter

_DONE = "__done__"


class FakePrompter:
    """A scripted :class:`Prompter` that drives the edit menu.

    ``select`` answers both the menu pick and any ``select``-kind field, popped
    in call order; ``text`` / ``secret`` / ``confirm`` pop their own queues.
    Running a queue dry raises rather than hanging, so a miscounted script fails
    loudly. ``checkbox`` returns the pre-checked rows by default (the editor's
    ``tools`` row delegates to ``agent_tools.run``, which is usually stubbed in
    these tests anyway).
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
        self.last_select_choices: list[Choice] | None = None

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        if not self._selects:
            raise AssertionError(f"unexpected select(): {message!r}")
        self.last_select_choices = choices
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


def _seed_agent(agents_dir: Path, name: str = "scribe", **meta: object) -> Path:
    """Write a minimal, valid ``agents/<name>.md`` and return its path."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {
        "name": name,
        "description": "Takes notes.",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
    }
    metadata.update(meta)
    post = frontmatter.Post("You are Scribe, a helpful teammate.", **metadata)
    md_path = agents_dir / f"{name}.md"
    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return md_path


# --- simple-field edits (validated write path) ------------------------------


def test_edit_description_writes_via_validated_path(tmp_path: Path) -> None:
    md = _seed_agent(tmp_path)
    prompter = FakePrompter(selects=["description", _DONE], texts=["New description."])
    rc = agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe")
    assert rc == 0
    assert parse_agent_md(md).description == "New description."


def test_edit_thinking_effort_select_writes(tmp_path: Path) -> None:
    md = _seed_agent(tmp_path)
    # menu→thinking_effort, field-select→xhigh, menu→done.
    prompter = FakePrompter(selects=["thinking_effort", "xhigh", _DONE])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0
    assert parse_agent_md(md).thinking_effort == "xhigh"


def test_edit_rejected_value_reports_error_and_keeps_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rejected value prints 'error:', leaves the file untouched, and the menu continues."""
    md = _seed_agent(tmp_path)
    # Bad thinking_effort first (rejected, menu continues), then a good
    # description, then done. The menu ``select`` queue answers both the menu
    # pick and the select-field value, in call order.
    prompter = FakePrompter(
        selects=["thinking_effort", "ludicrous", "description", _DONE],
        texts=["Still editable."],
    )
    rc = agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe")
    assert rc == 0

    out = capsys.readouterr().out
    assert "error:" in out
    agent = parse_agent_md(md)
    # The bad write never applied (the seed omitted thinking_effort → still unset).
    assert agent.thinking_effort is None
    # ...but the *subsequent* edit in the same session still applied — the menu
    # survived the bad value.
    assert agent.description == "Still editable."
    assert list(tmp_path.glob(".*.tmp")) == []


def test_unchanged_text_value_writes_nothing(tmp_path: Path) -> None:
    """Re-entering the current value issues no write (no restart hint, file identical)."""
    md = _seed_agent(tmp_path, description="Takes notes.")
    original = md.read_text(encoding="utf-8")
    prompter = FakePrompter(selects=["description", _DONE], texts=["Takes notes."])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0
    assert md.read_text(encoding="utf-8") == original


def test_unchanged_select_value_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-selecting the current ``thinking_effort`` is a no-op: no write, no hint."""
    md = _seed_agent(tmp_path, thinking_effort="high")
    original = md.read_text(encoding="utf-8")
    # menu→thinking_effort, field-select returns the SAME value, menu→done.
    prompter = FakePrompter(selects=["thinking_effort", "high", _DONE])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0

    assert md.read_text(encoding="utf-8") == original
    assert "Restart" not in capsys.readouterr().out


def test_unchanged_bool_value_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Confirming the current ``memory`` value is a no-op: no write, no hint."""
    md = _seed_agent(tmp_path, memory=False)
    original = md.read_text(encoding="utf-8")
    # menu→memory, confirm returns the SAME value (False), menu→done.
    prompter = FakePrompter(selects=["memory", _DONE], confirms=[False])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0

    assert md.read_text(encoding="utf-8") == original
    assert "Restart" not in capsys.readouterr().out


def test_restart_hint_printed_only_when_changed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The restart hint appears after a real change and is absent on a no-op session."""
    md = _seed_agent(tmp_path, description="Takes notes.")

    # Change nothing: select a field, re-enter its value, done → no hint.
    noop = FakePrompter(selects=["description", _DONE], texts=["Takes notes."])
    agent_edit.run(noop, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe")
    assert "Restart" not in capsys.readouterr().out

    # Change something → the EXACT terse next-step block (behavior #3): the
    # restart sentence (naming the provider-wide caveat), a blank line, the
    # two-space-indented `agent restart <name>` command.
    changed = FakePrompter(selects=["description", _DONE], texts=["Changed."])
    agent_edit.run(changed, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe")
    out = capsys.readouterr().out
    assert (
        "Restart scribe to apply (and any other agents on anthropic if the "
        "provider/key changed):\n\n  disco agent restart scribe"
    ) in out
    assert parse_agent_md(md).description == "Changed."


# --- provider_model row -----------------------------------------------------


def test_edit_provider_model_writes_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider/model row writes both via the configure_provider result."""
    md = _seed_agent(tmp_path, provider="anthropic", model="claude-sonnet-4-5")

    def _fixed(prompter: object, **_: object) -> tuple[str, str]:
        return ("openai", "gpt-5-mini")

    monkeypatch.setattr(agent_edit, "configure_provider", _fixed)

    prompter = FakePrompter(selects=["provider_model", _DONE])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0

    agent = parse_agent_md(md)
    assert agent.provider == "openai"
    assert agent.model == "gpt-5-mini"


# --- tools row delegates to the existing checkbox editor --------------------


def test_edit_tools_delegates_to_agent_tools_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``tools`` row calls ``agent_tools.run`` with the resolved agent name."""
    _seed_agent(tmp_path)
    calls: list[dict[str, object]] = []

    def _sentinel(prompter: object, *, agents_dir: Path, name: str | None) -> int:
        calls.append({"agents_dir": agents_dir, "name": name})
        return 0

    monkeypatch.setattr(agent_edit.agent_tools, "run", _sentinel)

    prompter = FakePrompter(selects=["tools", _DONE])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0

    assert calls == [{"agents_dir": tmp_path, "name": "scribe"}]


# --- prompt row + edit_system_prompt ($EDITOR helper) -----------------------


def _editor_writing(new_body: str):
    """Build a fake ``subprocess.run`` that writes ``new_body`` into the temp file.

    The helper launches ``[*editor_args, tmp_path]``; the fake grabs the last
    argv element (the temp file) and overwrites it with ``new_body``, then
    returns a zero-exit sentinel — emulating an editor session without spawning
    a process.
    """

    def _run(argv: list[str], *, check: bool = False):
        Path(argv[-1]).write_text(new_body, encoding="utf-8")

        class _Completed:
            returncode = 0

        return _Completed()

    return _run


def test_edit_system_prompt_saves_new_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed body is saved via update_system_prompt without launching a real editor."""
    md = _seed_agent(tmp_path)
    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(agent_edit.subprocess, "run", _editor_writing("You are Scribe, now revised and sharper."))

    agent_edit.edit_system_prompt(md)
    assert parse_agent_md(md).system_prompt == "You are Scribe, now revised and sharper."


def test_edit_system_prompt_no_change_leaves_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Saving the editor with the body unchanged writes nothing and notes it."""
    md = _seed_agent(tmp_path)
    original = md.read_text(encoding="utf-8")
    before = parse_agent_md(md).system_prompt
    monkeypatch.setenv("EDITOR", "fake-editor")
    # The fake writes back exactly the current body → no-op.
    monkeypatch.setattr(agent_edit.subprocess, "run", _editor_writing(before))

    agent_edit.edit_system_prompt(md)
    assert md.read_text(encoding="utf-8") == original
    assert "unchanged" in capsys.readouterr().out.lower()


def test_edit_system_prompt_emptied_body_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An emptied body (whitespace only) is rejected as a no-op, not written."""
    md = _seed_agent(tmp_path)
    before = parse_agent_md(md).system_prompt
    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(agent_edit.subprocess, "run", _editor_writing("   \n  "))

    agent_edit.edit_system_prompt(md)
    assert parse_agent_md(md).system_prompt == before


def test_edit_system_prompt_missing_editor_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing $EDITOR binary prints a clear hint, never a traceback, never writes."""
    md = _seed_agent(tmp_path)
    before = parse_agent_md(md).system_prompt
    monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor")

    def _missing(argv: list[str], *, check: bool = False):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(agent_edit.subprocess, "run", _missing)

    agent_edit.edit_system_prompt(md)
    out = capsys.readouterr().out
    assert "error:" in out
    assert "$EDITOR" in out
    assert parse_agent_md(md).system_prompt == before


def test_edit_system_prompt_non_utf8_body_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An editor that saves a non-UTF-8 body must not raise out of the helper.

    ``edit_system_prompt`` runs inside the menu loop AND ``agent create``'s prompt
    step, so a ``UnicodeDecodeError`` reading the edited temp file back has to be
    caught: it prints one ``error:`` line and leaves the on-disk body untouched.
    The fake ``subprocess.run`` writes invalid UTF-8 bytes into the temp file the
    helper hands it — mirroring an operator who saved a non-UTF-8 buffer.
    """
    md = _seed_agent(tmp_path)
    original = md.read_text(encoding="utf-8")
    before = parse_agent_md(md).system_prompt
    monkeypatch.setenv("EDITOR", "fake-editor")

    def _writes_bad_bytes(argv: list[str], *, check: bool = False):
        Path(argv[-1]).write_bytes(b"\xff\xfe bad")

        class _Completed:
            returncode = 0

        return _Completed()

    monkeypatch.setattr(agent_edit.subprocess, "run", _writes_bad_bytes)

    # Must not raise — the contract is "never escapes out of the menu".
    agent_edit.edit_system_prompt(md)

    out = capsys.readouterr().out
    assert "error: could not read the edited prompt" in out
    # The unreadable edit is discarded; the prompt body is unchanged on disk.
    assert md.read_text(encoding="utf-8") == original
    assert parse_agent_md(md).system_prompt == before


def test_edit_prompt_row_routes_through_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The menu's ``system_prompt`` row routes to edit_system_prompt on that file."""
    md = _seed_agent(tmp_path)
    seen: list[Path] = []
    monkeypatch.setattr(agent_edit, "edit_system_prompt", lambda p: seen.append(p))

    prompter = FakePrompter(selects=["system_prompt", _DONE])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0
    assert seen == [md]


# --- agent resolution (given / pick / empty dir) ----------------------------


def test_run_unknown_named_agent_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_agent(tmp_path)
    prompter = FakePrompter()
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="ghost") == 1
    assert "ghost" in capsys.readouterr().out


def test_run_no_name_picks_via_select(tmp_path: Path) -> None:
    """With no name, the picker lists detected agents and the chosen one is edited."""
    _seed_agent(tmp_path, name="alpha")
    md_beta = _seed_agent(tmp_path, name="beta")
    # First select picks the agent (beta); then menu→description, value, done.
    prompter = FakePrompter(selects=["beta", "description", _DONE], texts=["Edited beta."])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name=None) == 0
    assert parse_agent_md(md_beta).description == "Edited beta."


def test_run_empty_dir_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "agents"
    empty.mkdir()
    prompter = FakePrompter()
    assert agent_edit.run(prompter, agents_dir=empty, env_path=tmp_path / ".env", name=None) == 1
    assert "no agents" in capsys.readouterr().out


def test_done_immediately_is_a_clean_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Choosing Done first thing exits 0 with no restart hint (nothing changed)."""
    _seed_agent(tmp_path)
    prompter = FakePrompter(selects=[_DONE])
    assert agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe") == 0
    assert "Restart" not in capsys.readouterr().out


def test_menu_offers_done_row_and_field_rows(tmp_path: Path) -> None:
    """The menu lists every FIELDS row plus a trailing Done row."""
    from calfcord.cli._fields import FIELDS

    _seed_agent(tmp_path)
    prompter = FakePrompter(selects=[_DONE])
    agent_edit.run(prompter, agents_dir=tmp_path, env_path=tmp_path / ".env", name="scribe")

    values = [c.value for c in (prompter.last_select_choices or [])]
    assert values == [f.key for f in FIELDS] + [_DONE]


def test_fake_prompter_satisfies_protocol() -> None:
    """Guard that the test fake stays structurally compatible with the seam."""
    assert isinstance(FakePrompter(), Prompter)
