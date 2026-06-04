"""Tests for the grep / glob wrappers — exercise the real openhands
executors against a temp workspace.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from calfkit.models import ToolContext
from openhands.tools.glob.definition import GlobObservation
from openhands.tools.grep.definition import GrepObservation

from calfcord.tools.builtin import search, workspace


def _ctx() -> ToolContext:
    return ToolContext(
        deps={},
        run_id="c",
        agent_name="alice",
    )


@pytest.fixture
def seeded_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(tmp_path))
    workspace._reset_cache_for_tests()
    search._grep_executor = None
    search._glob_executor = None
    (tmp_path / "a.py").write_text("def foo(): return 1\n")
    (tmp_path / "b.py").write_text("def foo(): return 2\n# foo bar\n")
    (tmp_path / "c.txt").write_text("no match here\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.py").write_text("foo nested\n")
    yield tmp_path
    search._grep_executor = None
    search._glob_executor = None


class TestGrep:
    async def test_finds_matches_in_all_files(self, seeded_workspace: Path) -> None:
        result = await search.grep(_ctx(), "foo")
        assert "a.py" in result and "b.py" in result
        assert "c.txt" not in result

    async def test_no_matches_returns_friendly_message(
        self, seeded_workspace: Path
    ) -> None:
        result = await search.grep(_ctx(), "definitely_not_present")
        assert "No files matched grep" in result

    async def test_is_error_observation_surfaces_as_error_prefix(
        self, monkeypatch: pytest.MonkeyPatch, seeded_workspace: Path
    ) -> None:
        """Upstream catches its own permission-denied / bad-path errors
        and returns ``is_error=True`` with empty ``matches``. Without
        the wrapper checking that flag, the LLM would see a
        ``"No files matched ..."`` indistinguishable from a real
        no-hit — silently dropping the diagnostic."""
        err_obs = GrepObservation.from_text(
            text="Permission denied: /protected",
            matches=[],
            pattern="foo",
            search_path="/protected",
            is_error=True,
        )
        fake = MagicMock(return_value=err_obs)
        monkeypatch.setattr(search, "_get_grep_executor", lambda: fake)
        result = await search.grep(_ctx(), "foo")
        assert result.startswith("error: "), result
        assert "Permission denied" in result

    async def test_include_filter_narrows_results(self, seeded_workspace: Path) -> None:
        result = await search.grep(_ctx(), "foo", include="*.txt")
        # No .txt files match "foo" in our seed data.
        assert "No files matched grep" in result

    async def test_relative_path_resolves_to_workspace_subdir(
        self, seeded_workspace: Path
    ) -> None:
        result = await search.grep(_ctx(), "foo", path="sub")
        assert "d.py" in result
        # Files outside the sub/ dir must not appear.
        assert "/a.py" not in result


class TestGlob:
    async def test_finds_files_by_extension(self, seeded_workspace: Path) -> None:
        result = await search.glob(_ctx(), "*.py")
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    async def test_recursive_glob(self, seeded_workspace: Path) -> None:
        result = await search.glob(_ctx(), "**/*.py")
        assert "d.py" in result

    async def test_no_match_returns_friendly_message(
        self, seeded_workspace: Path
    ) -> None:
        result = await search.glob(_ctx(), "*.rs")
        assert "No files matched glob" in result

    async def test_is_error_observation_surfaces_as_error_prefix(
        self, monkeypatch: pytest.MonkeyPatch, seeded_workspace: Path
    ) -> None:
        err_obs = GlobObservation.from_text(
            text="Path is not a directory: /etc/hosts",
            files=[],
            pattern="*.py",
            search_path="/etc/hosts",
            is_error=True,
        )
        fake = MagicMock(return_value=err_obs)
        monkeypatch.setattr(search, "_get_glob_executor", lambda: fake)
        result = await search.glob(_ctx(), "*.py")
        assert result.startswith("error: "), result
        assert "not a directory" in result
