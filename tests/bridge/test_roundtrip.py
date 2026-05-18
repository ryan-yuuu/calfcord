"""Unit tests for ``BridgeRoundTrip.handle``.

Mocks the calfkit ``Client.execute_node`` to return a constructed
``NodeResult``, asserts ``DiscordPersonaSender.send`` is (or isn't) called
with the right arguments. No Kafka, no Discord, no LLM.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.client import NodeResult

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.roundtrip import BridgeRoundTrip
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.messages import SentMessage


def _wire() -> WireMessage:
    return WireMessage(
        event_id="evt-1",
        kind="slash",
        slash_target="scheduler",
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content="book me a haircut",
        author=WireAuthor(
            discord_user_id=111,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
            is_human_owner=True,
        ),
        created_at=datetime.now(UTC),
    )


def _registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                slash="/scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar.",
                avatar_url="https://example.com/aksel.png",
                system_prompt="Test scheduler.",
            )
        ]
    )


def _node_result(
    *,
    output: str = "Booked.",
    emitter_node_id: str | None = "scheduler",
    emitter_node_kind: str | None = "agent",
) -> NodeResult[Any]:
    return NodeResult(
        output=output,
        output_parts=[],
        message_history=[],
        metadata=None,
        correlation_id="evt-1",
        emitter_node_id=emitter_node_id,
        emitter_node_kind=emitter_node_kind,
    )


@pytest.fixture
def persona_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=SentMessage(id=99999, channel_id=6789))
    return sender


@pytest.fixture
def client() -> MagicMock:
    """A calfkit Client mock with ``execute_node`` as an AsyncMock by default."""
    c = MagicMock()
    c.execute_node = AsyncMock()
    return c


class TestHappyPath:
    async def test_posts_under_resolved_persona(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())

        persona_sender.send.assert_awaited_once()
        kwargs = persona_sender.send.call_args.kwargs
        assert kwargs["persona"].name == "Aksel (Scheduler)"
        assert kwargs["persona"].avatar_url == "https://example.com/aksel.png"
        assert kwargs["channel_id"] == 6789
        assert kwargs["content"] == "Booked."
        # ReplyContext built from wire — anchored to the inbound message id.
        assert kwargs["reply_to"].message_id == 12345
        assert kwargs["reply_to"].guild_id == 4242

    async def test_invokes_with_in_suffix_topic(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """Ingress topic must use the .in suffix to match the agent's subscribe."""
        client.execute_node.return_value = _node_result()
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())

        kwargs = client.execute_node.call_args.kwargs
        assert kwargs["topic"] == "discord.channel.6789.in"
        assert kwargs["correlation_id"] == "evt-1"
        # The full wire round-trips as a dep so the agent's gate can inspect it.
        assert kwargs["deps"]["discord"]["channel_id"] == 6789
        assert kwargs["deps"]["discord"]["slash_target"] == "scheduler"

    async def test_strips_whitespace_around_output(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        client.execute_node.return_value = _node_result(output="  Booked.\n\n")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())

        assert persona_sender.send.call_args.kwargs["content"] == "Booked."


class TestDropPaths:
    async def test_timeout_drops_silently_with_warning(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        client.execute_node.side_effect = asyncio.TimeoutError()
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()
        assert any("timed out" in r.message for r in caplog.records)

    async def test_non_agent_emitter_kind_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Defense-in-depth: ignore replies that aren't from an agent (e.g.
        accidental client emissions)."""
        client.execute_node.return_value = _node_result(emitter_node_kind="client")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()
        assert any("non-agent emitter" in r.message for r in caplog.records)

    async def test_missing_emitter_id_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        client.execute_node.return_value = _node_result(emitter_node_id=None)
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()

    async def test_unknown_emitter_id_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Agent emitter not in the registry: bug somewhere, but don't crash."""
        client.execute_node.return_value = _node_result(emitter_node_id="ghost")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        with caplog.at_level(logging.WARNING):
            await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()
        assert any("unknown agent emitter" in r.message for r in caplog.records)

    async def test_empty_output_drops(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """Discord rejects empty webhook executes (400); skip the post."""
        client.execute_node.return_value = _node_result(output="   \n  ")
        rt = BridgeRoundTrip(client, _registry(), persona_sender)
        await rt.handle(_wire())
        persona_sender.send.assert_not_awaited()


class TestConcurrency:
    async def test_semaphore_caps_outstanding(
        self,
        client: MagicMock,
        persona_sender: AsyncMock,
    ) -> None:
        """With max_in_flight=2, the third concurrent handle waits until one frees."""
        # Block execute_node on an event so handles park inside the semaphore.
        release = asyncio.Event()
        peak_in_flight = 0
        in_flight = 0
        lock = asyncio.Lock()

        async def slow_execute(*_args: Any, **_kwargs: Any) -> NodeResult[Any]:
            nonlocal peak_in_flight, in_flight
            async with lock:
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
            await release.wait()
            async with lock:
                in_flight -= 1
            return _node_result()

        client.execute_node.side_effect = slow_execute
        rt = BridgeRoundTrip(
            client, _registry(), persona_sender, max_in_flight=2
        )

        tasks = [asyncio.create_task(rt.handle(_wire())) for _ in range(4)]
        # Yield enough times for the semaphore to admit the first two and park
        # the remaining two.
        for _ in range(10):
            await asyncio.sleep(0)
        assert peak_in_flight == 2
        release.set()
        await asyncio.gather(*tasks)
        assert peak_in_flight == 2
