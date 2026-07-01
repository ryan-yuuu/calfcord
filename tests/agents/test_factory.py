"""Unit tests for AgentFactory.

The factory constructs a calfkit ``Worker`` over a single vanilla, **name-
addressed** ``Agent`` node. These tests verify the wiring without invoking a
real LLM: the ``model_client_factory`` constructor argument lets us inject a
fake so no provider client is constructed.

Name-addressing (calfkit 0.12, ADR-0017) means the built agent declares no
channel ``subscribe_topics`` and no addressing gate — it is reached by name on
its automatic private input topic. A2A/handoff reach is declared natively via
``peers`` from the ``a2a``/``handoff`` frontmatter fields.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from calfkit import Handoff, Messaging
from calfkit.nodes import Agent
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfcord.agents.definition import AgentDefinition, Provider
from calfcord.agents.factory import AgentFactory, resolve_provider
from calfcord.agents.memory import MEMORY_PROMPT_DEPS_KEY


def _definition(
    *,
    agent_id: str = "scheduler",
    description: str = "A test agent.",
    provider: Provider | None = None,
    model: str | None = None,
    tools: tuple[str, ...] = (),
    thinking_effort: str | None = None,
    a2a: bool | tuple[str, ...] = True,
    handoff: bool | tuple[str, ...] = True,
) -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        description=description,
        provider=provider,
        model=model,
        tools=tools,
        thinking_effort=thinking_effort,  # type: ignore[arg-type]
        a2a=a2a,
        handoff=handoff,
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


def _factory(**kwargs: Any) -> AgentFactory:
    """Construct an AgentFactory with a spy model-client factory by default."""
    kwargs.setdefault("model_client_factory", _model_factory_spy()[1])
    return AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock(), **kwargs)


def _registered_before_node_seams(node: Agent) -> list[Any]:
    """Return the agent's ``before_node`` seam chain (the 0.12 gate successor).

    A freshly built node only grows the lazy ``_seam_chains`` dict once a seam
    is registered, so an agent with no gate has either no attribute or an empty
    chain — both collapse to ``[]`` here.
    """
    return getattr(node, "_seam_chains", {}).get("before_node", [])


class TestConstruction:
    def test_constructs_with_required_args(self) -> None:
        factory = AgentFactory(persona_sender=MagicMock(), calfkit_client=MagicMock())
        assert factory is not None


class TestBuild:
    def test_returns_worker_with_one_node(self) -> None:
        worker = _factory().build(_definition())
        # Worker stores nodes in ``_nodes`` (internal; verified by reading
        # calfkit/worker/worker.py).
        assert len(worker._nodes) == 1
        assert isinstance(worker._nodes[0], Agent)

    def test_node_name_matches_definition(self) -> None:
        """The agent is addressed by name: ``Agent(name=...)`` -> ``node_id``."""
        worker = _factory().build(_definition(agent_id="scheduler"))
        assert worker._nodes[0].node_id == "scheduler"

    def test_description_is_wired_into_agent(self) -> None:
        """``description=`` must reach the Agent or every AgentCard.description
        is ``None`` and both the mesh roster and the message_agent peer
        directory render blank."""
        node = _factory().build_node(_definition(description="Books and preps meetings"))
        assert node._description == "Books and preps meetings"

    def test_no_channel_subscribe_topics(self) -> None:
        """Name-addressing: the agent declares no channel subscriptions; calfkit
        reaches it on its automatic private input topic."""
        node = _factory().build_node(_definition())
        assert node.subscribe_topics == []

    def test_no_publish_topic_steps_mirror(self) -> None:
        """The old ``publish_topic=AGENT_STEPS_TOPIC`` steps mirror is gone —
        live progress now rides the caller's run stream."""
        node = _factory().build_node(_definition())
        assert node.publish_topic is None

    def test_no_addressing_gates_registered(self) -> None:
        """The addressable / addressed-to-me gates are removed: a name-addressed
        agent registers no ``before_node`` seam."""
        node = _factory().build_node(_definition())
        assert _registered_before_node_seams(node) == []


class TestPeers:
    """``a2a``/``handoff`` frontmatter -> native ``peers`` (Messaging/Handoff)."""

    def test_default_both_a2a_and_handoff_discover(self) -> None:
        """Both fields default ``True`` -> a discovering Messaging + Handoff."""
        node = _factory().build_node(_definition())
        assert node._peers == (Messaging(discover=True), Handoff(discover=True))

    def test_a2a_false_omits_messaging(self) -> None:
        node = _factory().build_node(_definition(a2a=False))
        assert node._peers == (Handoff(discover=True),)

    def test_handoff_false_omits_handoff(self) -> None:
        node = _factory().build_node(_definition(handoff=False))
        assert node._peers == (Messaging(discover=True),)

    def test_both_false_yields_no_peers(self) -> None:
        """No A2A and no handoff -> ``peers=None`` reaches the Agent (empty tuple)."""
        node = _factory().build_node(_definition(a2a=False, handoff=False))
        assert node._peers == ()

    def test_empty_peer_lists_yield_no_peers_without_crashing(self) -> None:
        """`a2a: []` / `handoff: []` normalize to False at the definition layer, so
        the factory builds no peers rather than a bare Messaging()/Handoff() (which
        calfkit rejects — the boot-crash this guards against)."""
        node = _factory().build_node(_definition(a2a=[], handoff=[]))
        assert node._peers == ()

    def test_factory_guard_holds_when_validation_is_bypassed(self) -> None:
        """Defense-in-depth: a definition built bypassing the empty-tuple validator
        (``model_copy`` does not re-validate) keeps ``a2a``/``handoff`` as ``()`` —
        the factory's OWN truthiness guard (not just the definition normalizer)
        must still yield no peers rather than a bare, calfkit-rejected handle."""
        bypassed = _definition().model_copy(update={"a2a": (), "handoff": ()})
        assert bypassed.a2a == () and bypassed.handoff == ()  # bypass confirmed: still empty tuples
        node = _factory().build_node(bypassed)
        assert node._peers == ()

    def test_a2a_list_restricts_to_named_peers(self) -> None:
        node = _factory().build_node(_definition(a2a=("scribe", "researcher")))
        assert Messaging("scribe", "researcher") in node._peers
        # The named-peer Messaging does not discover.
        messaging = next(p for p in node._peers if isinstance(p, Messaging))
        assert messaging.names == ("scribe", "researcher")
        assert messaging.discover is False

    def test_handoff_list_restricts_to_named_targets(self) -> None:
        node = _factory().build_node(_definition(handoff=("scribe",)))
        handoff = next(p for p in node._peers if isinstance(p, Handoff))
        assert handoff.names == ("scribe",)
        assert handoff.discover is False


class TestProviderResolution:
    def test_default_provider_is_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider=None))
        assert calls[0][0] == "anthropic"

    def test_definition_provider_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_provider="anthropic", model_client_factory=model_factory)
        factory.build(_definition(provider="openai", model="gpt-5"))
        assert calls[0][0] == "openai"

    def test_env_provider_used_when_definition_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_provider="anthropic", model_client_factory=model_factory)
        factory.build(_definition(provider=None, model="gpt-5"))
        assert calls[0][0] == "openai"

    def test_ctor_default_used_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_provider="openai", model_client_factory=model_factory)
        factory.build(_definition(provider=None, model="gpt-5"))
        assert calls[0][0] == "openai"

    def test_unknown_env_provider_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var can carry a typo; surface it at build time."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            _factory().build(_definition(provider=None))


class TestModelResolution:
    def test_definition_model_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Definition takes precedence over env var, ctor default, and provider default."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_model="claude-from-ctor", model_client_factory=model_factory)
        factory.build(_definition(model="claude-from-defn"))
        assert calls[0][1] == "claude-from-defn"

    def test_env_var_used_when_definition_model_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_MODEL", "claude-from-env")
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_model="claude-from-ctor", model_client_factory=model_factory)
        factory.build(_definition(model=None))
        assert calls[0][1] == "claude-from-env"

    def test_ctor_default_used_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        factory = _factory(default_model="claude-from-ctor", model_client_factory=model_factory)
        factory.build(_definition(model=None))
        assert calls[0][1] == "claude-from-ctor"

    def test_provider_default_used_as_final_fallback_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without any model hint, anthropic agents fall back to the project's
        default Claude model."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider="anthropic", model=None))
        assert calls[0] == ("anthropic", "claude-sonnet-4-5")

    def test_provider_default_used_as_final_fallback_openai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without any model hint, openai agents fall back to the OpenAI default."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider="openai", model=None))
        assert calls[0] == ("openai", "gpt-5-mini")

    def test_openai_codex_resolves_to_none_when_no_model_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """openai-codex has no static default: with no model hint, ``None`` is
        passed through so the Codex client resolves a live-catalog default."""
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_MODEL", raising=False)
        calls, model_factory = _model_factory_spy()
        _factory(model_client_factory=model_factory).build(_definition(provider="openai-codex", model=None))
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
        worker = _factory().build(_definition(provider="anthropic", thinking_effort="high"))
        agent_loop = worker._nodes[0]._agent_loop  # internal access acceptable in tests
        assert agent_loop.model_settings == {"anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}}

    def test_openai_medium_passes_reasoning_effort(self) -> None:
        worker = _factory().build(_definition(provider="openai", thinking_effort="medium"))
        agent_loop = worker._nodes[0]._agent_loop
        # Matches the operator → OpenAI mapping in
        # :mod:`calfcord.agents.thinking`: operator ``medium`` → OpenAI
        # ``"medium"`` after the ramp shift that accompanied the ``minimal``
        # tier addition.
        assert agent_loop.model_settings == {"openai_reasoning_effort": "medium"}

    def test_no_effort_in_definition_no_model_settings(self) -> None:
        """thinking_effort=None → no tier-2 model_settings."""
        worker = _factory().build(_definition(provider="anthropic"))
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings is None

    def test_effort_none_passes_empty_dict(self) -> None:
        """Explicit "none" → empty dict (calfkit merges as no-op)."""
        worker = _factory().build(_definition(provider="openai", thinking_effort="none"))
        agent_loop = worker._nodes[0]._agent_loop
        assert agent_loop.model_settings == {}


class TestResolveProviderModuleFunction:
    """``resolve_provider`` is lifted to module scope so the bridge can reuse it."""

    def test_definition_provider_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "anthropic")
        assert resolve_provider(_definition(provider="openai"), default_provider="anthropic") == "openai"

    def test_env_var_used_when_definition_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai")
        assert resolve_provider(_definition(provider=None), default_provider="anthropic") == "openai"

    def test_default_used_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        assert resolve_provider(_definition(provider=None), default_provider="openai") == "openai"

    def test_unknown_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "cohere")
        with pytest.raises(ValueError, match="unknown provider 'cohere'"):
            resolve_provider(_definition(provider=None))


def _fake_tool_node(name: str) -> Any:
    """Build a real ``ToolNodeDef`` whose schema name is ``name``.

    calfkit's ``Agent(tools=...)`` flattener (``_flatten_tools``) rejects
    anything that is not a ``ToolNodeDef`` / ``BaseToolNodeSchema``, so a bare
    ``MagicMock`` no longer works as a stand-in.
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
        worker = _factory(tool_registry={}).build(_definition(tools=()))
        assert worker._nodes[0].tools == []

    def test_tools_none_expands_to_every_registered_tool(self) -> None:
        """``definition.tools is None`` means "frontmatter omitted ``tools:``
        line" — the factory expands it to every entry in the tool registry."""
        fake_a = _fake_tool_node("alpha")
        fake_b = _fake_tool_node("beta")
        worker = _factory(tool_registry={"alpha": fake_a, "beta": fake_b}).build(_definition(tools=None))
        # ``_resolve_tools`` returns ``registry.values()`` for ``tools=None``,
        # so the agent's bindings are exactly the registry nodes' bindings, in
        # insertion order (calfkit ≥ 0.9 expands ToolNodeDefs to ToolBindings
        # at Agent construction).
        assert worker._nodes[0].tools == [*fake_a.tool_bindings(), *fake_b.tool_bindings()]

    def test_known_tool_name_is_wired_through_registry(self) -> None:
        """A name listed in ``tools:`` resolves to the registry's ToolNodeDef,
        whose bindings land in ``Agent.tools``."""
        fake_calendar = _fake_tool_node("calendar")
        worker = _factory(tool_registry={"calendar": fake_calendar}).build(_definition(tools=("calendar",)))
        assert worker._nodes[0].tools == list(fake_calendar.tool_bindings())

    def test_unknown_tool_name_raises_with_known_list(self) -> None:
        """Typo in ``.md`` fails at build, listing every unknown plus
        what the registry actually contains so the operator can fix it."""
        factory = _factory(tool_registry={"calendar": _fake_tool_node("calendar")})
        with pytest.raises(ValueError, match="unknown tool"):
            factory.build(_definition(agent_id="scheduler", tools=("calndar",)))

    def test_unknown_tool_error_aggregates_multiple_names(self) -> None:
        """Several typos surface in one message — operator fixes the .md once."""
        factory = _factory(tool_registry={"calendar": _fake_tool_node("calendar")})
        with pytest.raises(ValueError) as excinfo:
            factory.build(_definition(agent_id="scheduler", tools=("calndar", "emial")))
        assert "calndar" in str(excinfo.value)
        assert "emial" in str(excinfo.value)

    def test_mcp_selectors_become_deferred_selectors(self) -> None:
        """``mcp/...`` entries are partitioned out of the builtin resolution
        and land on the agent as per-server deferred selectors (calfkit's
        ``_tool_selectors``) — one per server, with explicit tool picks
        merged into a sorted ``include`` tuple. The deferred side is what
        makes the Worker auto-register the capability view."""
        from calfkit.mcp import MCPToolbox

        fake_shell = _fake_tool_node("shell")
        factory = _factory(tool_registry={"shell": fake_shell})
        worker = factory.build(_definition(tools=("shell", "mcp/gmail/send", "mcp/gmail/search", "mcp/docs")))
        agent = worker._nodes[0]
        assert agent.tools == list(fake_shell.tool_bindings())
        assert agent._tool_selectors == [
            MCPToolbox("docs"),
            MCPToolbox("gmail", include=("search", "send")),
        ]

    def test_build_log_lists_mcp_grants_by_server(self, caplog: pytest.LogCaptureFixture) -> None:
        """The build log enumerates MCP grants as ``mcp:<server>`` — the
        operator-facing record of which servers an agent may reach. The
        label rides on the public ``MCPToolbox.name`` field, so a silent
        upstream rename must fail here, not in production logs."""
        factory = _factory(tool_registry={"shell": _fake_tool_node("shell")})
        with caplog.at_level(logging.INFO, logger="calfcord.agents.factory"):
            factory.build(_definition(tools=("shell", "mcp/gmail/send", "mcp/docs")))
        message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("building agent"))
        assert "mcp:docs" in message
        assert "mcp:gmail" in message

    def test_mcp_only_agent_builds_with_no_builtin_bindings(self) -> None:
        """An agent may declare only MCP tools; it builds with zero static
        bindings and resolves everything per turn from the capability view."""
        worker = _factory(tool_registry={}).build(_definition(tools=("mcp/gmail",)))
        agent = worker._nodes[0]
        assert agent.tools == []
        assert len(agent._tool_selectors) == 1

    def test_tools_none_grants_builtins_only_never_mcp(self) -> None:
        """The "tools omitted -> all tools" default expands to every BUILTIN;
        MCP tools are always explicit, so no selectors appear."""
        fake_a = _fake_tool_node("alpha")
        worker = _factory(tool_registry={"alpha": fake_a}).build(_definition(tools=None))
        assert worker._nodes[0]._tool_selectors == []

    def test_unknown_builtin_error_not_confused_by_mcp_entries(self) -> None:
        """A bogus bare name still raises the aggregate unknown-tool error;
        the co-declared ``mcp/...`` entry is NOT reported as unknown (it is
        not a builtin lookup)."""
        factory = _factory(tool_registry={})
        with pytest.raises(ValueError, match=r"\['definitely_not_real'\]"):
            factory.build(_definition(tools=("definitely_not_real", "mcp/gmail")))

    @pytest.mark.parametrize(
        "tool_name",
        [
            "terminal",
            "process",
            "read_file",
            "write_file",
            "patch",
            "search_files",
            "todo",
            "execute_code",
            "web_search",
            "web_extract",
            "web_fetch",
        ],
    )
    def test_builtin_tool_resolves_through_default_registry(self, tool_name: str) -> None:
        """Each builtin tool name listed in an agent's ``.md`` resolves
        against the in-tree :data:`TOOL_REGISTRY` (no per-test override).
        This is the smoke test that catches builtin-registration drift —
        if a new tool is added to the registry but a wrapper isn't, or
        vice versa, this test will fail.
        """
        # tool_registry=None → use the real TOOL_REGISTRY.
        worker = _factory().build(_definition(tools=(tool_name,)))
        resolved = worker._nodes[0].tools
        assert resolved is not None and len(resolved) == 1
        assert resolved[0].name == tool_name


class TestPublishTopicValidation:
    """A stray ``publish_topic`` on any agent is rejected at validation.

    This exercises :class:`AgentDefinition` validation directly — no factory
    build, no name-addressing. ``publish_topic`` was a reserved field for the
    built-in router (both the field AND its dedicated ``_forbid_publish_topic``
    validator were removed in the 0.12 migration); with no field declared,
    ``model_config extra="forbid"`` now rejects a stale ``publish_topic:`` as an
    unknown field (the ``ValidationError`` still names it), so the
    misconfiguration stays visible without a bespoke validator.
    """

    def test_default_no_publish_topic_builds(self) -> None:
        """A normal agent (no ``publish_topic``) validates and may carry tools."""
        AgentDefinition(
            agent_id="scribe",
            description="x",
            tools=("calendar",),
            system_prompt="x",
        )

    def test_publish_topic_raises(self) -> None:
        """A stale ``publish_topic`` is rejected as an unknown field (extra="forbid")
        so the migration's removal of the field fails loudly, not silently."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="publish_topic"):
            AgentDefinition(
                agent_id="scribe",
                description="x",
                publish_topic="some.topic",
                system_prompt="x",
            )


class TestMemoryFlag:
    """``memory: true`` requires the filesystem tools the memory block tells the
    agent to use; the factory's guard enforces this at build time. These tests
    use the real TOOL_REGISTRY (no override) so read_file/write_file resolve."""

    def test_memory_agent_with_only_mcp_fs_lookalikes_rejected(self) -> None:
        """MCP selectors cannot satisfy the memory guard: their tools resolve
        at runtime, so the factory cannot prove read_file/write_file exist.
        memory: true therefore requires the BUILTIN fs tools explicitly."""
        with pytest.raises(ValueError, match="memory needs read_file and"):
            _factory().build(_memory_definition(tools=("mcp/files",)))

    def test_memory_agent_with_explicit_fs_tools_builds(self) -> None:
        worker = _factory().build(_memory_definition(tools=("read_file", "write_file")))
        assert worker._nodes[0].node_id == "scribe"

    def test_memory_agent_with_all_tools_builds(self) -> None:
        """``tools`` omitted (None) grants every builtin — includes the fs
        tools, so the guard passes."""
        worker = _factory().build(_memory_definition(tools=None))
        assert worker._nodes[0].node_id == "scribe"

    def test_memory_true_without_tools_raises(self) -> None:
        with pytest.raises(ValueError, match="memory: true"):
            _factory().build(_memory_definition(tools=()))

    def test_memory_true_missing_write_file_raises(self) -> None:
        with pytest.raises(ValueError, match="write_file"):
            _factory().build(_memory_definition(tools=("read_file",)))

    def test_non_memory_agent_unaffected_by_guard(self) -> None:
        """A ``memory=False`` agent with no tools builds fine (guard skipped)."""
        worker = _factory().build(_definition(tools=()))
        assert worker._nodes[0].system_prompt == "You are a test agent."

    def test_memory_agent_registers_the_instructions_hook(self) -> None:
        """The factory wires the runtime hook onto memory agents — not just the
        guard. Registered dynamic-instructions functions land in pydantic-ai's
        ``_agent_loop._instructions`` (alongside the literal system prompt). Without
        this, the template would reach ``deps`` but never be injected — a silent
        no-op the guard alone can't catch."""
        node = _factory().build_node(
            _memory_definition(agent_id="scribe", tools=("read_file", "write_file")),
        )
        hooks = [i for i in node._agent_loop._instructions if callable(i)]
        assert len(hooks) == 1, "memory agent should register exactly one instructions hook"
        # The registered hook localizes the bridge-shipped template for THIS agent.
        ctx = SimpleNamespace(deps={MEMORY_PROMPT_DEPS_KEY: "block {{MEMORY_DIR}}"})
        assert hooks[0](ctx) == "block memory/scribe/"

    def test_non_memory_agent_registers_no_instructions_hook(self) -> None:
        """A memory=False agent must NOT carry the hook — only the literal
        system prompt is in ``_instructions``."""
        node = _factory().build_node(_definition(tools=("read_file",)))
        assert [i for i in node._agent_loop._instructions if callable(i)] == []
