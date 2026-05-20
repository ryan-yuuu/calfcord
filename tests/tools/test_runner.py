"""Unit tests for ``calfkit-tools`` runner helpers.

Covers the pure helpers (``_resolve_timeout``, ``_resolve_tool_nodes``) and
the ``_run_worker`` shutdown contract. The full ``_amain`` requires
Discord auth, a Kafka broker, and an agents directory — too heavy for a
unit test. Operators will see boot failures of those in stderr; the
contracts worth pinning are the local validation helpers and the
supervisor-restart invariant.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.worker import Worker

from calfkit_organization.tools import private_chat, runner


class TestResolveTimeout:
    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", raising=False)
        assert runner._resolve_timeout() == private_chat.DEFAULT_TIMEOUT_SECONDS

    def test_numeric_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "15.5")
        assert runner._resolve_timeout() == 15.5

    def test_non_numeric_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A typo'd env var must fail boot rather than silently use the
        default — a 60s timeout when the operator typed something different
        would be very confusing."""
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "abc")
        with pytest.raises(SystemExit, match="must be a number"):
            runner._resolve_timeout()

    def test_zero_or_negative_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-positive timeout is a misconfiguration — without this
        guard, ``execute_node(timeout=0)`` would either always fail or
        block depending on calfkit's interpretation."""
        monkeypatch.setenv("CALFKIT_TOOLS_TIMEOUT_SECONDS", "0")
        with pytest.raises(SystemExit, match="must be positive"):
            runner._resolve_timeout()


class TestResolveCategoryName:
    """``CALFKIT_A2A_CHANNEL_CATEGORY`` reading. Opt-in, empty-as-unset."""

    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_A2A_CHANNEL_CATEGORY", raising=False)
        assert runner._resolve_category_name() is None

    def test_set_returns_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "private-a2a")
        assert runner._resolve_category_name() == "private-a2a"

    def test_empty_string_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who leaves the line blank in ``.env`` should
        get the default uncategorized behavior, not a category literally
        named ``""``."""
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "")
        assert runner._resolve_category_name() is None

    def test_whitespace_only_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "   ")
        assert runner._resolve_category_name() is None

    def test_leading_trailing_whitespace_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quoting in shell/env files commonly leaves stray whitespace;
        normalize so ``"  private-a2a "`` and ``"private-a2a"`` are
        equivalent rather than two different Discord categories."""
        monkeypatch.setenv("CALFKIT_A2A_CHANNEL_CATEGORY", "  private-a2a  ")
        assert runner._resolve_category_name() == "private-a2a"


class TestResolveToolNodes:
    def test_returns_nodes_from_populated_registry(self) -> None:
        node = MagicMock()
        result = runner._resolve_tool_nodes({"private_chat": node})
        assert result == [node]

    def test_empty_registry_fails_fast(self) -> None:
        """The empty-registry guard exists specifically to prevent the
        worker from starting in an inert state — subscribed to nothing,
        responding to nothing, but otherwise looking healthy in logs."""
        with pytest.raises(SystemExit, match="empty"):
            runner._resolve_tool_nodes({})


class TestDefaultTimeoutValue:
    def test_default_is_60_seconds(self) -> None:
        """Pin the literal — the design discussion settled on 60s. A future
        change to e.g. 600s should be a deliberate decision the test forces
        a reader to confirm, not a silent edit that passes existing tests
        because they only compared against the constant."""
        assert private_chat.DEFAULT_TIMEOUT_SECONDS == 60.0


class TestRunWorkerShutdownContract:
    """The supervisor-restart invariant: any non-signal exit from the
    worker must raise out of ``_run_worker`` so the process exits
    non-zero. Pins both the crash path and the unexpected-clean-return
    path; without one of these tests a future refactor that re-swallowed
    either case would not surface."""

    async def test_worker_crash_propagates(self) -> None:
        """An exception inside ``worker.run()`` must escape ``_run_worker``
        so the surrounding ``asyncio.run`` exits non-zero."""
        crash = ValueError("simulated kafka drop")
        worker = MagicMock(spec=Worker)
        worker.run = AsyncMock(side_effect=crash)
        with pytest.raises(ValueError, match="simulated kafka drop"):
            await runner._run_worker(worker)

    async def test_worker_unexpected_clean_return_raises(self) -> None:
        """A clean ``worker.run()`` return without a shutdown signal is
        unexpected — must synthesize a RuntimeError so supervisors
        configured for ``Restart=on-failure`` restart us."""
        worker = MagicMock(spec=Worker)

        async def returns_immediately() -> None:
            return None

        worker.run = AsyncMock(side_effect=returns_immediately)
        with pytest.raises(RuntimeError, match="returned unexpectedly"):
            await runner._run_worker(worker)

