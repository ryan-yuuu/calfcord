"""Unit tests for AgentFactory.

The factory constructs a calfkit ``Worker`` over a single vanilla
``Agent`` node. These tests verify the wiring without invoking a real LLM:
the ``model_client_factory`` constructor argument lets us inject a fake
so no provider client is constructed.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from calfkit.nodes import Agent
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfkit_organization.agents.definition import AgentDefinition, Provider
from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.agents.state import AgentRuntimeState


def _definition(
    *,
    agent_id: str = "scheduler",
    provider: Provider | None = None,
    model: str | None = None,
    tools: tuple[str, ...] = (),
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        slash=f"/{agent_id}",
        display_name=f"Test ({agent_id})",
        description="A test agent.",
        provider=provider,
        model=model,
        tools=tools,
        system_prompt="You are a test agent.",
    )


def _model_factory_spy() -> tuple[list[tuple[str, str]], Any]:
    """Return ``(calls, factory)`` where ``calls`` collects ``(provider, model)`` tuples."""
    calls: list[tuple[str, str]] = []

    def factory(provider: Provider, model_name: str) -> PydanticModelClient:
        calls.append((provider, model_name))
        return MagicMock(spec=PydanticModelClient)

    return calls, factory


class TestConstruction:
    def test_constructs_with_required_args(self) -> None:
        factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
        assert factory is not None


class TestBuild:
    def test_returns_worker_with_one_node(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        # Worker stores nodes in ``_nodes`` (internal; verified by reading
        # calfkit/worker/worker.py).
        assert len(worker._nodes) == 1
        assert isinstance(worker._nodes[0], Agent)

    def test_node_identity_matches_definition(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(agent_id="scheduler"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].node_id == "scheduler"

    def test_subscribe_topics_use_in_suffix(self) -> None:
        """Bridge publishes to ``discord.channel.{cid}.in``; agent must match."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(),
            AgentRuntimeState(channels=[100, 200, 300]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics == [
            "discord.channel.100.in",
            "discord.channel.200.in",
            "discord.channel.300.in",
        ]

    def test_subscribe_topic_template_override(self) -> None:
        """The template is configurable for tests / alternate deployments."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            subscribe_topic_template="my.test.channel.{cid}",
        )
        worker = factory.build(
            _definition(),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics == ["my.test.channel.100"]

    def test_gates_registered_in_short_circuit_order(self) -> None:
        """Addressable gate first (cheap), addressed-to-me second (content-based)."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(agent_id="scheduler"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        node = worker._nodes[0]
        assert len(node.gates) == 2
        assert node.gates[0].__name__ == "addressable_scheduler"
        assert node.gates[1].__name__ == "addressed_to_me_scheduler"

    def test_empty_channels_raises(self) -> None:
        """An inert worker (no subscriptions) is a configuration bug; fail fast."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with pytest.raises(ValueError, match="no channels"):
            factory.build(_definition(), AgentRuntimeState(channels=[]), MagicMock())


class TestProviderResolution:
    def test_default_provider_is_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "anthropic"

    def test_definition_provider_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_provider="anthropic",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider="openai", model="gpt-5"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "openai"

    def test_env_provider_used_when_definition_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_provider="anthropic",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider=None, model="gpt-5"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "openai"

    def test_ctor_default_used_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_provider="openai",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider=None, model="gpt-5"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][0] == "openai"

    def test_unknown_env_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var can carry a typo; surface it at build time."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            factory.build(
                _definition(provider=None),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )


class TestModelResolution:
    def test_definition_model_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Definition takes precedence over env var, ctor default, and provider default."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_model="claude-from-ctor",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(model="claude-from-defn"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][1] == "claude-from-defn"

    def test_env_var_used_when_definition_model_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_model="claude-from-ctor",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][1] == "claude-from-env"

    def test_ctor_default_used_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            default_model="claude-from-ctor",
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0][1] == "claude-from-ctor"

    def test_provider_default_used_as_final_fallback_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without any model hint, anthropic agents fall back to the project's
        default Claude model."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider="anthropic", model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0] == ("anthropic", "claude-sonnet-4-5")

    def test_provider_default_used_as_final_fallback_openai(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without any model hint, openai agents fall back to the OpenAI default."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider="openai", model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0] == ("openai", "gpt-5-mini")


class TestToolsWarning:
    def test_non_empty_tools_logs_warning_with_agent_and_tools(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with caplog.at_level(logging.WARNING):
            factory.build(
                _definition(agent_id="scheduler", tools=("calendar", "email")),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )
        assert any(
            "scheduler" in r.message and "calendar" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )

    def test_empty_tools_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with caplog.at_level(logging.WARNING):
            factory.build(
                _definition(tools=()),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )
        assert not any("tools" in r.message for r in caplog.records)
