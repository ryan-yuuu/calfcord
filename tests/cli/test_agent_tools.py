"""Tests for the ``calfcord agent tools`` interactive editor.

The flow is pure logic over an injected :class:`Prompter`, so these tests never
touch a TTY or InquirerPy. A :class:`FakePrompter` records the checkbox
``choices`` it was handed (to assert pre-selection) and returns a scripted
multi-select result (to assert the write). We seed real ``.md`` files and
reload them with ``frontmatter`` / ``parse_agent_md`` to verify the on-disk
effect.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import agent_tools
from calfcord.cli._prompts import Choice, Prompter
from calfcord.tools import TOOL_REGISTRY

BUILTIN_NAMES = set(TOOL_REGISTRY)


class FakePrompter:
    """A :class:`Prompter` fake that scripts ``select``/``checkbox`` answers.

    ``checkbox`` records the exact ``choices`` it received in
    :attr:`last_checkbox_choices` so tests can assert pre-selection without a
    TTY, then returns the scripted ``checkbox_result``. ``select`` returns the
    scripted ``select_result`` (used only when ``name`` is omitted). Hitting an
    unscripted prompt raises rather than hangs.
    """

    def __init__(
        self,
        *,
        select_result: str | None = None,
        checkbox_result: list[str] | None = None,
    ) -> None:
        self._select_result = select_result
        self._checkbox_result = checkbox_result if checkbox_result is not None else []
        self.last_checkbox_choices: list[Choice] | None = None
        self.last_select_choices: list[Choice] | None = None

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        if self._select_result is None:
            raise AssertionError(f"unexpected select(): {message!r}")
        self.last_select_choices = choices
        return self._select_result

    def text(self, message: str, *, default: str = "") -> str:
        raise AssertionError(f"unexpected text(): {message!r}")

    def secret(self, message: str) -> str:
        raise AssertionError(f"unexpected secret(): {message!r}")

    def confirm(self, message: str, *, default: bool = False) -> bool:
        raise AssertionError(f"unexpected confirm(): {message!r}")

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        self.last_checkbox_choices = choices
        return list(self._checkbox_result)


def test_fake_prompter_satisfies_protocol() -> None:
    """The fake must stay structurally compatible with the (checkbox-bearing) seam."""
    assert isinstance(FakePrompter(), Prompter)


def _seed_agent(agents_dir: Path, name: str, *, tools_line: str | None) -> Path:
    """Write an ``agents/<name>.md`` whose ``tools:`` frontmatter is controlled.

    ``tools_line`` is the literal YAML value for ``tools`` (e.g. ``"[]"`` or
    ``"[read_file]"``) or ``None`` to omit the key entirely — the omitted /
    empty / explicit distinction these tests turn on.
    """
    agents_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"display_name: {name.capitalize()}",
        f"description: Test {name}.",
    ]
    if tools_line is not None:
        lines.append(f"tools: {tools_line}")
    lines += ["---", "", "You are a helpful agent.", ""]
    md_path = agents_dir / f"{name}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def _checked(choices: list[Choice]) -> set[str]:
    """Return the set of pre-checked choice VALUES from a captured choices list."""
    return {c.value for c in choices if c.checked}


def _values(choices: list[Choice]) -> set[str]:
    """Return every choice VALUE from a captured choices list (checked or not)."""
    return {c.value for c in choices}


# ---------------------------------------------------------------- pre-selection ---


def test_omitted_tools_prechecks_all_builtins(tmp_path: Path) -> None:
    _seed_agent(tmp_path, "assistant", tools_line=None)
    fake = FakePrompter(checkbox_result=[])
    agent_tools.run(fake, agents_dir=tmp_path, name="assistant")

    assert fake.last_checkbox_choices is not None
    # ``tools:`` omitted ⇒ every builtin pre-checked (and only builtins —
    # there are no MCP schemas in this repo, so no selectors appear anyway).
    assert _checked(fake.last_checkbox_choices) == BUILTIN_NAMES


def test_empty_tools_prechecks_none(tmp_path: Path) -> None:
    _seed_agent(tmp_path, "assistant", tools_line="[]")
    fake = FakePrompter(checkbox_result=[])
    agent_tools.run(fake, agents_dir=tmp_path, name="assistant")

    assert fake.last_checkbox_choices is not None
    assert _checked(fake.last_checkbox_choices) == set()


def test_explicit_tools_prechecks_exactly_those(tmp_path: Path) -> None:
    _seed_agent(tmp_path, "assistant", tools_line="[read_file]")
    fake = FakePrompter(checkbox_result=[])
    agent_tools.run(fake, agents_dir=tmp_path, name="assistant")

    assert fake.last_checkbox_choices is not None
    assert _checked(fake.last_checkbox_choices) == {"read_file"}


# ---------------------------------------------------------------------- writing ---


def test_selecting_subset_writes_that_subset(tmp_path: Path) -> None:
    md_path = _seed_agent(tmp_path, "assistant", tools_line=None)
    fake = FakePrompter(checkbox_result=["read_file", "shell"])
    rc = agent_tools.run(fake, agents_dir=tmp_path, name="assistant")
    assert rc == 0

    # On-disk: an explicit list of exactly the selected tools, reloadable.
    assert frontmatter.load(md_path).metadata["tools"] == ["read_file", "shell"]
    assert parse_agent_md(md_path).tools == ("read_file", "shell")


def test_deselecting_all_writes_empty_list(tmp_path: Path) -> None:
    md_path = _seed_agent(tmp_path, "assistant", tools_line="[read_file]")
    fake = FakePrompter(checkbox_result=[])
    assert agent_tools.run(fake, agents_dir=tmp_path, name="assistant") == 0
    assert frontmatter.load(md_path).metadata["tools"] == []


# --------------------------------------------------------------- agent selection ---


def test_name_omitted_picks_via_select(tmp_path: Path) -> None:
    _seed_agent(tmp_path, "alpha", tools_line="[]")
    md_beta = _seed_agent(tmp_path, "beta", tools_line="[]")
    fake = FakePrompter(select_result="beta", checkbox_result=["shell"])

    rc = agent_tools.run(fake, agents_dir=tmp_path, name=None)
    assert rc == 0
    # The picker offered both detected agents, sorted...
    assert fake.last_select_choices == [Choice("alpha", "alpha"), Choice("beta", "beta")]
    # ...and the chosen agent's file got the write.
    assert frontmatter.load(md_beta).metadata["tools"] == ["shell"]


def test_no_agents_returns_1(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "agents"
    empty.mkdir()
    fake = FakePrompter()
    assert agent_tools.run(fake, agents_dir=empty, name=None) == 1
    assert "no agents" in capsys.readouterr().out


def test_unknown_named_agent_returns_1(tmp_path: Path, capsys) -> None:
    _seed_agent(tmp_path, "assistant", tools_line="[]")
    fake = FakePrompter()
    assert agent_tools.run(fake, agents_dir=tmp_path, name="ghost") == 1
    assert "ghost" in capsys.readouterr().out


# --------------------------------------------------- preserving unknown tokens ---


def test_configured_mcp_selectors_kept_when_catalog_empty(tmp_path: Path) -> None:
    """An agent's existing ``mcp/...`` selectors must survive an empty catalog.

    This repo ships no MCP schemas, so ``discover_mcp_catalog`` returns ``{}``
    and the selectors aren't enumerable. They must still appear as PRE-CHECKED
    "kept" rows so confirming the checkbox never silently drops them.
    """
    _seed_agent(tmp_path, "assistant", tools_line="[read_file, mcp/gmail, mcp/gmail/search]")
    fake = FakePrompter(checkbox_result=[])
    agent_tools.run(fake, agents_dir=tmp_path, name="assistant")

    assert fake.last_checkbox_choices is not None
    values = _values(fake.last_checkbox_choices)
    checked = _checked(fake.last_checkbox_choices)
    # The unenumerable MCP selectors are present AND pre-checked.
    assert {"mcp/gmail", "mcp/gmail/search"} <= values
    assert {"mcp/gmail", "mcp/gmail/search"} <= checked
    # The builtin is enumerated and pre-checked too.
    assert "read_file" in checked


def test_keeping_prechecked_kept_tokens_writes_all_tokens(tmp_path: Path) -> None:
    """Confirming with the kept MCP tokens still checked preserves every token."""
    md_path = _seed_agent(tmp_path, "assistant", tools_line="[read_file, mcp/gmail, mcp/gmail/search]")
    # The operator confirms without unchecking anything: returns the full set.
    fake = FakePrompter(checkbox_result=["read_file", "mcp/gmail", "mcp/gmail/search"])
    assert agent_tools.run(fake, agents_dir=tmp_path, name="assistant") == 0

    # Nothing dropped: all three tokens still on disk after the round-trip.
    assert parse_agent_md(md_path).tools == ("read_file", "mcp/gmail", "mcp/gmail/search")


def test_unchecking_kept_mcp_token_removes_it(tmp_path: Path) -> None:
    """Unchecking a kept MCP token drops exactly that token and keeps the rest."""
    md_path = _seed_agent(tmp_path, "assistant", tools_line="[read_file, mcp/gmail, mcp/gmail/search]")
    # Operator unticks ``mcp/gmail`` only.
    fake = FakePrompter(checkbox_result=["read_file", "mcp/gmail/search"])
    assert agent_tools.run(fake, agents_dir=tmp_path, name="assistant") == 0

    assert parse_agent_md(md_path).tools == ("read_file", "mcp/gmail/search")


# ------------------------------------------------------------- error handling ---


def test_malformed_md_returns_1_without_traceback(tmp_path: Path, capsys) -> None:
    """A malformed ``.md`` (invalid YAML frontmatter) reports an error, not a crash."""
    agents_dir = tmp_path
    agents_dir.mkdir(parents=True, exist_ok=True)
    # Unbalanced bracket in the YAML value makes parse_agent_md raise ValueError.
    (agents_dir / "broken.md").write_text(
        "---\nname: broken\ntools: [unclosed\n---\nbody\n", encoding="utf-8"
    )
    fake = FakePrompter()
    assert agent_tools.run(fake, agents_dir=agents_dir, name="broken") == 1
    out = capsys.readouterr().out
    assert "error:" in out
    assert "broken" in out


# -------------------------------------------------------------- first_line ---


def test_first_line_strips_summary_and_backticks() -> None:
    assert agent_tools.first_line("<summary>foo</summary>") == "foo"
    assert agent_tools.first_line("``x``") == "x"
    assert agent_tools.first_line("") == ""
    assert agent_tools.first_line(None) == ""
    # The first NON-EMPTY line wins, with leading blank lines skipped.
    assert agent_tools.first_line("\n\n  <summary>second</summary>\nthird") == "second"
