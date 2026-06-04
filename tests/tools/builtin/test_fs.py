"""Tests for the read_file / write_file / edit_file wrappers.

These exercise the real :class:`FileEditorExecutor` against a temp
workspace — no mocks. The wrappers are thin enough that mocking would
mostly be testing the mock, not the wrapper.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from calfkit.models import ToolContext

from calfcord.tools.builtin import fs, workspace


def _ctx() -> ToolContext:
    return ToolContext(
        deps={},
        run_id="c",
        agent_name="alice",
    )


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point CALFCORD_WORKSPACE_DIR at tmp_path and reset all cached state."""
    monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(tmp_path))
    workspace._reset_cache_for_tests()
    fs._executor = None
    yield tmp_path
    fs._executor = None


class TestWriteFile:
    async def test_creates_new_file(self, _isolated_workspace: Path) -> None:
        result = await fs.write_file(_ctx(), "hello.txt", "hi there")
        assert (_isolated_workspace / "hello.txt").read_text() == "hi there"
        assert "Wrote" in result and "hello.txt" in result

    async def test_overwrites_existing_file(self, _isolated_workspace: Path) -> None:
        p = _isolated_workspace / "x.txt"
        p.write_text("old")
        await fs.write_file(_ctx(), "x.txt", "new")
        assert p.read_text() == "new"

    async def test_creates_parent_directories(self, _isolated_workspace: Path) -> None:
        await fs.write_file(_ctx(), "a/b/c.txt", "deep")
        assert (_isolated_workspace / "a" / "b" / "c.txt").read_text() == "deep"

    async def test_absolute_path_passes_through(
        self, _isolated_workspace: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.txt"
        await fs.write_file(_ctx(), str(outside), "abs")
        assert outside.read_text() == "abs"


class TestReadFile:
    async def test_returns_content_with_line_numbers(self, _isolated_workspace: Path) -> None:
        (_isolated_workspace / "f.py").write_text("a\nb\nc\n")
        result = await fs.read_file(_ctx(), "f.py")
        # Upstream renders cat -n style with right-aligned line numbers.
        assert "1\ta" in result
        assert "2\tb" in result
        assert "3\tc" in result

    async def test_view_range_subset(self, _isolated_workspace: Path) -> None:
        (_isolated_workspace / "f.py").write_text("a\nb\nc\nd\ne\n")
        result = await fs.read_file(_ctx(), "f.py", view_range=[2, 3])
        assert "2\tb" in result
        assert "3\tc" in result
        # First and last lines must be absent when range is restricted.
        assert "1\ta" not in result
        assert "5\te" not in result

    async def test_missing_file_returns_error_text(self, _isolated_workspace: Path) -> None:
        result = await fs.read_file(_ctx(), "nope.txt")
        # The upstream tool returns an error observation. The wrapper
        # must surface that as an "error: " prefix so the LLM can
        # distinguish failure from a successful empty read — without
        # this, the result is indistinguishable from "the file
        # contained no text".
        assert result.startswith("error: "), result
        assert "nope.txt" in result


class TestEditFile:
    async def test_unique_match_succeeds(self, _isolated_workspace: Path) -> None:
        (_isolated_workspace / "f.txt").write_text("hello world\n")
        result = await fs.edit_file(_ctx(), "f.txt", "world", "calfcord")
        assert (_isolated_workspace / "f.txt").read_text() == "hello calfcord\n"
        assert "edited" in result.lower() or "has been" in result.lower()

    async def test_multi_match_without_replace_all_errors(
        self, _isolated_workspace: Path
    ) -> None:
        (_isolated_workspace / "f.txt").write_text("foo\nfoo\nfoo\n")
        result = await fs.edit_file(_ctx(), "f.txt", "foo", "bar")
        # File must NOT have been edited.
        assert (_isolated_workspace / "f.txt").read_text() == "foo\nfoo\nfoo\n"
        assert "multiple occurrences" in result.lower()

    async def test_replace_all_replaces_every_occurrence(
        self, _isolated_workspace: Path
    ) -> None:
        (_isolated_workspace / "f.txt").write_text("foo\nfoo\nbar\nfoo\n")
        result = await fs.edit_file(
            _ctx(), "f.txt", "foo", "baz", replace_all=True,
        )
        assert (_isolated_workspace / "f.txt").read_text() == "baz\nbaz\nbar\nbaz\n"
        assert "3 occurrence" in result

    async def test_replace_all_with_no_match_returns_error(
        self, _isolated_workspace: Path
    ) -> None:
        (_isolated_workspace / "f.txt").write_text("hello\n")
        result = await fs.edit_file(
            _ctx(), "f.txt", "missing", "x", replace_all=True,
        )
        assert (_isolated_workspace / "f.txt").read_text() == "hello\n"
        assert "did not appear" in result

    async def test_replace_all_missing_file(
        self, _isolated_workspace: Path
    ) -> None:
        result = await fs.edit_file(
            _ctx(), "noexist.txt", "x", "y", replace_all=True,
        )
        assert "not found" in result

    async def test_replace_all_rejects_directory(
        self, _isolated_workspace: Path
    ) -> None:
        (_isolated_workspace / "subdir").mkdir()
        result = await fs.edit_file(
            _ctx(), "subdir", "x", "y", replace_all=True,
        )
        assert "directories" in result.lower() or "not support" in result.lower()
