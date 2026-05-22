"""Unit tests for :func:`build_router_definition`.

The router definition is constructed in code (not parsed from disk),
with provider/model/thinking_effort driven by environment variables.
These tests verify the env-driven defaults, the field invariants the
registry depends on, and the schema-level router constraints (no tools,
publish_topic set).
"""

from __future__ import annotations

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.router.definition import ROUTER_AGENT_ID, build_router_definition


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

    def test_thinking_effort_defaults_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_ROUTER_THINKING_EFFORT", raising=False)
        d = build_router_definition()
        assert d.thinking_effort == "none"


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
