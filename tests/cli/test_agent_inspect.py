"""Tests for ``calfcord agent list`` / ``show`` (read-only inspection).

Both commands are pure functions of an ``agents_dir`` (plus a name) with no
prompter, so these tests seed real ``.md`` files and assert on captured stdout.
They pin the contract that matters for inspection: ``list`` reports exactly the
*live* agents (dotfiles and ``*.template.md`` templates skipped, matching the
loader) and ``--json`` is valid, round-trippable JSON; ``show`` renders every
editable field for a real agent and errors cleanly (exit 1, no traceback) for a
missing one; an empty dir is a friendly no-op, not a crash.
"""

from __future__ import annotations

import json
from pathlib import Path

from calfcord.cli import agent_inspect


def _seed_agent(
    agents_dir: Path,
    name: str,
    *,
    description: str = "A test agent.",
    provider: str | None = "anthropic",
    model: str | None = "claude-sonnet-4-5",
    tools_line: str | None = "[read_file, shell]",
    extra: list[str] | None = None,
) -> Path:
    """Write a minimal valid ``agents/<name>.md`` and return its path."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"display_name: {name.capitalize()}",
        f"description: {description}",
    ]
    if provider is not None:
        lines.append(f"provider: {provider}")
    if model is not None:
        lines.append(f"model: {model}")
    if tools_line is not None:
        lines.append(f"tools: {tools_line}")
    lines += extra or []
    lines += ["---", "", f"You are {name}, a helpful agent.", ""]
    md_path = agents_dir / f"{name}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def _seed_two_plus_noise(tmp_path: Path) -> Path:
    """Seed two live agents plus a template and a dotfile that must be skipped."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe", description="Takes notes.")
    _seed_agent(agents_dir, "penny", description="Pairs on code.", tools_line="[]")
    # A reference template and a hidden file: neither is a live agent.
    (agents_dir / "agent.template.md").write_text(
        "---\nname: agent\ndisplay_name: Tmpl\ndescription: Template.\n---\nbody\n",
        encoding="utf-8",
    )
    (agents_dir / ".hidden.md").write_text(
        "---\nname: hidden\ndisplay_name: Hidden\ndescription: Nope.\n---\nbody\n",
        encoding="utf-8",
    )
    return agents_dir


# --- list -------------------------------------------------------------------


def test_list_human_names_live_agents_only(tmp_path: Path, capsys) -> None:
    agents_dir = _seed_two_plus_noise(tmp_path)
    rc = agent_inspect.run_list(agents_dir)
    assert rc == 0

    out = capsys.readouterr().out
    assert "scribe" in out
    assert "penny" in out
    # The template stem and the dotfile stem must not leak into the listing.
    assert "agent.template" not in out
    assert "hidden" not in out
    # Header is present and the explicit-empty-tools agent shows "0".
    assert "NAME" in out and "TOOLS" in out


def test_list_human_tools_summary(tmp_path: Path) -> None:
    """Omitted tools -> 'all'; explicit [] -> '0'; explicit list -> the count.

    Asserts on the helper directly rather than parsing the aligned table, so the
    summary contract is pinned without coupling to column widths.
    """
    from calfcord.cli.agent_inspect import _tools_summary

    assert _tools_summary(None) == "all"
    assert _tools_summary(()) == "0"
    assert _tools_summary(("read_file", "shell")) == "2"


def test_list_json_is_valid_and_contains_both(tmp_path: Path, capsys) -> None:
    agents_dir = _seed_two_plus_noise(tmp_path)
    rc = agent_inspect.run_list(agents_dir, as_json=True)
    assert rc == 0

    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    names = {row["name"] for row in data}
    assert names == {"penny", "scribe"}
    # The empty-tools agent serializes as an explicit list, the other too.
    by_name = {row["name"]: row for row in data}
    assert by_name["penny"]["tools"] == []
    assert by_name["scribe"]["tools"] == ["read_file", "shell"]
    assert by_name["scribe"]["provider"] == "anthropic"


def test_list_empty_dir_human_friendly_line(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "agents"
    empty.mkdir()
    assert agent_inspect.run_list(empty) == 0
    assert "no agents" in capsys.readouterr().out


def test_list_empty_dir_json_is_empty_array(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "agents"
    empty.mkdir()
    assert agent_inspect.run_list(empty, as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_list_skips_unparseable_but_lists_the_rest(tmp_path: Path, capsys) -> None:
    """One malformed ``.md`` is noted, not fatal: the good agents still list."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    # name != stem makes parse_agent_md raise; the listing must survive it.
    (agents_dir / "broken.md").write_text(
        "---\nname: mismatch\ndisplay_name: B\ndescription: Bad.\n---\nbody\n",
        encoding="utf-8",
    )
    assert agent_inspect.run_list(agents_dir) == 0
    out = capsys.readouterr().out
    assert "scribe" in out
    assert "skipped" in out and "broken" in out


# --- show -------------------------------------------------------------------


def test_show_human_prints_fields(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(
        agents_dir,
        "scribe",
        description="Takes notes.",
        extra=["thinking_effort: high", "history_turns: 12"],
    )
    rc = agent_inspect.run_show(agents_dir, "scribe")
    assert rc == 0

    out = capsys.readouterr().out
    assert "scribe" in out
    # Labels from the FIELDS registry appear, with their rendered values.
    assert "Description" in out and "Takes notes." in out
    assert "Provider / model" in out and "anthropic" in out
    assert "Thinking effort" in out and "high" in out
    assert "History turns" in out and "12" in out
    # The file path and a system-prompt preview are shown.
    assert str(agents_dir / "scribe.md") in out
    assert "System prompt" in out


def test_show_json_round_trips(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(
        agents_dir,
        "scribe",
        description="Takes notes.",
        tools_line="[read_file, shell]",
        extra=["thinking_effort: high", "history_turns: 12"],
    )
    rc = agent_inspect.run_show(agents_dir, "scribe", as_json=True)
    assert rc == 0

    obj = json.loads(capsys.readouterr().out)
    assert obj["name"] == "scribe"
    assert obj["description"] == "Takes notes."
    assert obj["provider"] == "anthropic"
    assert obj["model"] == "claude-sonnet-4-5"
    assert obj["tools"] == ["read_file", "shell"]
    assert obj["thinking_effort"] == "high"
    assert obj["history_turns"] == 12
    assert obj["memory"] is False
    # The full body is present (not a truncated preview).
    assert "You are scribe" in obj["system_prompt"]


def test_show_json_tools_omitted_is_null(tmp_path: Path, capsys) -> None:
    """An omitted ``tools:`` serializes as JSON null, preserving the all-builtins
    sentinel rather than collapsing it to an empty list."""
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe", tools_line=None)
    agent_inspect.run_show(agents_dir, "scribe", as_json=True)
    assert json.loads(capsys.readouterr().out)["tools"] is None


def test_show_missing_agent_errors(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    _seed_agent(agents_dir, "scribe")
    rc = agent_inspect.run_show(agents_dir, "ghost")
    assert rc == 1
    out = capsys.readouterr().out
    assert "error:" in out
    assert "ghost" in out


def test_show_unparseable_agent_errors(tmp_path: Path, capsys) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "broken.md").write_text(
        "---\nname: broken\ntools: [unclosed\n---\nbody\n", encoding="utf-8"
    )
    assert agent_inspect.run_show(agents_dir, "broken") == 1
    assert "error:" in capsys.readouterr().out
