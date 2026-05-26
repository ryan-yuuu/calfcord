"""Unit tests for the runner's bootstrap-env-var helper and state load/seed flow."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.runner import (
    BootstrapError,
    _build_node_or_bootstrap_error,
    _load_or_bootstrap_state,
    _parse_args,
    _parse_channel_ids,
    _prewarm_codex_if_needed,
    _resolve_agent_specs,
    _run_worker,
    bootstrap_env_var,
)
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore


def _write_agent_md(dir_: Path, name: str) -> None:
    """Write a minimal valid agents/<name>.md fixture in ``dir_``."""
    body = (
        "---\n"
        f"name: {name}\n"
        f"slash: /{name}\n"
        f"display_name: {name.title()}\n"
        f"description: Test agent {name}.\n"
        "---\n"
        "\n"
        f"You are {name}.\n"
    )
    (dir_ / f"{name}.md").write_text(body)


class TestBootstrapEnvVar:
    def test_uppercases_simple_name(self) -> None:
        assert bootstrap_env_var("echo") == "CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS"

    def test_hyphens_become_underscores(self) -> None:
        # POSIX env var names cannot contain hyphens.
        assert (
            bootstrap_env_var("code-quality-reviewer")
            == "CALFKIT_AGENT_CODE_QUALITY_REVIEWER_BOOTSTRAP_CHANNELS"
        )


class TestParseChannelIds:
    def test_strips_whitespace_and_blanks(self) -> None:
        assert _parse_channel_ids(" 111, 222 ,, 333 ", env_var="X") == [111, 222, 333]

    def test_empty_string_returns_empty(self) -> None:
        assert _parse_channel_ids("", env_var="X") == []
        assert _parse_channel_ids("  ,, ,", env_var="X") == []

    def test_non_integer_token_names_offender(self) -> None:
        with pytest.raises(BootstrapError, match="invalid channel id 'nope'"):
            _parse_channel_ids("111, nope, 222", env_var="X")

    def test_error_names_env_var(self) -> None:
        with pytest.raises(BootstrapError, match="MY_VAR"):
            _parse_channel_ids("abc", env_var="MY_VAR")


class TestLoadOrBootstrapState:
    @pytest.fixture
    def store(self, tmp_path: Path) -> AgentStateStore:
        return AgentStateStore(tmp_path / "echo.json")

    async def test_loads_existing_state(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await store.save(AgentRuntimeState(channels=[42]))
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        state = await _load_or_bootstrap_state(store, "echo")
        assert state.channels == [42]

    async def test_bootstrap_from_env_when_state_absent(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", "111, 222 , 333")
        state = await _load_or_bootstrap_state(store, "echo")
        assert state.channels == [111, 222, 333]
        # File was written so the next boot ignores the env var.
        assert store.path.exists()
        reloaded = await store.load()
        assert reloaded.channels == [111, 222, 333]

    async def test_bootstrap_logs_warning_with_cleanup_hint(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The bootstrap is rare and load-bearing; surface it at WARNING with a hint."""
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", "555")
        with caplog.at_level(logging.WARNING):
            await _load_or_bootstrap_state(store, "echo")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("bootstrapped" in r.message for r in warnings)
        assert any("clear" in r.message.lower() for r in warnings)

    async def test_missing_state_and_env_exits(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.delenv("DISCORD_DEFAULT_CHANNEL_ID", raising=False)
        with pytest.raises(BootstrapError, match="CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS"):
            await _load_or_bootstrap_state(store, "echo")

    async def test_missing_state_and_env_mentions_default_channel_id(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fallback should be discoverable from the error message."""
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.delenv("DISCORD_DEFAULT_CHANNEL_ID", raising=False)
        with pytest.raises(BootstrapError, match="DISCORD_DEFAULT_CHANNEL_ID"):
            await _load_or_bootstrap_state(store, "echo")

    async def test_empty_env_var_exits(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", " , , ")
        with pytest.raises(BootstrapError, match="parsed to zero channels"):
            await _load_or_bootstrap_state(store, "echo")

    async def test_non_integer_bootstrap_value_exits_with_token_in_message(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", "123,nope,456")
        with pytest.raises(BootstrapError, match="invalid channel id 'nope'"):
            await _load_or_bootstrap_state(store, "echo")

    async def test_env_var_ignored_when_state_file_exists(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await store.save(AgentRuntimeState(channels=[42]))
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", "999")
        with caplog.at_level(logging.WARNING):
            state = await _load_or_bootstrap_state(store, "echo")
        assert state.channels == [42]
        assert any("ignoring" in r.message for r in caplog.records)

    async def test_bootstrap_falls_back_to_default_channel_id(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the per-agent var is unset, DISCORD_DEFAULT_CHANNEL_ID seeds state."""
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.setenv("DISCORD_DEFAULT_CHANNEL_ID", "555")
        state = await _load_or_bootstrap_state(store, "echo")
        assert state.channels == [555]
        assert store.path.exists()

    async def test_bootstrap_default_channel_id_supports_comma_list(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parser is comma-aware so a multi-channel dev env works too."""
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.setenv("DISCORD_DEFAULT_CHANNEL_ID", "111, 222 ,333")
        state = await _load_or_bootstrap_state(store, "echo")
        assert state.channels == [111, 222, 333]

    async def test_per_agent_env_var_wins_over_default_channel_id(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit per-agent intent beats shared dev fallback."""
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", "111")
        monkeypatch.setenv("DISCORD_DEFAULT_CHANNEL_ID", "999")
        state = await _load_or_bootstrap_state(store, "echo")
        assert state.channels == [111]

    async def test_default_channel_id_bootstrap_logs_source(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Log line names the env var actually used so reader knows the source."""
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.setenv("DISCORD_DEFAULT_CHANNEL_ID", "555")
        with caplog.at_level(logging.WARNING):
            await _load_or_bootstrap_state(store, "echo")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("DISCORD_DEFAULT_CHANNEL_ID" in r.message for r in warnings)

    async def test_empty_default_channel_id_exits(
        self,
        store: AgentStateStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whitespace-only DISCORD_DEFAULT_CHANNEL_ID doesn't accidentally bootstrap."""
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.setenv("DISCORD_DEFAULT_CHANNEL_ID", " , , ")
        with pytest.raises(BootstrapError, match="parsed to zero channels"):
            await _load_or_bootstrap_state(store, "echo")


class TestBuildNodeOrBootstrapError:
    """The factory.build_node wrap converts every failure mode into BootstrapError
    so the CLI exits cleanly instead of dumping a traceback."""

    @staticmethod
    def _definition() -> AgentDefinition:
        return AgentDefinition(
            agent_id="echo",
            slash="/echo",
            display_name="Echo",
            description="Test.",
            system_prompt="Test echo.",
        )

    def test_value_error_wrapped(self) -> None:
        factory = MagicMock()
        factory.build_node.side_effect = ValueError("no channels")
        with pytest.raises(BootstrapError, match="echo.*failed to construct.*no channels"):
            _build_node_or_bootstrap_error(
                factory,
                self._definition(),
                AgentRuntimeState(channels=[1]),
                MagicMock(),
            )

    def test_runtime_error_wrapped(self) -> None:
        """pydantic_ai raises UserError(RuntimeError) on missing API keys —
        not ValueError or KeyError. The wrap must catch this too."""
        factory = MagicMock()
        factory.build_node.side_effect = RuntimeError("ANTHROPIC_API_KEY is not set")
        with pytest.raises(BootstrapError, match="ANTHROPIC_API_KEY"):
            _build_node_or_bootstrap_error(
                factory,
                self._definition(),
                AgentRuntimeState(channels=[1]),
                MagicMock(),
            )

    def test_happy_path_returns_node(self) -> None:
        factory = MagicMock()
        sentinel = object()
        factory.build_node.return_value = sentinel
        result = _build_node_or_bootstrap_error(
            factory,
            self._definition(),
            AgentRuntimeState(channels=[1]),
            MagicMock(),
        )
        assert result is sentinel


class TestParseArgs:
    def test_no_positional_returns_none(self) -> None:
        """All-agents mode: omitting the positional arg yields agent=None."""
        args = _parse_args([])
        assert args.agent is None

    def test_named_agent_returns_name(self) -> None:
        """Single-agent mode is unchanged: the positional arg lands as-is."""
        args = _parse_args(["echo"])
        assert args.agent == "echo"


class TestResolveAgentSpecs:
    """``_resolve_agent_specs`` is the unified entry point for both runner modes.

    Single-agent (``agent_name`` set) → list of one; fail-fast on bootstrap.
    All-agents (``agent_name`` is None) → list of N; aggregate bootstrap failures.
    """

    @pytest.fixture
    def agents_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "agents"
        d.mkdir()
        return d

    @pytest.fixture
    def state_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "state"
        d.mkdir()
        return d

    async def test_all_mode_builds_specs_sorted_by_agent_id(
        self,
        agents_dir: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_agent_md(agents_dir, "scribe")
        _write_agent_md(agents_dir, "echo")
        # load_agents_dir sorts by stem; both agents bootstrap from
        # DISCORD_DEFAULT_CHANNEL_ID since neither per-agent env is set.
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.delenv("CALFKIT_AGENT_SCRIBE_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.setenv("DISCORD_DEFAULT_CHANNEL_ID", "100")

        specs = await _resolve_agent_specs(None, agents_dir, state_dir)

        assert [d.agent_id for d, _, _ in specs] == ["echo", "scribe"]
        assert all(s.channels == [100] for _, s, _ in specs)

    async def test_all_mode_aggregates_bootstrap_failures(
        self,
        agents_dir: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multiple misconfigured agents → one error naming every offender."""
        _write_agent_md(agents_dir, "echo")
        _write_agent_md(agents_dir, "scribe")
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.delenv("CALFKIT_AGENT_SCRIBE_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.delenv("DISCORD_DEFAULT_CHANNEL_ID", raising=False)

        with pytest.raises(BootstrapError) as exc_info:
            await _resolve_agent_specs(None, agents_dir, state_dir)

        msg = str(exc_info.value)
        assert "2 agent(s)" in msg
        assert "echo" in msg
        assert "scribe" in msg

    async def test_all_mode_partial_failure_still_aggregates(
        self,
        agents_dir: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If one agent bootstraps cleanly and another doesn't, still raise
        — but only the misconfigured one is named in the error."""
        _write_agent_md(agents_dir, "echo")
        _write_agent_md(agents_dir, "scribe")
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", "100")
        monkeypatch.delenv("CALFKIT_AGENT_SCRIBE_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.delenv("DISCORD_DEFAULT_CHANNEL_ID", raising=False)

        with pytest.raises(BootstrapError) as exc_info:
            await _resolve_agent_specs(None, agents_dir, state_dir)

        msg = str(exc_info.value)
        assert "1 agent(s)" in msg
        assert "scribe" in msg
        assert "echo:" not in msg  # Hyphen-prefix in the list-line format

    async def test_all_mode_empty_agents_dir_exits(
        self,
        agents_dir: Path,
        state_dir: Path,
    ) -> None:
        """An empty agents directory is a misconfiguration, not a no-op."""
        with pytest.raises(BootstrapError, match="no agent definitions found"):
            await _resolve_agent_specs(None, agents_dir, state_dir)

    async def test_single_mode_returns_one_spec(
        self,
        agents_dir: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Backwards-compat: passing a name returns just that agent."""
        _write_agent_md(agents_dir, "echo")
        _write_agent_md(agents_dir, "scribe")
        monkeypatch.setenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", "100")

        specs = await _resolve_agent_specs("echo", agents_dir, state_dir)

        assert len(specs) == 1
        assert specs[0][0].agent_id == "echo"

    async def test_single_mode_unknown_agent_lists_known(
        self,
        agents_dir: Path,
        state_dir: Path,
    ) -> None:
        """Existing single-mode error message is unchanged."""
        _write_agent_md(agents_dir, "echo")
        with pytest.raises(BootstrapError, match=r"not found.*Known: echo"):
            await _resolve_agent_specs("ghost", agents_dir, state_dir)

    async def test_single_mode_bootstrap_failure_is_unwrapped(
        self,
        agents_dir: Path,
        state_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single-mode bootstrap failure must NOT get the all-mode aggregation
        envelope. Operators invoking ``calfkit-agent <name>`` should see the
        same bare per-agent message the pre-all-mode runner produced."""
        _write_agent_md(agents_dir, "echo")
        monkeypatch.delenv("CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS", raising=False)
        monkeypatch.delenv("DISCORD_DEFAULT_CHANNEL_ID", raising=False)

        with pytest.raises(BootstrapError) as exc_info:
            await _resolve_agent_specs("echo", agents_dir, state_dir)

        msg = str(exc_info.value)
        assert "no state file" in msg
        assert "CALFKIT_AGENT_ECHO_BOOTSTRAP_CHANNELS" in msg
        # The aggregation wrapper must not appear in single-mode.
        assert "bootstrap failed for" not in msg
        assert "agent(s):" not in msg

    async def test_all_mode_corrupt_state_file_aggregated(
        self,
        agents_dir: Path,
        state_dir: Path,
    ) -> None:
        """A state file with malformed JSON must produce a BootstrapError
        rather than letting a raw json.JSONDecodeError escape — and must be
        collected into the aggregation alongside other agents' results."""
        _write_agent_md(agents_dir, "echo")
        _write_agent_md(agents_dir, "scribe")
        (state_dir / "echo.json").write_text("{not json")
        await AgentStateStore(state_dir / "scribe.json").save(
            AgentRuntimeState(channels=[200])
        )

        with pytest.raises(BootstrapError) as exc_info:
            await _resolve_agent_specs(None, agents_dir, state_dir)

        msg = str(exc_info.value)
        assert "1 agent(s)" in msg
        assert "echo" in msg
        assert "failed to read state file" in msg

    async def test_all_mode_invalid_state_schema_aggregated(
        self,
        agents_dir: Path,
        state_dir: Path,
    ) -> None:
        """A state file whose JSON is valid but the schema is wrong (channels
        is a string, not a list) produces pydantic.ValidationError. That's a
        ValueError subclass, so the (OSError, ValueError) catch in
        _load_or_bootstrap_state converts it to BootstrapError."""
        _write_agent_md(agents_dir, "echo")
        (state_dir / "echo.json").write_text(
            '{"schema_version": 1, "channels": "not-a-list"}'
        )

        with pytest.raises(BootstrapError) as exc_info:
            await _resolve_agent_specs(None, agents_dir, state_dir)

        msg = str(exc_info.value)
        assert "echo" in msg
        assert "failed to read state file" in msg

    async def test_all_mode_missing_agents_dir_raises_bootstrap_error(
        self,
        tmp_path: Path,
        state_dir: Path,
    ) -> None:
        """A nonexistent agents directory must surface as a clean
        BootstrapError (not a raw FileNotFoundError traceback) so main()
        can convert it into a clean SystemExit."""
        missing = tmp_path / "nonexistent"
        with pytest.raises(BootstrapError, match="failed to load"):
            await _resolve_agent_specs(None, missing, state_dir)

    async def test_all_mode_malformed_md_raises_bootstrap_error(
        self,
        agents_dir: Path,
        state_dir: Path,
    ) -> None:
        """A bad .md file causes load_agents_dir to raise ValueError; the
        runner must convert it to BootstrapError for a clean exit."""
        _write_agent_md(agents_dir, "echo")
        # Frontmatter name mismatches the filename stem → load_agents_dir
        # raises ValueError from parse_agent_md.
        (agents_dir / "broken.md").write_text(
            "---\nname: mismatch\n---\nbody\n"
        )
        with pytest.raises(BootstrapError, match="failed to load"):
            await _resolve_agent_specs(None, agents_dir, state_dir)


class TestRunWorker:
    """``_run_worker`` is the shared shutdown/error-surfacing helper. It
    must (a) re-raise any exception that escapes ``worker.run`` so the
    process exits non-zero, and (b) handle clean returns and signal-driven
    shutdown without hanging."""

    async def test_worker_exception_is_reraised(self) -> None:
        """If worker.run() raises, _run_worker must log and re-raise so
        the process exits non-zero. The gather(return_exceptions=True)
        on the drain path must not swallow the exception."""
        worker = MagicMock()
        worker.run = AsyncMock(side_effect=RuntimeError("kafka broker drop"))

        with pytest.raises(RuntimeError, match="kafka broker drop"):
            await _run_worker(worker, num_agents=2)

    async def test_clean_worker_return_raises_runtime_error(self) -> None:
        """A clean ``worker.run()`` return without a shutdown signal is
        unexpected — ``worker.run`` is meant to be infinite. Treat as a
        crash so supervisors configured for ``Restart=on-failure`` restart
        us; without this, the process exits 0 and the supervisor leaves
        us down."""
        worker = MagicMock()
        worker.run = AsyncMock(return_value=None)

        with pytest.raises(RuntimeError, match="returned unexpectedly"):
            await _run_worker(worker, num_agents=1)

    async def test_clean_worker_return_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The synthetic RuntimeError must be logged at ERROR so operators
        see it in the same band as a real crash, not buried at warning."""
        worker = MagicMock()
        worker.run = AsyncMock(return_value=None)

        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
            await _run_worker(worker, num_agents=1)

        assert any(
            "returned unexpectedly" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        )

    async def test_worker_exception_is_logged_with_exc_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The error log line must carry exc_info so the traceback lands
        in the log stream — silent exit was the C2 production bug."""
        worker = MagicMock()
        worker.run = AsyncMock(side_effect=RuntimeError("kafka broker drop"))

        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
            await _run_worker(worker, num_agents=1)

        crash_logs = [
            r for r in caplog.records
            if r.levelno >= logging.ERROR and "worker crashed" in r.message
        ]
        assert crash_logs, "expected an ERROR log naming the worker crash"
        assert crash_logs[0].exc_info is not None

    async def test_signal_path_logs_drain_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the stop signal fires first (e.g., Ctrl-C), the drain log
        must name the agent count so operators don't think it hung."""
        # Simulate the signal path by making worker.run hang forever and
        # cancelling the _run_worker call externally — the cancellation
        # is delivered to worker_task while stop_task is still pending,
        # which mirrors what happens when asyncio.wait completes on a
        # non-signal cancellation. To exercise the actual signal branch
        # without touching real signals, run _run_worker as a task and
        # cancel it; the stop_task path is the closest analogue we can
        # exercise in-process. (Signal-handler installation is a process-
        # level side effect we don't want to assert against in unit tests.)
        worker = MagicMock()
        # A run that hangs until cancelled.
        running = asyncio.Event()
        async def _hang() -> None:
            running.set()
            await asyncio.Event().wait()
        worker.run = _hang

        task = asyncio.create_task(_run_worker(worker, num_agents=3))
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # No explicit log assertion here — the cancellation path runs
        # through the finally drain, which is what we wanted to cover.
        # The drain log line is exercised by the real-signal path which
        # we can't unit-test portably; this test guards against the
        # cancel/drain path hanging or raising the wrong exception type.


class TestPrewarmCodexIfNeeded:
    """``_prewarm_codex_if_needed`` is the runner's bridge between agent specs
    and the openai-codex prompt resolver. Must invoke prewarm exactly when at
    least one resolved agent ends up on the openai-codex provider — including
    via the ``CALFKIT_AGENT_DEFAULT_PROVIDER`` env-var path, which a naive
    ``spec.provider == "openai-codex"`` check would miss for agents that omit
    the frontmatter field entirely.
    """

    def _spec(self, provider: str | None) -> tuple:
        """Build a (definition, state, store) triple matching the runner's spec shape."""
        definition = MagicMock(spec=AgentDefinition)
        definition.provider = provider
        definition.agent_id = "test-agent"
        return (definition, MagicMock(spec=AgentRuntimeState), MagicMock(spec=AgentStateStore))

    @pytest.mark.asyncio
    async def test_skips_prewarm_when_no_codex_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No agent declares openai-codex and no env-var override — prewarm
        must NOT be invoked (avoids the authlib/openhands-sdk import cost for
        non-codex deployments)."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        called = False

        async def _fake_prewarm() -> None:
            nonlocal called
            called = True

        import calfkit_organization.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", _fake_prewarm)
        await _prewarm_codex_if_needed([self._spec("anthropic"), self._spec("openai")])
        assert called is False

    @pytest.mark.asyncio
    async def test_invokes_prewarm_when_any_agent_declares_openai_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        prewarm = AsyncMock()
        import calfkit_organization.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", prewarm)
        await _prewarm_codex_if_needed(
            [self._spec("anthropic"), self._spec("openai-codex")]
        )
        prewarm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invokes_prewarm_when_env_var_default_is_openai_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the C1 fix: an agent that omits ``provider:``
        from its frontmatter must still trigger prewarm when the operator has
        set ``CALFKIT_AGENT_DEFAULT_PROVIDER=openai-codex``. The pre-fix
        check (``spec[0].provider == "openai-codex"``) saw ``None`` and
        silently skipped, leaving the factory to crash mid-construction with
        an opaque RuntimeError."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai-codex")
        prewarm = AsyncMock()
        import calfkit_organization.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", prewarm)
        # Agent's frontmatter omits provider; only the env var selects codex.
        await _prewarm_codex_if_needed([self._spec(None)])
        prewarm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_converts_codex_prompts_unavailable_to_bootstrap_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When upstream prompts can't be fetched and no cache exists, the
        runner must wrap the typed exception in BootstrapError with a hint
        pointing the operator at the CLI — otherwise the worker process would
        crash with an unactionable traceback."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        from calfkit_organization.providers.codex import CodexPromptsUnavailableError
        import calfkit_organization.providers.codex as codex_pkg

        async def _failing_prewarm() -> None:
            raise CodexPromptsUnavailableError("simulated network failure")

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", _failing_prewarm)
        with pytest.raises(BootstrapError, match="refresh-prompts"):
            await _prewarm_codex_if_needed([self._spec("openai-codex")])
