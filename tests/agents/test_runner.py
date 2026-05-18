"""Unit tests for the runner's bootstrap-env-var helper and state load/seed flow."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from calfkit_organization.agents.runner import (
    BootstrapError,
    _load_or_bootstrap_state,
    _parse_channel_ids,
    bootstrap_env_var,
)
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore


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
