"""Unit tests for AgentFactory.

The factory constructs a calfkit ``Worker`` over a single vanilla
``Agent`` node. These tests verify the wiring without invoking a real LLM:
the ``model_client_factory`` constructor argument lets us inject a fake
so no provider client is constructed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from calfkit.mcp import McpToolDef
from calfkit.nodes import Agent
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfcord.agents.definition import AgentDefinition, Provider
from calfcord.agents.factory import AgentFactory, resolve_provider
from calfcord.agents.memory import MEMORY_PROMPT_DEPS_KEY
from calfcord.agents.state import AgentRuntimeState


def _definition(
    *,
    agent_id: str = "scheduler",
    provider: Provider | None = None,
    model: str | None = None,
    tools: tuple[str, ...] = (),
    thinking_effort: str | None = None,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        display_name=f"Test ({agent_id})",
        description="A test agent.",
        provider=provider,
        model=model,
        tools=tools,
        thinking_effort=thinking_effort,  # type: ignore[arg-type]
        system_prompt="You are a test agent.",
    )


def _memory_definition(
    *,
    agent_id: str = "scribe",
    tools: tuple[str, ...] | None = (),
) -> AgentDefinition:
    """A ``memory: true`` definition (built against the real TOOL_REGISTRY)."""
    return AgentDefinition(
        agent_id=agent_id,
        display_name=f"Test ({agent_id})",
        description="A test agent.",
        tools=tools,
        memory=True,
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
        """Bridge publishes to ``discord.channel.{cid}.in``; agent must match.
        Per-agent private return topic is at index [0]; per-agent inbox
        ``agent.{id}.in`` is appended last for A2A."""
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
            "scheduler.private.return",
            "discord.channel.100.in",
            "discord.channel.200.in",
            "discord.channel.300.in",
            "agent.scheduler.in",
        ]

    def test_subscribe_topic_template_override(self) -> None:
        """The template is configurable for tests / alternate deployments.
        The per-agent inbox and private return topic are independent of
        the channel template."""
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
        assert worker._nodes[0].subscribe_topics == [
            "scheduler.private.return",
            "my.test.channel.100",
            "agent.scheduler.in",
        ]

    def test_per_agent_inbox_uses_agent_id(self) -> None:
        """Inbox suffix derives from agent_id, not display_name or slash."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(agent_id="researcher"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics[-1] == "agent.researcher.in"

    def test_private_return_topic_is_at_index_zero(self) -> None:
        """Calfkit uses ``subscribe_topics[0]`` as the callback topic for
        tool ``Call`` envelopes (``base.py:_publish_action``) and as the
        ``TailCall`` retry target (``agent.py``). If a co-tenant agent's
        channel topic ever ended up at index 0, the other agent would
        receive every tool return and emit duplicate replies — pin the
        ordering invariant here so a future refactor that reorders the
        list trips this test first."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(agent_id="researcher"),
            AgentRuntimeState(channels=[100, 200]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics[0] == "researcher.private.return"

    def test_private_return_topic_is_per_agent(self) -> None:
        """Two co-tenant agents must get distinct private return topics —
        otherwise the workaround collapses back to the shared-topic bug.
        The topic name is derived from agent_id, so distinct ids produce
        distinct topics by construction; this guard pins that contract."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker_a = factory.build(
            _definition(agent_id="alpha"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        worker_b = factory.build(
            _definition(agent_id="bravo"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker_a._nodes[0].subscribe_topics[0] != worker_b._nodes[0].subscribe_topics[0]
        assert worker_a._nodes[0].subscribe_topics[0] == "alpha.private.return"
        assert worker_b._nodes[0].subscribe_topics[0] == "bravo.private.return"

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

    def test_openai_codex_resolves_to_none_when_no_model_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """openai-codex has no static default: with no model hint, ``None`` is
        passed through so the Codex client resolves a live-catalog default."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _definition(provider="openai-codex", model=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert calls[0] == ("openai-codex", None)


class TestRequireModelGuard:
    """``_default_model_client_factory`` must reject ``None`` for providers that
    have no catalog-resolved default — only openai-codex tolerates it."""

    @pytest.mark.parametrize("provider", ["anthropic", "openai"])
    def test_none_model_raises_for_non_codex(self, provider: str) -> None:
        from calfcord.agents.factory import _default_model_client_factory

        with pytest.raises(ValueError, match="requires a model name"):
            _default_model_client_factory(provider, None)  # type: ignore[arg-type]


class TestThinkingEffortBaking:
    """Factory passes definition.thinking_effort through build_model_settings
    into the calfkit Agent constructor as a tier-2 default."""

    def test_anthropic_high_passes_thinking_dict(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="anthropic", thinking_effort="high"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop  # internal access acceptable in tests
        assert agent_loop.model_settings == {
            "anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}
        }

    def test_openai_medium_passes_reasoning_effort(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="openai", thinking_effort="medium"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop
        # Matches the operator → OpenAI mapping in
        # :mod:`calfcord.agents.thinking`: operator
        # ``medium`` → OpenAI ``"medium"`` after the ramp shift that
        # accompanied the ``minimal`` tier addition. Was ``"low"``
        # under the previous mapping.
        assert agent_loop.model_settings == {"openai_reasoning_effort": "medium"}

    def test_no_effort_in_definition_no_model_settings(self) -> None:
        """thinking_effort=None → no tier-2 model_settings."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="anthropic"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings is None

    def test_effort_none_passes_empty_dict(self) -> None:
        """Explicit "none" → empty dict (calfkit merges as no-op)."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(provider="openai", thinking_effort="none"),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings == {}


class TestResolveProviderModuleFunction:
    """``resolve_provider`` is lifted to module scope so the bridge can reuse it."""

    def test_definition_provider_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        assert (
            resolve_provider(_definition(provider="openai"), default_provider="anthropic")
            == "openai"
        )

    def test_env_var_used_when_definition_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        assert (
            resolve_provider(_definition(provider=None), default_provider="anthropic")
            == "openai"
        )

    def test_default_used_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        assert (
            resolve_provider(_definition(provider=None), default_provider="openai")
            == "openai"
        )

    def test_unknown_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            resolve_provider(_definition(provider=None))


def _fake_tool_node(name: str) -> Any:
    """Build a real ``ToolNodeDef`` whose schema name is ``name``.

    calfkit's ``Agent(tools=...)`` flattener (``_flatten_tools``) rejects
    anything that is not a ``ToolNodeDef`` / ``BaseToolNodeSchema`` /
    ``McpServer``, so a bare ``MagicMock`` no longer works as a stand-in.
    ``agent_tool`` derives the schema name from the wrapped function's
    ``__name__``; we rewrite ``__name__`` to ``name`` so the registered key,
    the schema name, and the wiring assertions all line up.
    """
    from calfkit.nodes import agent_tool

    async def _impl(ctx: Any, payload: str) -> str:
        """Trivial tool body for wiring tests (never executed here)."""
        return payload

    _impl.__name__ = name
    return agent_tool(_impl)


class TestToolsWiring:
    """``definition.tools`` names are resolved against the registry and passed
    to the calfkit ``Agent``. Unknown names raise at build time."""

    def test_empty_tools_passes_none_to_agent(self) -> None:
        """Empty tuple → ``Agent(tools=None)`` (calfkit's no-tools sentinel).
        ``tools=()`` is the explicit "I want no tools" frontmatter case."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={},
        )
        worker = factory.build(
            _definition(tools=()),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].tools == []

    def test_tools_none_expands_to_every_registered_tool(self) -> None:
        """``definition.tools is None`` means "frontmatter omitted ``tools:``
        line" — the factory expands it to every entry in the tool registry.
        The loader normalizes this for .md-loaded specs, but code-built
        definitions (tests, the router build path) hit this branch directly."""
        _, model_factory = _model_factory_spy()
        fake_a = _fake_tool_node("alpha")
        fake_b = _fake_tool_node("beta")
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"alpha": fake_a, "beta": fake_b},
        )
        worker = factory.build(
            _definition(tools=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        # ``_resolve_tools`` returns ``registry.values()`` for ``tools=None``,
        # so the wired nodes are exactly the registry's, in insertion order.
        # (``ToolNodeDef`` is unhashable, so compare the list rather than a
        # set.)
        assert worker._nodes[0].tools == [fake_a, fake_b]

    def test_known_tool_name_is_wired_through_registry(self) -> None:
        """A name listed in ``tools:`` resolves to the registry's ToolNodeDef
        and lands in ``Agent.tools``."""
        _, model_factory = _model_factory_spy()
        fake_calendar = _fake_tool_node("calendar")
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"calendar": fake_calendar},
        )
        worker = factory.build(
            _definition(tools=("calendar",)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].tools == [fake_calendar]

    def test_unknown_tool_name_raises_with_known_list(self) -> None:
        """Typo in ``.md`` fails at build, listing every unknown plus
        what the registry actually contains so the operator can fix it."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"calendar": _fake_tool_node("calendar")},
        )
        with pytest.raises(ValueError, match="unknown tool"):
            factory.build(
                _definition(agent_id="scheduler", tools=("calndar",)),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )

    def test_unknown_tool_error_aggregates_multiple_names(self) -> None:
        """Several typos surface in one message — operator fixes the .md once."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"calendar": _fake_tool_node("calendar")},
        )
        with pytest.raises(ValueError) as excinfo:
            factory.build(
                _definition(agent_id="scheduler", tools=("calndar", "emial")),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )
        assert "calndar" in str(excinfo.value)
        assert "emial" in str(excinfo.value)

    @pytest.mark.parametrize(
        "tool_name",
        [
            "shell",
            "read_file",
            "write_file",
            "edit_file",
            "grep",
            "glob",
            "web_fetch",
            "web_search",
            "todo_view",
            "todo_write",
        ],
    )
    def test_builtin_tool_resolves_through_default_registry(self, tool_name: str) -> None:
        """Each builtin tool name listed in an agent's ``.md`` resolves
        against the in-tree :data:`TOOL_REGISTRY` (no per-test override).
        This is the smoke test that catches builtin-registration drift —
        if a new tool is added to the registry but a wrapper isn't, or
        vice versa, this test will fail.
        """
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            # tool_registry=None → use the real TOOL_REGISTRY.
        )
        worker = factory.build(
            _definition(tools=(tool_name,)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        resolved = worker._nodes[0].tools
        assert resolved is not None and len(resolved) == 1
        assert resolved[0].tool_schema.name == tool_name


@pytest.fixture
def mcp_catalog() -> dict[str, list[McpToolDef]]:
    """A small transport-free MCP catalog for selector-resolution tests.

    ``McpToolDef`` is schema-only (no transport / no ``$VAR`` secrets), so
    constructing these in-process is safe and never opens an MCP server.
    """
    return {"demo": [McpToolDef(name="echo"), McpToolDef(name="ask")]}


class TestMcpToolsWiring:
    """``mcp/...`` selectors in ``definition.tools`` resolve through the
    injected ``mcp_catalog`` into schema-only nodes named ``<server>_<tool>``,
    are concatenated with resolved builtins, and collide-check across the
    combined surface."""

    def test_bare_server_selector_resolves_all_tools(
        self, mcp_catalog: dict[str, list[McpToolDef]]
    ) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={},
            mcp_catalog=mcp_catalog,
        )
        worker = factory.build(
            _definition(tools=("mcp/demo",)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        names = {t.tool_schema.name for t in worker._nodes[0].tools}
        assert names == {"demo_echo", "demo_ask"}

    def test_single_tool_selector_resolves_only_that_tool(
        self, mcp_catalog: dict[str, list[McpToolDef]]
    ) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={},
            mcp_catalog=mcp_catalog,
        )
        worker = factory.build(
            _definition(tools=("mcp/demo/echo",)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        names = {t.tool_schema.name for t in worker._nodes[0].tools}
        assert names == {"demo_echo"}

    def test_unknown_server_raises(
        self, mcp_catalog: dict[str, list[McpToolDef]]
    ) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={},
            mcp_catalog=mcp_catalog,
        )
        with pytest.raises(ValueError, match="unknown server"):
            factory.build(
                _definition(tools=("mcp/ghost",)),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )

    def test_unknown_tool_raises(
        self, mcp_catalog: dict[str, list[McpToolDef]]
    ) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={},
            mcp_catalog=mcp_catalog,
        )
        with pytest.raises(ValueError, match="unknown tool"):
            factory.build(
                _definition(tools=("mcp/demo/missing",)),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )

    def test_builtin_and_mcp_mix_yields_combined_surface(
        self, mcp_catalog: dict[str, list[McpToolDef]]
    ) -> None:
        """A flat ``tools:`` list mixing a builtin and an MCP selector resolves
        to the builtin node followed by the MCP nodes (builtins first, then
        MCP — see ``_resolve_tools``)."""
        _, model_factory = _model_factory_spy()
        fake_calendar = _fake_tool_node("calendar")
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"calendar": fake_calendar},
            mcp_catalog=mcp_catalog,
        )
        worker = factory.build(
            _definition(tools=("calendar", "mcp/demo/echo")),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        names = [t.tool_schema.name for t in worker._nodes[0].tools]
        assert names == ["calendar", "demo_echo"]

    def test_collision_between_builtin_and_mcp_raises(
        self, mcp_catalog: dict[str, list[McpToolDef]]
    ) -> None:
        """A builtin whose schema name equals an MCP selection's flattened
        ``<server>_<tool>`` name (here both ``demo_echo``) is a collision.
        calfkit would silently last-wins on it, so the factory must detect
        and reject it at build time."""
        _, model_factory = _model_factory_spy()
        fake_collider = _fake_tool_node("demo_echo")
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={"demo_echo": fake_collider},
            mcp_catalog=mcp_catalog,
        )
        with pytest.raises(ValueError, match="colliding tool name"):
            factory.build(
                _definition(tools=("demo_echo", "mcp/demo/echo")),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )

    def test_mcp_nodes_carry_original_topics(
        self, mcp_catalog: dict[str, list[McpToolDef]]
    ) -> None:
        """The wired MCP node advertises ``demo_echo`` to the LLM but routes
        on the original-name topics — the agent↔bridge wire contract."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
            tool_registry={},
            mcp_catalog=mcp_catalog,
        )
        worker = factory.build(
            _definition(tools=("mcp/demo/echo",)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        node = next(
            t for t in worker._nodes[0].tools if t.tool_schema.name == "demo_echo"
        )
        assert node.subscribe_topics == ["mcp.demo.echo.input"]
        assert node.publish_topic == "mcp.demo.echo.output"


def _router_definition(
    *,
    agent_id: str = "_router",
    provider: Provider | None = "openai",
    model: str | None = "gpt-5-nano",
    thinking_effort: str | None = "none",
    publish_topic: str = "routing.decisions",
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        display_name="Router",
        description="Internal routing agent",
        provider=provider,
        model=model,
        thinking_effort=thinking_effort,  # type: ignore[arg-type]
        role="router",
        publish_topic=publish_topic,
        system_prompt="You are the routing agent. Pick the right respondents.",
    )


class TestRouterRole:
    """Router-role build path: single fixed topic, no gates, ToolOutput.

    Verifies the factory branches correctly on ``role="router"`` and
    wires the agent with the special-case configuration the routing
    component needs.
    """

    def test_router_builds_without_channels(self) -> None:
        """Routers subscribe to a fixed ambient topic, not per-channel
        topics — so an empty channels list is acceptable."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _router_definition(),
            AgentRuntimeState(channels=[]),
            MagicMock(),
        )
        assert len(worker._nodes) == 1
        assert isinstance(worker._nodes[0], Agent)

    def test_router_subscribes_to_ambient_topic_only(self) -> None:
        """Single subscribe topic: the bridge's ambient ingress.
        No private return topic at [0], no per-agent inbox."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _router_definition(),
            AgentRuntimeState(channels=[]),
            MagicMock(),
        )
        assert worker._nodes[0].subscribe_topics == ["discord.ambient.in"]

    def test_router_has_no_standard_gates(self) -> None:
        """The router is the only consumer of its ingress topic; no
        self-recognition or addressed-to-me checks are needed."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _router_definition(),
            AgentRuntimeState(channels=[]),
            MagicMock(),
        )
        # The assistant path attaches two gates (addressable +
        # addressed_to_me); the router path attaches none.
        assert worker._nodes[0].gates == []

    def test_router_publish_topic_is_set(self) -> None:
        """The router declares its own output topic; the fan-out
        consumer subscribes there. Without publish_topic, the agent's
        ReturnCall would only land on frame.callback_topic — the
        bridge's throwaway topic, which has no consumer."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _router_definition(publish_topic="routing.decisions"),
            AgentRuntimeState(channels=[]),
            MagicMock(),
        )
        assert worker._nodes[0].publish_topic == "routing.decisions"

    def test_router_final_output_type_is_tooloutput_routing_decision(self) -> None:
        """The router's terminal output is a structured RoutingDecision
        emitted via pydantic-ai's ToolOutput pattern, which terminates
        the agent loop in one LLM turn (no second-pass narration).

        The tool ``name`` MUST come from the
        :data:`ROUTER_OUTPUT_TOOL_NAME` constant — the same constant
        the router's system prompt (``router/prompt.py``) interpolates
        into rule 5. A hardcoded ``"dispatch"`` here would still pass
        the prompt-coupling tests today (the literal value is
        unchanged) but the symbolic coupling would silently break: a
        future rename of the constant would skip this site."""
        from calfkit._vendor.pydantic_ai import ToolOutput

        from calfcord.agents.routing import (
            ROUTER_OUTPUT_TOOL_NAME,
            RoutingDecision,
        )

        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _router_definition(),
            AgentRuntimeState(channels=[]),
            MagicMock(),
        )
        final_output_type = worker._nodes[0].final_output_type
        assert isinstance(final_output_type, ToolOutput)
        assert final_output_type.output is RoutingDecision
        # Pin against the constant, not the literal — see docstring above.
        assert final_output_type.name == ROUTER_OUTPUT_TOOL_NAME

    def test_router_model_resolution_uses_definition_fields(self) -> None:
        """Router still uses the provider/model fallback chain. The
        definition's explicit values win."""
        calls, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        factory.build(
            _router_definition(provider="openai", model="gpt-5-nano"),
            AgentRuntimeState(channels=[]),
            MagicMock(),
        )
        assert calls == [("openai", "gpt-5-nano")]


class TestRouterDefinitionValidation:
    """Schema-level invariants on the router definition itself.

    The model_validator on AgentDefinition catches:
        - role=router + tools=... (forbidden)
        - role=router + publish_topic=None (required)
    """

    def test_router_with_tools_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must declare no tools"):
            AgentDefinition(
                agent_id="_router",
                display_name="Router",
                description="x",
                role="router",
                publish_topic="routing.decisions",
                tools=("private_chat",),
                system_prompt="x",
            )

    def test_router_without_publish_topic_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must declare a publish_topic"):
            AgentDefinition(
                agent_id="_router",
                display_name="Router",
                description="x",
                role="router",
                publish_topic=None,
                system_prompt="x",
            )

    def test_assistant_role_has_no_router_constraints(self) -> None:
        """Default role='assistant' allows tools and no publish_topic."""
        AgentDefinition(
            agent_id="scribe",
            display_name="Scribe",
            description="x",
            tools=("private_chat",),
            system_prompt="x",
        )

    def test_assistant_with_publish_topic_raises(self) -> None:
        """Assistants emit ReturnCall to the inbound frame's
        callback_topic; setting ``publish_topic`` on them would be a
        silent no-op. Reject at validation so the misconfiguration is
        visible."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="publish_topic is reserved for routers"):
            AgentDefinition(
                agent_id="scribe",
                display_name="Scribe",
                description="x",
                publish_topic="some.topic",
                system_prompt="x",
            )

    def test_router_with_empty_publish_topic_raises(self) -> None:
        """``min_length=1`` on the field catches the empty-string case
        BEFORE the model_validator runs — pydantic's field validation
        runs first, so the error mentions the field constraint rather
        than the router-specific message."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentDefinition(
                agent_id="_router",
                display_name="Router",
                description="x",
                role="router",
                publish_topic="",
                system_prompt="x",
            )


class TestMemoryFlag:
    """``memory: true`` requires the filesystem tools the memory block tells the
    agent to use; the factory's guard enforces this at build time. These tests
    use the real TOOL_REGISTRY (no override) so read_file/write_file resolve."""

    def test_memory_agent_with_explicit_fs_tools_builds(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _memory_definition(tools=("read_file", "write_file")),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].node_id == "scribe"

    def test_memory_agent_with_all_tools_builds(self) -> None:
        """``tools`` omitted (None) grants every builtin — includes the fs
        tools, so the guard passes."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _memory_definition(tools=None),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].node_id == "scribe"

    def test_memory_true_without_tools_raises(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with pytest.raises(ValueError, match="memory: true"):
            factory.build(
                _memory_definition(tools=()),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )

    def test_memory_true_missing_write_file_raises(self) -> None:
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        with pytest.raises(ValueError, match="write_file"):
            factory.build(
                _memory_definition(tools=("read_file",)),
                AgentRuntimeState(channels=[100]),
                MagicMock(),
            )

    def test_non_memory_agent_unaffected_by_guard(self) -> None:
        """A ``memory=False`` agent with no tools builds fine (guard skipped)."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        worker = factory.build(
            _definition(tools=()),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert worker._nodes[0].system_prompt == "You are a test agent."

    def test_memory_agent_registers_the_instructions_hook(self) -> None:
        """The factory wires the runtime hook onto memory agents — not just the
        guard. Registered dynamic-instructions functions land in pydantic-ai's
        ``_agent_loop._instructions`` (alongside the literal system prompt). Without
        this, the template would reach ``deps`` but never be injected — a silent
        no-op the guard alone can't catch."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        node = factory.build_node(
            _memory_definition(agent_id="scribe", tools=("read_file", "write_file")),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        hooks = [i for i in node._agent_loop._instructions if callable(i)]
        assert len(hooks) == 1, "memory agent should register exactly one instructions hook"
        # The registered hook localizes the bridge-shipped template for THIS agent.
        ctx = SimpleNamespace(deps={MEMORY_PROMPT_DEPS_KEY: "block {{MEMORY_DIR}}"})
        assert hooks[0](ctx) == "block memory/scribe/"

    def test_non_memory_agent_registers_no_instructions_hook(self) -> None:
        """A memory=False agent must NOT carry the hook — only the literal
        system prompt is in ``_instructions``."""
        _, model_factory = _model_factory_spy()
        factory = AgentFactory(
            persona_sender=MagicMock(),
            calfkit_client=MagicMock(),
            model_client_factory=model_factory,
        )
        node = factory.build_node(
            _definition(tools=("read_file",)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        assert [i for i in node._agent_loop._instructions if callable(i)] == []
