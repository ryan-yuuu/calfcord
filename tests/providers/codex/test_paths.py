"""Tests for resolving the calfcord install root for codex-local paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.providers.codex._paths import calfcord_home


class TestCalfcordHome:
    def test_honors_calfcord_home_when_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "opt" / "calfcord"))
        assert calfcord_home() == tmp_path / "opt" / "calfcord"

    def test_falls_back_to_dot_calfcord_when_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("CALFCORD_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert calfcord_home() == tmp_path / ".calfcord"

    def test_empty_calfcord_home_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # A stray ``CALFCORD_HOME=`` must not root paths at ``/`` — same guard
        # the CLI / mcp / bridge resolvers use.
        monkeypatch.setenv("CALFCORD_HOME", "")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert calfcord_home() == tmp_path / ".calfcord"
