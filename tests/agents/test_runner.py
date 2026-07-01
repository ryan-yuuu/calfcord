"""Unit tests for the agents runner: .md resolution, node build, codex prewarm.

Name-addressing removed the per-agent channel state + bootstrap machinery, so
the runner is now pure ``.md`` loading + per-node build + the shared worker
shutdown contract. Resolution failures exit cleanly via :class:`SystemExit`
(no traceback) rather than a bespoke ``BootstrapError``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.worker import Worker

from calfcord.agents.definition import AgentDefinition
from calfcord.agents.runner import (
    _build_node_or_exit,
    _parse_args,
    _prewarm_codex_if_needed,
    _resolve_definitions,
    _run_worker,
)


def _write_agent_md(dir_: Path, name: str) -> None:
    """Write a minimal valid agents/<name>.md fixture in ``dir_``."""
    body = f"---\nname: {name}\ndescription: Test agent {name}.\n---\n\nYou are {name}.\n"
    (dir_ / f"{name}.md").write_text(body)


class TestParseArgs:
    def test_no_positional_returns_none(self) -> None:
        """All-agents mode: omitting the positional arg yields agent=None."""
        args = _parse_args([])
        assert args.agent is None

    def test_named_agent_returns_name(self) -> None:
        """Single-agent mode: the positional arg lands as-is."""
        args = _parse_args(["echo"])
        assert args.agent == "echo"

    def test_single_target_short_flag(self) -> None:
        """``-t`` accumulates into ``targets`` and leaves the positional None."""
        args = _parse_args(["-t", "a.md"])
        assert args.targets == ["a.md"]
        assert args.agent is None

    def test_target_long_flag(self) -> None:
        args = _parse_args(["--target", "a.md"])
        assert args.targets == ["a.md"]
        assert args.agent is None

    def test_repeated_target_accumulates(self) -> None:
        """``action="append"`` collects every ``-t``/``--target`` in order."""
        args = _parse_args(["-t", "a.md", "--target", "dir", "-t", "b.md"])
        assert args.targets == ["a.md", "dir", "b.md"]
        assert args.agent is None

    def test_no_target_defaults_to_none(self) -> None:
        """Without ``-t``, ``targets`` is None (not an empty list) so the
        precedence check in _resolve_definitions falls through cleanly."""
        args = _parse_args([])
        assert args.targets is None

    def test_target_and_positional_are_mutually_exclusive(self) -> None:
        """Passing both a positional name and ``-t`` is a usage error.
        ``parser.error`` raises SystemExit(2)."""
        with pytest.raises(SystemExit):
            _parse_args(["foo", "-t", "a.md"])


class TestResolveDefinitions:
    """``_resolve_definitions`` is the unified entry point for all runner modes.

    Targets > single-agent > directory scan. Every load failure exits cleanly
    via :class:`SystemExit` so ``main()`` shows a message, not a traceback.
    """

    @pytest.fixture
    def agents_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "agents"
        d.mkdir()
        return d

    def test_all_mode_returns_definitions_sorted_by_agent_id(self, agents_dir: Path) -> None:
        _write_agent_md(agents_dir, "scribe")
        _write_agent_md(agents_dir, "echo")
        # load_agents_dir sorts by stem.
        definitions = _resolve_definitions(None, agents_dir)
        assert [d.agent_id for d in definitions] == ["echo", "scribe"]

    def test_all_mode_empty_agents_dir_exits(self, agents_dir: Path) -> None:
        """An empty agents directory is a misconfiguration, not a no-op."""
        with pytest.raises(SystemExit, match="no agent definitions found"):
            _resolve_definitions(None, agents_dir)

    def test_all_mode_missing_agents_dir_exits(self, tmp_path: Path) -> None:
        """A nonexistent agents directory surfaces as a clean SystemExit
        (not a raw FileNotFoundError traceback)."""
        with pytest.raises(SystemExit, match="failed to load"):
            _resolve_definitions(None, tmp_path / "nonexistent")

    def test_all_mode_malformed_md_exits(self, agents_dir: Path) -> None:
        """A bad .md file makes load_agents_dir raise ValueError; the runner
        converts it to SystemExit for a clean exit."""
        _write_agent_md(agents_dir, "echo")
        # Frontmatter name mismatches the filename stem → parse_agent_md raises.
        (agents_dir / "broken.md").write_text("---\nname: mismatch\n---\nbody\n")
        with pytest.raises(SystemExit, match="failed to load"):
            _resolve_definitions(None, agents_dir)

    def test_single_mode_returns_one_definition(self, agents_dir: Path) -> None:
        _write_agent_md(agents_dir, "echo")
        _write_agent_md(agents_dir, "scribe")
        definitions = _resolve_definitions("echo", agents_dir)
        assert [d.agent_id for d in definitions] == ["echo"]

    def test_single_mode_unknown_agent_lists_known(self, agents_dir: Path) -> None:
        """Unknown single-agent name exits cleanly, naming the known agents."""
        _write_agent_md(agents_dir, "echo")
        with pytest.raises(SystemExit, match=r"not found.*Known: echo"):
            _resolve_definitions("ghost", agents_dir)

    def test_targets_file_yields_one_definition(self, agents_dir: Path) -> None:
        """A single file target resolves to exactly that agent."""
        _write_agent_md(agents_dir, "echo")
        _write_agent_md(agents_dir, "scribe")
        definitions = _resolve_definitions(None, agents_dir, targets=[agents_dir / "echo.md"])
        assert [d.agent_id for d in definitions] == ["echo"]

    def test_targets_directory_matches_all_mode(self, agents_dir: Path) -> None:
        """A directory target behaves like all-mode: every agent, sorted."""
        _write_agent_md(agents_dir, "scribe")
        _write_agent_md(agents_dir, "echo")
        definitions = _resolve_definitions(None, agents_dir, targets=[agents_dir])
        assert [d.agent_id for d in definitions] == ["echo", "scribe"]

    def test_targets_empty_result_exits(self, agents_dir: Path, tmp_path: Path) -> None:
        """A target directory with no .md files yields nothing — a clean exit."""
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SystemExit, match="no agent definitions found in --target paths"):
            _resolve_definitions(None, agents_dir, targets=[empty])

    def test_targets_missing_path_exits(self, agents_dir: Path) -> None:
        """A nonexistent target surfaces as a clean SystemExit."""
        with pytest.raises(SystemExit, match="failed to load --target paths"):
            _resolve_definitions(None, agents_dir, targets=[agents_dir / "ghost.md"])

    def test_targets_duplicate_agent_id_exits(self, agents_dir: Path) -> None:
        """Targeting the same agent via a file and its parent dir collides on
        agent_id; load_agent_targets raises ValueError, wrapped as a clean exit
        naming the offending agent_id."""
        _write_agent_md(agents_dir, "echo")
        with pytest.raises(SystemExit) as exc_info:
            _resolve_definitions(None, agents_dir, targets=[agents_dir / "echo.md", agents_dir])
        msg = str(exc_info.value)
        assert "duplicate agent_id" in msg
        assert "echo" in msg

    def test_targets_take_precedence_over_positional_name(self, agents_dir: Path) -> None:
        """Defense-in-depth: even if both arrive (argparse normally blocks
        this), targets win the precedence ladder over a positional name."""
        _write_agent_md(agents_dir, "echo")
        _write_agent_md(agents_dir, "scribe")
        definitions = _resolve_definitions("scribe", agents_dir, targets=[agents_dir / "echo.md"])
        assert [d.agent_id for d in definitions] == ["echo"]


class TestBuildNodeOrExit:
    """The factory.build_node wrap converts every failure mode into a clean
    SystemExit so the CLI exits without a traceback (worker-assembly unit)."""

    @staticmethod
    def _definition() -> AgentDefinition:
        return AgentDefinition(
            agent_id="echo",
            description="Test.",
            system_prompt="Test echo.",
        )

    def test_value_error_exits(self) -> None:
        factory = MagicMock()
        factory.build_node.side_effect = ValueError("unknown tool")
        with pytest.raises(SystemExit, match="echo.*failed to construct.*unknown tool"):  # noqa: RUF043
            _build_node_or_exit(factory, self._definition())

    def test_runtime_error_exits(self) -> None:
        """pydantic_ai raises UserError(RuntimeError) on missing API keys —
        not ValueError or KeyError. The wrap must catch this too."""
        factory = MagicMock()
        factory.build_node.side_effect = RuntimeError("ANTHROPIC_API_KEY is not set")
        with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
            _build_node_or_exit(factory, self._definition())

    def test_happy_path_returns_node(self) -> None:
        factory = MagicMock()
        sentinel = object()
        factory.build_node.return_value = sentinel
        assert _build_node_or_exit(factory, self._definition()) is sentinel


class TestPrewarmCodexIfNeeded:
    """``_prewarm_codex_if_needed`` is the runner's bridge between agent
    definitions and the openai-codex prompt resolver. Must invoke prewarm
    exactly when at least one resolved agent ends up on the openai-codex
    provider — including via the ``CALFKIT_AGENT_DEFAULT_PROVIDER`` env-var
    path, which a naive ``definition.provider == "openai-codex"`` check would
    miss for agents that omit the frontmatter field entirely.
    """

    @staticmethod
    def _definition(provider: str | None) -> AgentDefinition:
        return AgentDefinition(
            agent_id="test-agent",
            description="Test agent.",
            provider=provider,  # type: ignore[arg-type]
            system_prompt="You are a test agent.",
        )

    async def test_skips_prewarm_when_no_codex_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No agent declares openai-codex and no env-var override — prewarm
        must NOT be invoked (avoids the authlib/openhands-sdk import cost for
        non-codex deployments)."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        called = False

        async def _fake_prewarm() -> None:
            nonlocal called
            called = True

        import calfcord.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", _fake_prewarm)
        await _prewarm_codex_if_needed([self._definition("anthropic"), self._definition("openai")])
        assert called is False

    async def test_invokes_prewarm_when_any_agent_declares_openai_codex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        prewarm = AsyncMock()
        import calfcord.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", prewarm)
        await _prewarm_codex_if_needed([self._definition("anthropic"), self._definition("openai-codex")])
        prewarm.assert_awaited_once()

    async def test_invokes_prewarm_when_env_var_default_is_openai_codex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: an agent that omits ``provider:`` from its
        frontmatter must still trigger prewarm when the operator has set
        ``CALFKIT_AGENT_DEFAULT_PROVIDER=openai-codex``. The naive
        ``definition.provider == "openai-codex"`` check would see ``None`` and
        silently skip, leaving the factory to crash mid-construction."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai-codex")
        prewarm = AsyncMock()
        import calfcord.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", prewarm)
        # The definition omits provider; only the env var selects codex.
        await _prewarm_codex_if_needed([self._definition(None)])
        prewarm.assert_awaited_once()

    async def test_converts_codex_prompts_unavailable_to_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When upstream prompts can't be fetched and no cache exists, the
        runner must wrap the typed exception in a clean SystemExit with a hint
        pointing the operator at the CLI — not crash with an unactionable
        traceback."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        import calfcord.providers.codex as codex_pkg
        from calfcord.providers.codex import CodexPromptsUnavailableError

        async def _failing_prewarm() -> None:
            raise CodexPromptsUnavailableError("simulated network failure")

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", _failing_prewarm)
        with pytest.raises(SystemExit, match="refresh-prompts"):
            await _prewarm_codex_if_needed([self._definition("openai-codex")])


class TestRunWorkerShutdownContract:
    """The agents runner delegates its shutdown contract to the shared
    :func:`run_worker_until_signal`, which drives the worker via the embedded
    ``Worker.start()``/``stop()`` surface (not ``run()``) so it keeps SIGINT/
    SIGTERM ownership. The full contract is exercised in
    ``tests/test_worker_runtime.py``. Here we only pin, per runner, that a
    managed-boot crash propagates out of ``_run_worker`` (so this passes only
    if the runner really wires into the shared helper).
    """

    async def test_worker_boot_crash_propagates(self) -> None:
        """A crash during the managed boot (``Worker.start()``) must escape
        ``_run_worker`` so the surrounding ``asyncio.run`` exits non-zero."""
        crash = ValueError("simulated kafka drop")
        worker = MagicMock(spec=Worker)
        worker.start = AsyncMock(side_effect=crash)
        with pytest.raises(ValueError, match="simulated kafka drop"):
            await _run_worker(worker)
