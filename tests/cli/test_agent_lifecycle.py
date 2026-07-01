"""Tests for ``disco agent set`` / ``rename`` / ``delete``.

These commands mutate an agent's ``agents/<name>.md``, so the tests seed real
files and re-parse / re-stat to assert the on-disk effect. The contracts that
matter:

* ``set`` writes through the validated paths, so a bad value (a rejected
  ``thinking_effort``) fails with the file untouched.
* ``rename`` moves the ``.md`` and never loses the agent: the ``.md`` lands under
  the new name and the old ``.md`` is gone. Renaming onto an existing agent is a
  hard error that does not clobber the target.
* ``delete`` confirms first (via an injected fake prompter), removes the ``.md``,
  honors ``yes``, and treats a declined confirm as a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import agent_lifecycle
from calfcord.cli._prompts import Choice, Prompter


class FakePrompter:
    """A :class:`Prompter` fake that scripts the ``confirm`` answer for delete.

    Only ``confirm`` is exercised by these flows; the other prompt shapes raise
    if hit so an unscripted prompt is a loud test failure, not a hang.
    """

    def __init__(self, *, confirm_result: bool = True) -> None:
        self._confirm_result = confirm_result
        self.confirm_calls: list[str] = []

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        raise AssertionError(f"unexpected select(): {message!r}")

    def text(self, message: str, *, default: str = "") -> str:
        raise AssertionError(f"unexpected text(): {message!r}")

    def secret(self, message: str) -> str:
        raise AssertionError(f"unexpected secret(): {message!r}")

    def confirm(self, message: str, *, default: bool = False) -> bool:
        self.confirm_calls.append(message)
        return self._confirm_result

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        raise AssertionError(f"unexpected checkbox(): {message!r}")


def test_fake_prompter_satisfies_protocol() -> None:
    assert isinstance(FakePrompter(), Prompter)


def _seed_agent(agents_dir: Path, name: str, *, tools_line: str | None = "[read_file, terminal]") -> Path:
    """Write a minimal valid ``agents/<name>.md`` and return its path."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: Test {name}.",
        "provider: anthropic",
    ]
    if tools_line is not None:
        lines.append(f"tools: {tools_line}")
    lines += ["---", "", f"You are {name}.", ""]
    md_path = agents_dir / f"{name}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


# --- set --------------------------------------------------------------------


def test_set_writes_multiple_simple_fields(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")
    rc = agent_lifecycle.run_set(
        agents_dir, "scribe", {"description": "New desc.", "thinking_effort": "high"}
    )
    assert rc == 0

    reparsed = parse_agent_md(md_path)
    assert reparsed.description == "New desc."
    assert reparsed.thinking_effort == "high"


def test_set_success_prints_next_step_block(tmp_path: Path, capsys) -> None:
    """A successful ``set`` names the fields it wrote, then the EXACT terse
    next-step block (behavior #3): the restart sentence (naming the resolved
    provider in the provider-wide caveat), a blank line, the indented
    `agent restart <name>` command — the roster verb, not the old runner banner."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")  # seed provider is anthropic
    assert agent_lifecycle.run_set(agents_dir, "scribe", {"description": "New desc."}) == 0
    out = capsys.readouterr().out
    assert "Updated scribe (description)." in out
    assert (
        "Restart scribe to apply (and any other agents on anthropic if the "
        "provider/key changed):\n\n  disco agent restart scribe"
    ) in out


def test_set_success_survives_unparsable_md_on_provider_reread(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A success line was already printed, so a failing post-write provider re-read
    must NOT escape: ``run_set`` still reports success + the restart hint, just
    without the provider parenthetical.

    The next-step caveat re-reads the ``.md`` off disk to name the agent's CURRENT
    provider. If that re-read raises (a now-unparsable file — e.g. an external edit
    racing the write), the only ``try/except`` is in the per-field loop, so the
    error would escape AFTER ``run_set`` already printed ``Updated …`` — leaving the
    operator with a traceback on an otherwise-successful command. The re-read must
    be guarded.
    """
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")

    # The per-field write uses md_writer (not parse_agent_md), so the edit lands
    # cleanly; only the final next-step re-read is forced to fail, simulating a
    # ``.md`` that became unparsable between the write and the re-read.
    def _boom_reread(path):
        raise ValueError(f"{path}: malformed YAML frontmatter")

    monkeypatch.setattr(agent_lifecycle, "parse_agent_md", _boom_reread)

    rc = agent_lifecycle.run_set(agents_dir, "scribe", {"description": "New desc."})

    assert rc == 0  # the command still succeeded; no traceback escaped
    out = capsys.readouterr().out
    assert "Updated scribe (description)." in out
    # The restart hint still appears (so the operator knows to apply the change),
    # naming the agent and the roster `restart` verb.
    assert "disco agent restart scribe" in out


def test_set_tools_writes_exactly_those(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe", tools_line=None)
    assert agent_lifecycle.run_set(agents_dir, "scribe", {"tools": "read_file, terminal"}) == 0
    assert parse_agent_md(md_path).tools == ("read_file", "terminal")


def test_set_provider_and_model_keys(tmp_path: Path) -> None:
    """``provider``/``model`` are standalone update keys (not a FIELDS_BY_KEY row)."""
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")
    rc = agent_lifecycle.run_set(agents_dir, "scribe", {"provider": "openai", "model": "gpt-5"})
    assert rc == 0
    reparsed = parse_agent_md(md_path)
    assert reparsed.provider == "openai"
    assert reparsed.model == "gpt-5"


def test_set_system_prompt_rewrites_body(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")
    rc = agent_lifecycle.run_set(agents_dir, "scribe", {"system_prompt": "Brand new prompt body."})
    assert rc == 0
    assert parse_agent_md(md_path).system_prompt == "Brand new prompt body."


def test_set_invalid_value_errors_and_leaves_file(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")
    original = md_path.read_text(encoding="utf-8")

    rc = agent_lifecycle.run_set(agents_dir, "scribe", {"thinking_effort": "ludicrous"})
    assert rc == 1
    out = capsys.readouterr().out
    assert "error:" in out and "thinking_effort" in out
    # Validate-before-write: the on-disk file is untouched and no tmp leaked.
    assert md_path.read_text(encoding="utf-8") == original
    assert list(agents_dir.glob(".*.tmp")) == []


def test_set_unknown_agent_errors(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    rc = agent_lifecycle.run_set(agents_dir, "ghost", {"description": "x"})
    assert rc == 1
    assert "ghost" in capsys.readouterr().out


def test_set_no_updates_errors(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    assert agent_lifecycle.run_set(agents_dir, "scribe", {}) == 1
    assert "error:" in capsys.readouterr().out


def test_set_unknown_field_errors(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    assert agent_lifecycle.run_set(agents_dir, "scribe", {"bogus": "x"}) == 1
    assert "bogus" in capsys.readouterr().out


def test_set_provider_without_model_warns_but_writes(tmp_path: Path, capsys) -> None:
    """``--provider`` alone keeps the current model, which may not be valid for the
    new provider — surface that to the operator while still applying the switch."""
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")

    rc = agent_lifecycle.run_set(agents_dir, "scribe", {"provider": "openai"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning: --provider was set without --model" in out
    # The provider switch still lands despite the warning.
    assert parse_agent_md(md_path).provider == "openai"


def test_set_provider_with_model_does_not_warn(tmp_path: Path, capsys) -> None:
    """Passing both ``--provider`` and ``--model`` carries a matched pair, so the
    mismatch warning must stay silent."""
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")

    rc = agent_lifecycle.run_set(agents_dir, "scribe", {"provider": "openai", "model": "gpt-5-mini"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning: --provider was set without --model" not in out
    reparsed = parse_agent_md(md_path)
    assert reparsed.provider == "openai"
    assert reparsed.model == "gpt-5-mini"


def test_set_reports_partial_apply_before_mid_loop_failure(tmp_path: Path, capsys) -> None:
    """A later field's validation failure leaves earlier successes written, and the
    operator is told which fields already landed before the offending one."""
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")
    # Seed a valid thinking_effort so the later (invalid) write is the only
    # failure and the field's prior on-disk value is checkable.
    agent_lifecycle.run_set(agents_dir, "scribe", {"thinking_effort": "high"})
    assert parse_agent_md(md_path).thinking_effort == "high"

    # Dict order matters: description (applies), then the invalid effort (fails).
    rc = agent_lifecycle.run_set(
        agents_dir, "scribe", {"description": "New desc", "thinking_effort": "ludicrous"}
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "note: already applied description before this error." in out
    assert "error: thinking_effort:" in out

    # The earlier field's write stuck; the failing field's value is untouched.
    reparsed = parse_agent_md(md_path)
    assert reparsed.description == "New desc"
    assert reparsed.thinking_effort == "high"


# --- rename -----------------------------------------------------------------


def test_rename_moves_md(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")

    agent_lifecycle.rename_agent(agents_dir, "scribe", "penny")

    # New .md exists, parses, and carries the new name; old .md is gone.
    new_md = agents_dir / "penny.md"
    assert new_md.is_file()
    assert parse_agent_md(new_md).agent_id == "penny"
    assert not (agents_dir / "scribe.md").exists()


def test_rename_onto_existing_name_raises_and_keeps_both(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    src = _seed_agent(agents_dir, "scribe")
    dst = _seed_agent(agents_dir, "penny")
    src_before = src.read_text(encoding="utf-8")
    dst_before = dst.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        agent_lifecycle.rename_agent(agents_dir, "scribe", "penny")

    # Neither file is lost or clobbered — the source .md must survive a refused
    # rename so the agent can't vanish.
    assert src.read_text(encoding="utf-8") == src_before
    assert dst.read_text(encoding="utf-8") == dst_before


def test_rename_missing_source_raises(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="no agent"):
        agent_lifecycle.rename_agent(agents_dir, "ghost", "penny")


def test_rename_to_same_id_raises(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    # "Scribe" slugifies back to "scribe": a no-op rename is rejected.
    with pytest.raises(ValueError, match="same agent id"):
        agent_lifecycle.rename_agent(agents_dir, "scribe", "Scribe")


def test_run_rename_success(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    rc = agent_lifecycle.run_rename(agents_dir, "scribe", "penny")
    assert rc == 0
    out = capsys.readouterr().out
    assert "Renamed" in out and "penny" in out


def test_run_rename_existing_returns_1(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    _seed_agent(agents_dir, "penny")
    assert agent_lifecycle.run_rename(agents_dir, "scribe", "penny") == 1
    assert "error:" in capsys.readouterr().out
    # The source is still intact after a refused rename.
    assert (agents_dir / "scribe.md").is_file()


def test_rename_rolls_back_new_md_when_old_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If removing the OLD ``.md`` fails after the NEW one is written, the rename
    must roll the new file back so two live agents aren't left on disk."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")

    # Capture the real unlink before patching so the rollback unlink (penny.md)
    # still works; only the old-.md removal is forced to fail.
    real_unlink = Path.unlink

    def fake(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "scribe.md":
            raise OSError("cannot remove old md")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake)

    with pytest.raises(OSError, match="cannot remove old md"):
        agent_lifecycle.rename_agent(agents_dir, "scribe", "penny")

    # Original intact, new file rolled back.
    assert (agents_dir / "scribe.md").is_file()
    assert not (agents_dir / "penny.md").exists()


# --- delete -----------------------------------------------------------------


def test_delete_agent_removes_md(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")

    agent_lifecycle.delete_agent(agents_dir, "scribe")
    assert not (agents_dir / "scribe.md").exists()


def test_delete_agent_missing_raises(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="no agent"):
        agent_lifecycle.delete_agent(agents_dir, "ghost")


def test_run_delete_confirmed_removes_md(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")

    fake = FakePrompter(confirm_result=True)
    rc = agent_lifecycle.run_delete(fake, agents_dir, "scribe")
    assert rc == 0
    assert fake.confirm_calls  # the operator was asked
    assert not (agents_dir / "scribe.md").exists()


def test_run_delete_declined_keeps_file(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    md_path = _seed_agent(agents_dir, "scribe")

    fake = FakePrompter(confirm_result=False)
    rc = agent_lifecycle.run_delete(fake, agents_dir, "scribe")
    assert rc == 0
    assert "cancelled" in capsys.readouterr().out
    assert md_path.is_file()


def test_run_delete_yes_skips_prompt(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")

    fake = FakePrompter(confirm_result=False)  # would decline if asked
    rc = agent_lifecycle.run_delete(fake, agents_dir, "scribe", yes=True)
    assert rc == 0
    assert fake.confirm_calls == []  # --yes means no prompt
    assert not (agents_dir / "scribe.md").exists()


def test_run_delete_missing_returns_1(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    fake = FakePrompter(confirm_result=True)
    assert agent_lifecycle.run_delete(fake, agents_dir, "ghost") == 1
    assert "ghost" in capsys.readouterr().out
    assert fake.confirm_calls == []  # never prompted for a nonexistent agent
