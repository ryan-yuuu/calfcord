"""Unit tests for load_agents_dir."""

from __future__ import annotations

from pathlib import Path

import pytest

from calfkit_organization.agents.loader import load_agents_dir


def _write_agent(dir_: Path, name: str, **frontmatter_extra) -> None:
    fields = {
        "name": name,
        "slash": f"/{name}",
        "display_name": name.title(),
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
            "---\nname: draft\nslash: /draft\ndisplay_name: Draft\ndescription: Draft.\n---\nBody.\n"
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
        from calfkit_organization.tools import TOOL_REGISTRY

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
