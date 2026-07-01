"""Unit tests for load_agents_dir."""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.agents.loader import _load_one, load_agent_targets, load_agents_dir


def _write_agent(dir_: Path, name: str, **frontmatter_extra) -> None:
    fields = {
        "name": name,
        "description": f"Test agent {name}.",
    }
    fields.update(frontmatter_extra)
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(f"You are {name}.")
    (dir_ / f"{name}.md").write_text("\n".join(lines))


class TestLoadAgentsDir:
    def test_loads_all_md_files(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        _write_agent(tmp_path, "finance")
        defs = load_agents_dir(tmp_path)
        assert {d.agent_id for d in defs} == {"scheduler", "finance"}

    def test_returns_sorted_by_agent_id(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "zulu")
        _write_agent(tmp_path, "alpha")
        _write_agent(tmp_path, "mike")
        defs = load_agents_dir(tmp_path)
        assert [d.agent_id for d in defs] == ["alpha", "mike", "zulu"]

    def test_skips_non_md_files(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        (tmp_path / "README.txt").write_text("not an agent")
        (tmp_path / "config.yaml").write_text("name: not-an-agent")
        defs = load_agents_dir(tmp_path)
        assert [d.agent_id for d in defs] == ["scheduler"]

    def test_skips_hidden_md_files(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        # A draft file starting with a dot should be ignored.
        (tmp_path / ".draft.md").write_text(
            "---\nname: draft\ndescription: Draft.\n---\nBody.\n"
        )
        defs = load_agents_dir(tmp_path)
        assert [d.agent_id for d in defs] == ["scheduler"]

    def test_skips_template_md_files(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        # ``*.template.md`` reference templates document the frontmatter schema
        # and are never live agents. Their ``name`` deliberately does not match
        # the filename stem, so the loader must skip them *before* parsing —
        # otherwise the stem/name mismatch would abort the whole load.
        (tmp_path / "agent.template.md").write_text(
            "---\nname: example\ndescription: Template.\n---\nBody.\n"
        )
        defs = load_agents_dir(tmp_path)
        assert [d.agent_id for d in defs] == ["scheduler"]

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        assert load_agents_dir(tmp_path) == []

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_agents_dir(tmp_path / "missing")

    def test_path_is_file_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "file.md"
        f.write_text("not a directory")
        with pytest.raises(NotADirectoryError):
            load_agents_dir(f)

    def test_one_bad_file_aborts_load(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        (tmp_path / "broken.md").write_text("---\nname: mismatch\n---\nbody\n")
        with pytest.raises(ValueError):
            load_agents_dir(tmp_path)


class TestToolsDefaultExpansion:
    """The loader normalizes ``tools=None`` (frontmatter omitted) to every
    registered tool, so downstream consumers see a concrete tuple. Explicit
    ``tools: []`` and explicit lists are preserved unchanged."""

    def test_omitted_tools_expands_to_all_registered(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")  # no tools: line
        defs = load_agents_dir(tmp_path)
        from calfcord.tools import TOOL_REGISTRY

        assert defs[0].tools is not None
        assert set(defs[0].tools) == set(TOOL_REGISTRY)

    def test_explicit_empty_list_stays_empty(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "minimal", tools="[]")
        defs = load_agents_dir(tmp_path)
        assert defs[0].tools == ()

    def test_explicit_list_passes_through(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "researcher", tools="[read_file, grep, glob]")
        defs = load_agents_dir(tmp_path)
        assert defs[0].tools == ("read_file", "grep", "glob")


class TestLoadOne:
    """``_load_one`` is the single source of truth for turning one file into a
    live definition; both the directory scan and explicit file targeting route
    through it, so tools normalization must apply identically either way."""

    def test_omitted_tools_expands_to_all_registered(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")  # no tools: line
        from calfcord.tools import TOOL_REGISTRY

        definition = _load_one(tmp_path / "scheduler.md")
        assert definition.tools is not None
        assert set(definition.tools) == set(TOOL_REGISTRY)

    def test_parity_with_dir_loaded_version(self, tmp_path: Path) -> None:
        """A file loaded via ``_load_one`` must equal the same file loaded via
        the directory scan — identical definition regardless of selection."""
        _write_agent(tmp_path, "scheduler")
        via_file = _load_one(tmp_path / "scheduler.md")
        via_dir = load_agents_dir(tmp_path)[0]
        assert via_file == via_dir


def _write_template_named_agent(dir_: Path, stem: str) -> Path:
    """Write a ``<stem>.template.md`` whose frontmatter ``name`` matches its
    full stem (``<stem>.template``).

    Returns the path. Used to prove explicit file targeting routes through
    ``parse_agent_md`` (it is NOT silently dropped by the dir ``*.template.md``
    skip filter). ``parse_agent_md`` still validates: the dot in the stem makes
    the ``name`` fail the ``[a-z0-9_-]`` agent_id pattern, so this construction
    is used in the "explicit file is validated, not skipped" test.
    """
    path = dir_ / f"{stem}.template.md"
    expected = f"{stem}.template"  # Path(...).stem of a *.template.md file
    path.write_text(
        f"---\nname: {expected}\ndescription: Template agent.\n---\n\nYou are a template.\n"
    )
    return path


class TestLoadAgentTargets:
    def test_single_file_loaded_literally(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        defs = load_agent_targets([tmp_path / "scheduler.md"])
        assert [d.agent_id for d in defs] == ["scheduler"]

    def test_template_file_named_explicitly_bypasses_skip_filter(self, tmp_path: Path) -> None:
        """A ``*.template.md`` file is skipped by the dir scan, but naming it
        explicitly routes it through ``parse_agent_md`` (it is NOT silently
        dropped). Proof: ``parse_agent_md`` runs its validation and rejects the
        dotted stem as an invalid agent_id — a *skipped* file would instead
        yield an empty list with no error. Either way the skip filter is
        provably bypassed; here parse-time validation is what fires.

        (A ``*.template.md`` whose ``name`` would survive validation is
        impossible: the stem always carries a ``.`` from ``.template``, which
        the ``[a-z0-9_-]`` agent_id pattern forbids — so the validation-raises
        path is the canonical demonstration of the bypass.)"""
        path = _write_template_named_agent(tmp_path, "foo")
        # The dir scan skips it silently (empty list, no error).
        assert load_agents_dir(tmp_path) == []
        # The explicit file target reaches parse_agent_md, which validates and
        # rejects the dotted name — proving the skip filter was bypassed.
        with pytest.raises(ValueError, match="name must match"):
            load_agent_targets([path])

    def test_template_file_with_valid_name_loaded_when_named(self, tmp_path: Path) -> None:
        """A literal ``template.md`` (does NOT end in ``.template.md``, stem is
        the dot-free ``template``) has a valid agent_id and loads cleanly when
        named explicitly — confirming explicit files are loaded literally."""
        path = tmp_path / "template.md"
        path.write_text(
            "---\nname: template\ndescription: A valid template-named agent.\n---\n\nYou are template.\n"
        )
        defs = load_agent_targets([path])
        assert [d.agent_id for d in defs] == ["template"]

    def test_multiple_files(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        _write_agent(tmp_path, "finance")
        defs = load_agent_targets(
            [tmp_path / "scheduler.md", tmp_path / "finance.md"],
        )
        assert {d.agent_id for d in defs} == {"scheduler", "finance"}

    def test_directory_target(self, tmp_path: Path) -> None:
        d = tmp_path / "agents"
        d.mkdir()
        _write_agent(d, "scheduler")
        _write_agent(d, "finance")
        defs = load_agent_targets([d])
        assert {d.agent_id for d in defs} == {"scheduler", "finance"}

    def test_file_and_directory_mix(self, tmp_path: Path) -> None:
        d = tmp_path / "agents"
        d.mkdir()
        _write_agent(d, "scheduler")
        loose = tmp_path / "loose"
        loose.mkdir()
        _write_agent(loose, "finance")
        defs = load_agent_targets([loose / "finance.md", d])
        assert {d.agent_id for d in defs} == {"scheduler", "finance"}

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            load_agent_targets([tmp_path / "missing.md"])

    def test_duplicate_agent_id_same_file_twice_raises(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "scheduler")
        f = tmp_path / "scheduler.md"
        with pytest.raises(ValueError, match="duplicate agent_id") as exc_info:
            load_agent_targets([f, f])
        assert "scheduler" in str(exc_info.value)

    def test_duplicate_agent_id_file_plus_parent_dir_raises(self, tmp_path: Path) -> None:
        """Targeting a file plus the directory that contains it collides on
        ``agent_id`` — a hard error, not silent last-wins."""
        d = tmp_path / "agents"
        d.mkdir()
        _write_agent(d, "scheduler")
        with pytest.raises(ValueError, match="duplicate agent_id") as exc_info:
            load_agent_targets([d / "scheduler.md", d])
        msg = str(exc_info.value)
        assert "scheduler" in msg
        # Both source paths surface so the operator can disambiguate.
        assert str(d / "scheduler.md") in msg
        assert str(d) in msg

    def test_result_sorted_by_agent_id(self, tmp_path: Path) -> None:
        _write_agent(tmp_path, "zulu")
        _write_agent(tmp_path, "alpha")
        _write_agent(tmp_path, "mike")
        defs = load_agent_targets(
            [tmp_path / "zulu.md", tmp_path / "alpha.md", tmp_path / "mike.md"],
        )
        assert [d.agent_id for d in defs] == ["alpha", "mike", "zulu"]
