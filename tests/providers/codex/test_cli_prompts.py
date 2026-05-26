"""Tests for the calfkit-auth codex prompt-management subcommands."""

from __future__ import annotations

import argparse
from datetime import timedelta

import pytest

from calfkit_organization.providers.codex import cli
from calfkit_organization.providers.codex.prompt_cache import PromptCache


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """Redirect the prompt cache to a tmp dir per test."""
    monkeypatch.setenv("CALFCORD_PROMPT_CACHE_DIR", str(tmp_path / "prompts"))
    # Reset the singleton resolver between tests
    from calfkit_organization.providers.codex import prompts

    monkeypatch.setattr(prompts, "_default_resolver", None)
    yield


class TestPromptStatus:
    def test_empty_cache_reports_not_cached(self, capsys):
        rc = cli._cmd_prompt_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert rc == 1
        assert "No Codex prompts cached" in out
        assert "refresh-prompts" in out

    def test_populated_cache_lists_entries(self, capsys):
        cache = PromptCache()
        cache.save("models.json", b'{"models": []}', "etag-xyz-123456789")
        cache.save("prompt.md", b"FALLBACK", "etag-abc-987654321")
        rc = cli._cmd_prompt_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert rc == 0
        assert "models.json" in out
        assert "prompt.md" in out
        assert "etag=" in out


class TestClearPrompts:
    def test_clear_removes_files(self, capsys):
        cache = PromptCache()
        cache.save("models.json", b"x", "e")
        assert len(cache.files()) == 1
        rc = cli._cmd_clear_prompts(argparse.Namespace())
        err = capsys.readouterr().err
        assert rc == 0
        assert "Cleared" in err
        assert len(cache.files()) == 0

    def test_clear_tolerates_empty_cache(self, capsys):
        # No save first; clear should not raise.
        rc = cli._cmd_clear_prompts(argparse.Namespace())
        assert rc == 0


class TestRefreshPrompts:
    @pytest.mark.asyncio
    async def test_refresh_failure_returns_nonzero(self, capsys, monkeypatch):
        """When upstream unavailable AND cache empty, refresh hard-fails."""
        from calfkit_organization.providers.codex import prompts as p

        class _FailingResolver:
            def reset(self) -> None:
                pass

            async def ensure_loaded(self) -> None:
                raise p.CodexPromptsUnavailableError("test failure")

        monkeypatch.setattr(p, "get_default_resolver", lambda **_: _FailingResolver())

        rc = await cli._cmd_refresh_prompts(argparse.Namespace())
        err = capsys.readouterr().err
        assert rc == 1
        assert "test failure" in err


class TestFormatAge:
    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (5, "5s"),
            (90, "1m"),
            (3700, "1h"),
            (86400 * 3, "3d"),
        ],
    )
    def test_buckets(self, seconds, expected):
        assert cli._format_age(timedelta(seconds=seconds)) == expected


class TestParser:
    def test_refresh_prompts_subcommand_registered(self):
        parser = cli._build_parser()
        args = parser.parse_args(["codex", "refresh-prompts"])
        assert args.command == "refresh-prompts"

    def test_prompt_status_subcommand_registered(self):
        parser = cli._build_parser()
        args = parser.parse_args(["codex", "prompt-status"])
        assert args.command == "prompt-status"

    def test_clear_prompts_subcommand_registered(self):
        parser = cli._build_parser()
        args = parser.parse_args(["codex", "clear-prompts"])
        assert args.command == "clear-prompts"
