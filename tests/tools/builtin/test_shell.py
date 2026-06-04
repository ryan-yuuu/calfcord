"""Tests for the shell tool — mock the upstream executor so we test the
wrapper's behavior, not openhands' shell implementation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from calfkit.models import ToolContext
from openhands.tools.terminal.definition import TerminalObservation

from calfcord.tools.builtin import shell, workspace


def _ctx() -> ToolContext:
    return ToolContext(
        deps={},
        run_id="c",
        agent_name="alice",
    )


def _make_observation(text: str, exit_code: int = 0) -> TerminalObservation:
    """Build a minimal TerminalObservation that the wrapper can flatten."""
    return TerminalObservation.from_text(
        text=text, command="dummy", exit_code=exit_code,
    )


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(tmp_path))
    workspace._reset_cache_for_tests()
    shell._executor = None
    yield
    shell._executor = None


class TestShell:
    async def test_passes_command_to_executor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = MagicMock(return_value=_make_observation("hello world", exit_code=0))
        monkeypatch.setattr(shell, "_get_executor", lambda: fake)

        result = await shell.shell(_ctx(), "echo hi")

        assert fake.call_count == 1
        action = fake.call_args.args[0]
        assert action.command == "echo hi"
        assert action.timeout is None
        assert action.is_input is False
        assert action.reset is False
        assert "hello world" in result
        assert "exit code: 0" in result

    async def test_timeout_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = MagicMock(return_value=_make_observation("ok"))
        monkeypatch.setattr(shell, "_get_executor", lambda: fake)
        await shell.shell(_ctx(), "sleep 1", timeout=12.5)
        assert fake.call_args.args[0].timeout == 12.5

    async def test_nonzero_exit_code_surfaced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = MagicMock(return_value=_make_observation("oops", exit_code=42))
        monkeypatch.setattr(shell, "_get_executor", lambda: fake)
        result = await shell.shell(_ctx(), "false")
        assert "exit code: 42" in result

    async def test_no_output_yields_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = MagicMock(return_value=_make_observation("", exit_code=0))
        monkeypatch.setattr(shell, "_get_executor", lambda: fake)
        result = await shell.shell(_ctx(), "true")
        # Empty content must still produce a usable string for the LLM
        # and Discord (which rejects empty webhook posts).
        assert "(no output)" in result or "exit code: 0" in result

    async def test_executor_init_exception_returns_error_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the lazy executor init raises (e.g. tmux missing on a
        host with CALFCORD_SHELL_BACKEND=tmux), the wrapper must convert
        the exception to an ``"error: ..."`` string so the LLM can
        adapt instead of triggering the calfkit infra-bug RuntimeError
        path."""
        def _boom() -> object:
            raise RuntimeError("tmux not installed")

        monkeypatch.setattr(shell, "_get_executor", _boom)
        result = await shell.shell(_ctx(), "echo hi")
        assert result.startswith("error: "), result
        assert "tmux not installed" in result

    async def test_executor_call_exception_returns_error_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same contract for mid-call failures (session crash)."""
        fake = MagicMock(side_effect=RuntimeError("session disappeared"))
        monkeypatch.setattr(shell, "_get_executor", lambda: fake)
        result = await shell.shell(_ctx(), "echo hi")
        assert result.startswith("error: "), result
        assert "session disappeared" in result

    async def test_is_error_observation_flagged_with_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An observation with ``is_error=True`` (e.g. timeout, non-zero
        exit-as-error) must surface as an ``"error: "`` line so the LLM
        can tell it apart from a successful read of similar-looking text."""
        obs = TerminalObservation.from_text(
            text="command timed out", command="sleep 10", exit_code=-1,
            is_error=True,
        )
        fake = MagicMock(return_value=obs)
        monkeypatch.setattr(shell, "_get_executor", lambda: fake)
        result = await shell.shell(_ctx(), "sleep 10")
        assert "error: " in result
        assert "command timed out" in result


class TestResolveBackend:
    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFCORD_SHELL_BACKEND", raising=False)
        assert shell._resolve_backend() is None

    def test_valid_value_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_SHELL_BACKEND", "subprocess")
        assert shell._resolve_backend() == "subprocess"

    def test_invalid_value_falls_back_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFCORD_SHELL_BACKEND", "bash")
        # Garbage value falls back to auto-detect rather than raising —
        # the LLM caller can't fix a misconfigured operator env.
        assert shell._resolve_backend() is None
