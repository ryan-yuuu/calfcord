"""Integration tests linking the agent-side wiring with the tool-side runner.

The unit tests in :mod:`tests.tools.builtin.test_private_chat` exercise the tool
body in isolation, and :mod:`tests.agents.test_factory` exercises the
factory's tool-resolution path against a fake registry. These tests
connect the two: an agent declared with ``tools: [private_chat]`` must
end up holding the *same* :class:`ToolNodeDef` object that the
``calfkit-tools`` runner would mount on its Worker. Any drift between
the two views — different topics, different schemas — would silently
break A2A in production.

Also pins the wire-convention round-trip: the dict shape the bridge
serializes into ``deps`` must validate as a phonebook on the tool side.
A renamed PhonebookEntry field would silently break A2A in production
while passing both the bridge-side and tool-side unit tests in
isolation (each builds its own dict shape).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.agents.state import AgentRuntimeState
from calfkit_organization.bridge.ingress import BridgeIngress
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.tools import TOOL_REGISTRY
from calfkit_organization.tools.builtin.private_chat import private_chat_tool


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


class TestWireConventionRoundTrip:
    """Pin the cross-deployment dict shape: what the bridge writes into
    ``deps["phonebook"]`` must validate as a phonebook on the tool side.

    Bridge-side unit tests assert on dict keys (``e["agent_id"]``); tool-side
    unit tests build their own dicts via ``phonebook_to_deps``. A renamed
    PhonebookEntry field (e.g. ``display_name`` → ``name``) breaks A2A in
    production but passes both isolated test surfaces — only this seam test
    exercises the actual handoff.
    """

    async def test_phonebook_serialized_by_bridge_validates_at_tool(
        self,
    ) -> None:
        from calfkit_organization.agents.phonebook import phonebook_from_deps
        from calfkit_organization.tools.builtin import private_chat as pc

        # Stub calfkit Client to capture the deps the bridge writes —
        # no Kafka, no real publish. Reuses the same fixture pattern as
        # tests/bridge/test_ingress.py.
        client = MagicMock()
        handle = MagicMock()
        handle._future = asyncio.get_event_loop().create_future()
        client.invoke_node = AsyncMock(return_value=handle)

        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="alice",
                    slash="/alice",
                    display_name="Alice Bot",
                    description="Scheduler.",
                    avatar_url="https://example.com/a.png",
                    tools=("private_chat",),
                    history_turns=15,
                    system_prompt="x",
                ),
                AgentDefinition(
                    agent_id="bob",
                    slash="/bob",
                    display_name="Bob Bot",
                    description="Note-taker.",
                    tools=(),
                    history_turns=42,
                    system_prompt="y",
                ),
            ]
        )
        wire = WireMessage(
            event_id="evt-1",
            kind="slash",
            slash_target="alice",
            message_id=1,
            channel_id=2,
            guild_id=3,
            content="hi",
            author=WireAuthor(
                discord_user_id=4,
                display_name="ryan",
                is_bot=False,
                is_webhook=False,
            ),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        ingress = BridgeIngress(client, registry, PendingWires())
        await ingress.handle(wire)

        # What the bridge actually published in deps.
        serialized = client.invoke_node.call_args.kwargs["deps"]["phonebook"]

        # The tool consumes this on the other side. The full round-trip
        # must succeed AND preserve every field the tool reads.
        parsed = phonebook_from_deps(serialized)
        ids = {e.agent_id for e in parsed}
        assert ids == {"alice", "bob"}

        alice = next(e for e in parsed if e.agent_id == "alice")
        assert alice.display_name == "Alice Bot"
        assert alice.avatar_url == "https://example.com/a.png"
        assert alice.description == "Scheduler."
        assert alice.tools == ("private_chat",)
        # ``history_turns`` must ride along so the tool's continue-thread
        # branch can use the target's configured budget when fetching
        # prior turns. A missing field would silently fall back to the
        # PhonebookEntry default (30) — invisible drift from the
        # operator's frontmatter.
        assert alice.history_turns == 15

        # The tool's ``_lookup`` helper would scan the same parsed list.
        looked_up = pc._lookup(parsed, "bob")
        assert looked_up is not None
        assert looked_up.display_name == "Bob Bot"
        assert looked_up.history_turns == 42
