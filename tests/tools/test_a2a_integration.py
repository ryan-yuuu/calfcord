"""Integration tests linking the agent-side wiring with the tool-side runner.

The unit tests in :mod:`tests.tools.test_private_chat` exercise the tool
body in isolation, and :mod:`tests.agents.test_factory` exercises the
factory's tool-resolution path against a fake registry. These tests
connect the two: an agent declared with ``tools: [private_chat]`` must
end up holding the *same* :class:`ToolNodeDef` object that the
``calfkit-tools`` runner would mount on its Worker. Any drift between
the two views — different topics, different schemas — would silently
break A2A in production.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.agents.state import AgentRuntimeState
from calfkit_organization.tools import TOOL_REGISTRY
from calfkit_organization.tools.private_chat import private_chat_tool


def _definition(tools: tuple[str, ...]) -> AgentDefinition:
    return AgentDefinition(
        agent_id="scheduler",
        slash="/scheduler",
        display_name="Scheduler Bot",
        description="test",
        tools=tools,
        system_prompt="You are scheduler.",
    )


def _factory() -> AgentFactory:
    """Factory with a fake model client so we don't touch real providers."""

    def _model_factory(provider, model_name):  # type: ignore[no-untyped-def]
        return MagicMock(spec=PydanticModelClient)

    return AgentFactory(
        persona_sender=MagicMock(),
        calfkit_client=MagicMock(),
        model_client_factory=_model_factory,
    )


class TestAgentToToolRegistryConsistency:
    """Wires the registry-from-factory path end-to-end."""

    def test_registry_contains_private_chat(self) -> None:
        """If the runner reads TOOL_REGISTRY at boot, private_chat must
        already be registered by import side-effect."""
        assert "private_chat" in TOOL_REGISTRY

    def test_agent_with_private_chat_uses_same_node_object_as_runner(self) -> None:
        """The factory should pull the SAME ToolNodeDef object that the
        runner would mount — same subscribe topic, same schema. Object
        identity catches accidental duplication (e.g. someone defines a
        second private_chat fixture in tests)."""
        worker = _factory().build(
            _definition(tools=("private_chat",)),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        agent_tools = worker._nodes[0].tools
        assert agent_tools == [private_chat_tool]
        assert agent_tools[0] is private_chat_tool

    def test_private_chat_advertises_expected_schema_name(self) -> None:
        """LLM tool advertising uses the schema name. If this drifts from
        the wire convention ("private_chat"), agents' tool calls would
        emit the wrong tool name and route nowhere."""
        assert private_chat_tool.tool_schema.name == "private_chat"

    def test_private_chat_subscribe_topic_matches_runner_convention(self) -> None:
        """The runner mounts the tool on its own subscribe topic. Calfkit's
        @agent_tool decorator derives this from the function name, and the
        ``tool.private_chat.input`` form is what calfkit's Agent.run uses
        when emitting a Call. Drift here would route to a topic the
        runner doesn't consume."""
        assert private_chat_tool.subscribe_topics == ["tool.private_chat.input"]

    def test_agent_inbox_subscribe_topic_matches_tool_publish_target(
        self,
    ) -> None:
        """The tool publishes the A2A invocation to
        ``agent.{target_agent_id}.in``; the factory must subscribe each
        agent to that exact topic. Mismatch = silent A2A breakage."""
        worker = _factory().build(
            _definition(tools=()),
            AgentRuntimeState(channels=[100]),
            MagicMock(),
        )
        topics = worker._nodes[0].subscribe_topics
        # Last topic is the per-agent inbox (the factory appends it after
        # channel topics).
        assert topics[-1] == "agent.scheduler.in"
