"""Unit tests for :func:`build_router_definition`.

The router definition is constructed in code with three configuration
tiers (highest wins): ``router.yml`` field > env var > in-code default.
These tests verify the env-tier defaults, the field invariants the
registry depends on, the schema-level router constraints (no tools,
publish_topic set), and the precedence chain.

Every test runs from a ``tmp_path`` CWD so the default ``./router.yml``
lookup deterministically finds nothing unless the test plants a file
itself — a developer with a stray router.yml in their actual working
directory won't trip the env-tier suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.router.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
)
from calfkit_organization.router.definition import ROUTER_AGENT_ID, build_router_definition


@pytest.fixture(autouse=True)
def _isolated_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin every test's CWD to ``tmp_path`` and clear the path override.

    The router config loader looks for ``./router.yml`` at CWD, so a
    developer who has a stray ``router.yml`` in their real working
    directory would otherwise see flaky pass/fail. Pinning each test's
    CWD to a clean tmp dir makes the default-path behavior deterministic.
    """
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    monkeypatch.chdir(tmp_path)


class TestDefaults:
    """When no env vars are set, the router uses fast/cheap defaults."""

    def test_provider_defaults_to_openai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_ROUTER_PROVIDER", raising=False)
        monkeypatch.delenv("CALFKIT_ROUTER_MODEL", raising=False)
        monkeypatch.delenv("CALFKIT_ROUTER_THINKING_EFFORT", raising=False)
        d = build_router_definition()
        assert d.provider == "openai"

    def test_model_defaults_to_gpt_5_nano(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_ROUTER_MODEL", raising=False)
        d = build_router_definition()
        assert d.model == "gpt-5-nano"


class TestEnvOverrides:
    """Env vars override the in-code defaults so operators can swap
    LLMs without editing source."""

    def test_provider_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_ROUTER_PROVIDER", "anthropic")
        d = build_router_definition()
        assert d.provider == "anthropic"

    def test_model_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_ROUTER_MODEL", "claude-haiku-4-5")
        d = build_router_definition()
        assert d.model == "claude-haiku-4-5"

    def test_thinking_effort_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_ROUTER_THINKING_EFFORT", "high")
        d = build_router_definition()
        assert d.thinking_effort == "high"

    def test_history_turns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_ROUTER_HISTORY_TURNS", raising=False)
        d = build_router_definition()
        assert d.history_turns == 10

    def test_history_turns_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_ROUTER_HISTORY_TURNS", "25")
        d = build_router_definition()
        assert d.history_turns == 25

    def test_history_turns_invalid_string_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-integer value logs WARN and uses the default rather
        than crashing the router build (which would brick deploys)."""
        monkeypatch.setenv("CALFKIT_ROUTER_HISTORY_TURNS", "not-a-number")
        d = build_router_definition()
        assert d.history_turns == 10
        assert any("not an integer" in r.message for r in caplog.records)

    def test_history_turns_out_of_range_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A value outside 0..100 logs WARN and uses the default."""
        monkeypatch.setenv("CALFKIT_ROUTER_HISTORY_TURNS", "999")
        d = build_router_definition()
        assert d.history_turns == 10
        assert any("outside the 0..100" in r.message for r in caplog.records)

    def test_history_turns_zero_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0 is a valid value (disables router history)."""
        monkeypatch.setenv("CALFKIT_ROUTER_HISTORY_TURNS", "0")
        d = build_router_definition()
        assert d.history_turns == 0

    def test_history_turns_negative_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("CALFKIT_ROUTER_HISTORY_TURNS", "-1")
        d = build_router_definition()
        assert d.history_turns == 10
        assert any("outside the 0..100" in r.message for r in caplog.records)


class TestFieldInvariants:
    """The fields that other modules rely on are pinned here.

    Changing any of these is a topology-contract change and should be
    a deliberate, reviewed commit — these assertions exist to surface
    it as a test failure rather than a silent edit.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "CALFKIT_ROUTER_PROVIDER",
            "CALFKIT_ROUTER_MODEL",
            "CALFKIT_ROUTER_THINKING_EFFORT",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_returns_agent_definition(self) -> None:
        d = build_router_definition()
        assert isinstance(d, AgentDefinition)

    def test_agent_id_is_router_constant(self) -> None:
        d = build_router_definition()
        assert d.agent_id == ROUTER_AGENT_ID == "_router"

    def test_slash_is_underscore_router(self) -> None:
        """The slash is reserved (never registered with Discord) but
        must satisfy the schema validator's ``/<name>`` format."""
        d = build_router_definition()
        assert d.slash == "/_router"

    def test_display_name_is_router(self) -> None:
        d = build_router_definition()
        assert d.display_name == "Router"

    def test_role_is_router(self) -> None:
        d = build_router_definition()
        assert d.role == "router"

    def test_publish_topic_is_routing_decisions(self) -> None:
        """The fan-out consumer subscribes to this topic; the router's
        ReturnCall publishes here via FastStream's @publisher
        wrapping. Without the publish_topic set, the router has no
        downstream consumer pathway and the routing decisions go
        nowhere."""
        d = build_router_definition()
        assert d.publish_topic == "routing.decisions"

    def test_tools_is_empty(self) -> None:
        """Routers use the ToolOutput pattern, not function tools.
        The AgentDefinition validator also enforces this; this test
        pins the definition's own value rather than the validator's
        behavior."""
        d = build_router_definition()
        assert d.tools == ()

    def test_source_path_is_none(self) -> None:
        """In-code definition — no on-disk .md file."""
        d = build_router_definition()
        assert d.source_path is None

    def test_avatar_url_is_none(self) -> None:
        d = build_router_definition()
        assert d.avatar_url is None

    def test_system_prompt_is_non_empty(self) -> None:
        d = build_router_definition()
        assert d.system_prompt.strip() != ""
        # Sanity: the hardcoded prompt mentions the dispatch tool
        # (the tool name pydantic-ai's ToolOutput pattern uses).
        assert "dispatch" in d.system_prompt


class TestYamlConfigPrecedence:
    """``router.yml`` field > env var > code default.

    Mirrors :func:`resolve_provider`'s file-beats-env chain. The YAML
    file is the operator's authored source-of-truth; env vars stay as
    a runtime override hook for ops staging a swap without editing the
    file.
    """

    @pytest.fixture(autouse=True)
    def _clean_router_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "CALFKIT_ROUTER_PROVIDER",
            "CALFKIT_ROUTER_MODEL",
            "CALFKIT_ROUTER_THINKING_EFFORT",
            "CALFKIT_ROUTER_HISTORY_TURNS",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_file_supplies_all_fields(self, tmp_path: Path) -> None:
        """With no env vars set, file values flow straight into the
        AgentDefinition."""
        (tmp_path / DEFAULT_CONFIG_PATH).write_text(
            "provider: openai-codex\n"
            "model: gpt-5.3-codex\n"
            "thinking_effort: medium\n"
            "history_turns: 25\n"
        )
        d = build_router_definition()
        assert d.provider == "openai-codex"
        assert d.model == "gpt-5.3-codex"
        assert d.thinking_effort == "medium"
        assert d.history_turns == 25

    def test_file_wins_over_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The file is the authored source-of-truth; env is a runtime
        override layer that should NOT shadow an explicit file value."""
        monkeypatch.setenv("CALFKIT_ROUTER_PROVIDER", "anthropic")
        monkeypatch.setenv("CALFKIT_ROUTER_MODEL", "claude-haiku-4-5")
        (tmp_path / DEFAULT_CONFIG_PATH).write_text(
            "provider: openai\nmodel: gpt-5-mini\n"
        )
        d = build_router_definition()
        assert d.provider == "openai"
        assert d.model == "gpt-5-mini"

    def test_env_fills_gaps_when_file_partial(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Fields the file omits fall through to env vars (then defaults).
        The 3-tier chain is per-field, not all-or-nothing."""
        monkeypatch.setenv("CALFKIT_ROUTER_MODEL", "claude-haiku-4-5")
        monkeypatch.setenv("CALFKIT_ROUTER_THINKING_EFFORT", "high")
        (tmp_path / DEFAULT_CONFIG_PATH).write_text("provider: anthropic\n")
        d = build_router_definition()
        assert d.provider == "anthropic"  # from file
        assert d.model == "claude-haiku-4-5"  # from env
        assert d.thinking_effort == "high"  # from env

    def test_defaults_fill_gaps_when_neither_file_nor_env_set(
        self, tmp_path: Path
    ) -> None:
        """Fields absent from both file and env land on the in-code
        defaults — same path the env-only suite already covers, here
        verified with a partial file present."""
        (tmp_path / DEFAULT_CONFIG_PATH).write_text("thinking_effort: low\n")
        d = build_router_definition()
        assert d.thinking_effort == "low"  # from file
        assert d.provider == "openai"  # code default
        assert d.model == "gpt-5-nano"  # code default
        assert d.history_turns == 10  # code default

    def test_history_turns_file_value_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """history_turns has its own resolver because the env tier has
        validate-and-warn semantics; the file tier should still win."""
        monkeypatch.setenv("CALFKIT_ROUTER_HISTORY_TURNS", "5")
        (tmp_path / DEFAULT_CONFIG_PATH).write_text("history_turns: 50\n")
        d = build_router_definition()
        assert d.history_turns == 50

    def test_history_turns_file_zero_is_respected(self, tmp_path: Path) -> None:
        """``history_turns: 0`` is a valid "disable history" signal;
        the resolver must distinguish 0 from "not set" so a falsy-check
        bug doesn't fall through to the default."""
        (tmp_path / DEFAULT_CONFIG_PATH).write_text("history_turns: 0\n")
        d = build_router_definition()
        assert d.history_turns == 0

    def test_explicit_path_missing_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A typo'd ``CALFKIT_ROUTER_CONFIG_PATH`` propagates the
        ``FileNotFoundError`` out of build_router_definition so the
        runner converts it to a clean CLI exit rather than silently
        booting with defaults."""
        monkeypatch.setenv(CONFIG_PATH_ENV, str(tmp_path / "missing.yml"))
        with pytest.raises(FileNotFoundError):
            build_router_definition()
