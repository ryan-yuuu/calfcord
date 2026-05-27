"""Unit tests for the runner's bootstrap-env-var helper and state load/seed flow."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any
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
    _publish_departures_best_effort,
    _resolve_agent_specs,
    _run_worker,
    bootstrap_env_var,
)
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore
from calfkit_organization.control_plane.definition_ref import AgentDefinitionRef


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


# ---------------------------------------------------------------------------
# Helpers + fakes for control-plane wiring tests
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Records (topic, payload) tuples for every publish call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(
        self, payload: str, *, topic: str, key: bytes | None = None
    ) -> None:
        self.calls.append({"topic": topic, "payload": payload, "key": key})


class _FakeClient:
    def __init__(self) -> None:
        self._connection = _FakeConnection()


class _StuckConnection:
    """publish() never completes — simulates a hung Kafka producer."""

    async def publish(
        self, payload: str, *, topic: str, key: bytes | None = None
    ) -> None:
        await asyncio.Event().wait()  # never returns


class _StuckClient:
    def __init__(self) -> None:
        self._connection = _StuckConnection()


class _RaisingConnection:
    """publish() always raises."""

    async def publish(
        self, payload: str, *, topic: str, key: bytes | None = None
    ) -> None:
        raise RuntimeError("simulated kafka outage")


class _RaisingClient:
    def __init__(self) -> None:
        self._connection = _RaisingConnection()


def _make_ref(agent_id: str) -> AgentDefinitionRef:
    """Build an AgentDefinitionRef around a minimal valid AgentDefinition."""
    return AgentDefinitionRef(
        current=AgentDefinition(
            agent_id=agent_id,
            slash=f"/{agent_id}",
            display_name=agent_id.title(),
            description=f"Test agent {agent_id}.",
            system_prompt=f"You are {agent_id}.",
        ),
    )


class TestPublishDeparturesBestEffort:
    """The shutdown helper must publish once per agent, swallow timeouts /
    exceptions, and bound total wall time at ~timeout regardless of count."""

    async def test_publishes_one_per_agent(self) -> None:
        """Every AgentDefinitionRef gets a departure publish."""
        client = _FakeClient()
        refs = [_make_ref("echo"), _make_ref("scribe"), _make_ref("bridge")]

        await _publish_departures_best_effort(client, refs)  # type: ignore[arg-type]

        assert len(client._connection.calls) == 3
        topics = {call["topic"] for call in client._connection.calls}
        assert topics == {"agent.state"}

        agent_ids = sorted(
            call["payload"]["agent_id"] for call in client._connection.calls
        )
        assert agent_ids == ["bridge", "echo", "scribe"]

        # Each payload carries the departure discriminator on the wire so
        # the bridge dispatches it correctly.
        kinds = {call["payload"]["kind"] for call in client._connection.calls}
        assert kinds == {"departure"}

    async def test_timeout_is_swallowed(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A publish that exceeds the timeout is logged and skipped, not raised."""
        client = _StuckClient()
        refs = [_make_ref("echo")]

        with caplog.at_level(logging.WARNING):
            await _publish_departures_best_effort(
                client, refs, timeout=0.05,  # type: ignore[arg-type]
            )

        warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "departure publish timed out for agent=echo" in r.message
            for r in warnings
        ), f"expected timeout warning, got: {[r.message for r in caplog.records]}"

    async def test_exception_is_swallowed(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A publish that raises is logged at ERROR and skipped, not raised."""
        client = _RaisingClient()
        refs = [_make_ref("echo")]

        with caplog.at_level(logging.ERROR):
            await _publish_departures_best_effort(
                client, refs, timeout=1.0,  # type: ignore[arg-type]
            )

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "departure publish failed for agent=echo" in r.message
            for r in errors
        )
        # exc_info attached via logger.exception so operators get the traceback.
        assert any(r.exc_info is not None for r in errors)

    async def test_parallel_total_time_is_bounded(self) -> None:
        """With N stalled publishes, total time is ~timeout (not N*timeout)."""
        client = _StuckClient()
        refs = [_make_ref(f"agent-{i}") for i in range(5)]
        timeout = 0.1

        start = time.monotonic()
        await _publish_departures_best_effort(
            client, refs, timeout=timeout,  # type: ignore[arg-type]
        )
        elapsed = time.monotonic() - start

        # Generous slack for scheduling on a busy CI runner. The serial
        # equivalent would be 5*0.1 = 0.5s; we want to prove parallel.
        assert elapsed < timeout + 0.5, (
            f"total elapsed {elapsed:.3f}s exceeded parallel budget"
        )


class TestRunWorkerShutdownCallback:
    """``_run_worker`` blocks on a SIGINT/SIGTERM stop event and runs an
    optional ``on_shutdown_signal`` callback while the broker is still
    alive. The caller (``_amain``) is responsible for ``register_handlers``
    + ``broker.start``; this helper only owns the signal-wait + drain hook.
    """

    @staticmethod
    def _disable_signal_handlers(
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[int, Any]:
        """Patch ``loop.add_signal_handler`` to capture (sig, callback) pairs
        rather than register OS-level handlers. Tests can fire a captured
        handler synchronously to simulate a signal without process-level
        side effects."""
        loop = asyncio.get_running_loop()
        captured: dict[int, Any] = {}

        def _fake_add(sig: int, cb: Any, *args: Any) -> None:
            captured[sig] = cb

        monkeypatch.setattr(loop, "add_signal_handler", _fake_add)
        return captured

    async def test_callback_fires_on_signal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SIGINT triggers on_shutdown_signal before _run_worker returns."""
        captured = self._disable_signal_handlers(monkeypatch)
        callback_fired = asyncio.Event()

        async def _on_shutdown() -> None:
            callback_fired.set()

        run_task = asyncio.create_task(
            _run_worker(num_agents=1, on_shutdown_signal=_on_shutdown),
        )
        # Give _run_worker a chance to register handlers + start the wait.
        for _ in range(50):
            await asyncio.sleep(0)
            if signal.SIGINT in captured:
                break
        assert signal.SIGINT in captured, "handler never registered"

        # Synchronously set the stop event the way the real signal handler would.
        captured[signal.SIGINT]()
        await run_task
        assert callback_fired.is_set()

    async def test_callback_not_required(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Passing ``on_shutdown_signal=None`` is a no-op past the stop wait."""
        captured = self._disable_signal_handlers(monkeypatch)

        run_task = asyncio.create_task(_run_worker(num_agents=1))
        for _ in range(50):
            await asyncio.sleep(0)
            if signal.SIGINT in captured:
                break
        assert signal.SIGINT in captured, "handler never registered"

        captured[signal.SIGINT]()
        # Must complete without raising — the absence of a callback is
        # not an error.
        await run_task

    async def test_callback_exception_is_swallowed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An exception from ``on_shutdown_signal`` is logged at ERROR
        and swallowed — a misbehaving callback must not block teardown."""
        captured = self._disable_signal_handlers(monkeypatch)

        async def _bad_callback() -> None:
            raise RuntimeError("callback boom")

        with caplog.at_level(logging.ERROR):
            run_task = asyncio.create_task(
                _run_worker(num_agents=1, on_shutdown_signal=_bad_callback),
            )
            for _ in range(50):
                await asyncio.sleep(0)
                if signal.SIGINT in captured:
                    break
            assert signal.SIGINT in captured, "handler never registered"
            captured[signal.SIGINT]()
            # Must NOT raise — callback errors are swallowed.
            await run_task

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(
            "on_shutdown_signal callback raised" in r.message for r in errors
        ), f"expected callback-raised log, got: {[r.message for r in errors]}"
