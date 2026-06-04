"""Integration tests for the conversation-history wiring inside
:class:`BridgeIngress`.

Exercises the full slash and ambient paths with a real (but mock-backed)
:class:`ChannelHistoryFetcher` injected so the fetcher → projection →
``message_history`` flow is end-to-end-tested without standing up Kafka
or Discord.

These complement the focused unit tests in
:mod:`tests.bridge.test_history` by asserting the *wiring* between
``ingress.handle``, the fetcher, and the consumer-facing ``message_history``
parameter on :meth:`Client.invoke_node`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
)

from calfcord.agents.definition import AgentDefinition
from calfcord.bridge.history import (
    ChannelHistoryFetcher,
    HistoryRecord,
)
from calfcord.bridge.ingress import BridgeIngress
from calfcord.bridge.pending_wires import PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.router.definition import build_router_definition


def _slash_wire(
    *,
    event_id: str = "evt-1",
    slash_target: str = "scribe",
    channel_id: int = 6789,
    source_channel_id: int | None = None,
    message_id: int = 12345,
) -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind="slash",
        slash_target=slash_target,
        message_id=message_id,
        channel_id=channel_id,
        source_channel_id=source_channel_id,
        guild_id=4242,
        content="how do I X?",
        author=WireAuthor(
            discord_user_id=111,
            display_name="ryan",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
            is_human_owner=True,
        ),
        created_at=datetime.now(UTC),
    )


def _ambient_wire(
    *,
    event_id: str = "evt-amb",
    channel_id: int = 6789,
    source_channel_id: int | None = None,
    message_id: int = 99999,
) -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind="message",
        slash_target=None,
        message_id=message_id,
        channel_id=channel_id,
        source_channel_id=source_channel_id,
        guild_id=4242,
        content="just chatting",
        author=WireAuthor(
            discord_user_id=111,
            display_name="ryan",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
            is_human_owner=True,
        ),
        created_at=datetime.now(UTC),
    )


def _registry_with_router(
    *,
    scribe_history_turns: int = 30,
    scheduler_history_turns: int = 30,
) -> AgentRegistry:
    """Production-shaped registry: two assistants + the built-in router."""
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar.",
                provider="anthropic",
                history_turns=scheduler_history_turns,
                system_prompt="Anthropic scheduler.",
            ),
            AgentDefinition(
                agent_id="scribe",
                display_name="Scribe",
                description="Notes.",
                provider="openai",
                history_turns=scribe_history_turns,
                system_prompt="OpenAI scribe.",
            ),
            build_router_definition(),
        ]
    )


def _record(
    *,
    content: str = "hello",
    author_display_name: str = "ryan",
    author_agent_id: str | None = None,
    message_id: int = 1,
) -> HistoryRecord:
    return HistoryRecord(
        message_id=message_id,
        created_at=datetime.now(UTC),
        content=content,
        author_display_name=author_display_name,
        author_agent_id=author_agent_id,
    )


def _fresh_handle() -> Any:
    h = MagicMock()
    h._future = asyncio.Future()
    return h


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    c.invoke_node = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c.reply_topic = "discord.outbox"
    return c


@pytest.fixture
def pending_wires() -> PendingWires:
    return PendingWires()


def _make_fake_fetcher(
    records_to_return: list[HistoryRecord] | None = None,
) -> MagicMock:
    """Fake :class:`ChannelHistoryFetcher` that records its calls.

    Returns a fresh copy of ``records_to_return`` per call so callers
    can't accidentally mutate it across invocations.
    """
    fetcher = MagicMock(spec=ChannelHistoryFetcher)
    fetcher.fetch = AsyncMock(side_effect=lambda **_kw: list(records_to_return or []))
    return fetcher


# ---------------------------------------------------------------------------
# Slash branch — history wiring
# ---------------------------------------------------------------------------


class TestSlashHistoryFetch:
    async def test_fetcher_is_called_with_target_history_turns(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """The fetcher receives the target agent's ``history_turns`` as ``limit``."""
        registry = _registry_with_router(scribe_history_turns=15)
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([])
        ingress.set_fetcher(fetcher)

        await ingress.handle(_slash_wire(slash_target="scribe"))

        fetcher.fetch.assert_awaited_once()
        kw = fetcher.fetch.call_args.kwargs
        assert kw["limit"] == 15

    async def test_fetcher_uses_source_channel_id_when_set(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """source_channel_id (thread id) is preferred over channel_id (parent)."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([])
        ingress.set_fetcher(fetcher)

        wire = _slash_wire(channel_id=200, source_channel_id=500)
        await ingress.handle(wire)

        kw = fetcher.fetch.call_args.kwargs
        assert kw["source_channel_id"] == 500

    async def test_fetcher_falls_back_to_channel_id_when_source_unset(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Backward-compat: source_channel_id=None falls back to channel_id."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([])
        ingress.set_fetcher(fetcher)

        wire = _slash_wire(channel_id=200, source_channel_id=None)
        await ingress.handle(wire)

        kw = fetcher.fetch.call_args.kwargs
        assert kw["source_channel_id"] == 200

    async def test_fetcher_uses_message_id_for_before(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([])
        ingress.set_fetcher(fetcher)

        wire = _slash_wire(message_id=12345)
        await ingress.handle(wire)

        kw = fetcher.fetch.call_args.kwargs
        assert kw["before_message_id"] == 12345

    async def test_history_turns_zero_skips_fetch_entirely(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """An agent with ``history_turns=0`` opts out — no Discord call."""
        registry = _registry_with_router(scribe_history_turns=0)
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([_record()])
        ingress.set_fetcher(fetcher)

        await ingress.handle(_slash_wire(slash_target="scribe"))

        fetcher.fetch.assert_not_called()
        # ...and invoke_node still runs, with empty history.
        kw = client.invoke_node.call_args.kwargs
        assert kw["message_history"] == []

    async def test_pre_ready_fetcher_none_returns_empty_history(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """An event arriving before _on_ready fires must not crash."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        # Deliberately do NOT call set_fetcher.

        await ingress.handle(_slash_wire())

        kw = client.invoke_node.call_args.kwargs
        assert kw["message_history"] == []

    async def test_unknown_target_returns_empty_history(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """If the slash_target isn't in the registry, no fetch + empty history."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([_record()])
        ingress.set_fetcher(fetcher)

        # Build a wire whose slash_target doesn't exist in the registry.
        # We bypass the validator by handcrafting it from a serializable dict.
        wire = WireMessage.model_validate(
            {
                **_slash_wire(slash_target="scribe").model_dump(mode="json"),
                "slash_target": "ghost",
            }
        )
        await ingress.handle(wire)

        fetcher.fetch.assert_not_called()


class TestSlashHistoryProjection:
    async def test_history_passed_to_invoke_node(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher(
            [
                _record(content="hi", author_display_name="ryan"),
                _record(
                    content="hey",
                    author_display_name="Scribe",
                    author_agent_id="scribe",
                ),
            ]
        )
        ingress.set_fetcher(fetcher)

        await ingress.handle(_slash_wire(slash_target="scribe"))

        kw = client.invoke_node.call_args.kwargs
        message_history: list[ModelMessage] = kw["message_history"]
        assert len(message_history) == 2
        # ryan's msg → ModelRequest from scribe's POV.
        assert isinstance(message_history[0], ModelRequest)
        # scribe's prior reply → ModelResponse from scribe's POV.
        assert isinstance(message_history[1], ModelResponse)

    async def test_records_trimmed_to_target_turns(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Fetcher returns 30 records but scribe has history_turns=5."""
        registry = _registry_with_router(scribe_history_turns=5)
        ingress = BridgeIngress(client, registry, pending_wires)
        records = [
            _record(content=f"msg-{i}", message_id=i) for i in range(30)
        ]
        fetcher = _make_fake_fetcher(records)
        ingress.set_fetcher(fetcher)

        await ingress.handle(_slash_wire(slash_target="scribe"))

        kw = client.invoke_node.call_args.kwargs
        message_history = kw["message_history"]
        assert len(message_history) == 5  # trimmed
        # Confirm we kept the MOST RECENT 5 (records[-5:]).
        assert message_history[-1].parts[0].content.endswith("msg-29")


class TestSlashPrefetchedHistory:
    async def test_prefetched_history_skips_fetcher_call(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Synthesized-in path: prefetched records bypass the fetcher entirely."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([_record(content="should not be used")])
        ingress.set_fetcher(fetcher)

        prefetched = [_record(content="from envelope", author_display_name="ryan")]
        await ingress.handle(
            _slash_wire(slash_target="scribe"),
            prefetched_history=prefetched,
        )

        fetcher.fetch.assert_not_called()
        kw = client.invoke_node.call_args.kwargs
        message_history = kw["message_history"]
        assert len(message_history) == 1
        assert "from envelope" in message_history[0].parts[0].content

    async def test_prefetched_empty_tuple_means_no_history(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """An empty tuple from the envelope is "no history", NOT "go fetch"."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher(
            [_record(content="should not be used because prefetched=()")]
        )
        ingress.set_fetcher(fetcher)

        await ingress.handle(_slash_wire(slash_target="scribe"), prefetched_history=())

        fetcher.fetch.assert_not_called()
        kw = client.invoke_node.call_args.kwargs
        assert kw["message_history"] == []


# ---------------------------------------------------------------------------
# Ambient branch — envelope.history
# ---------------------------------------------------------------------------


class TestAmbientHistory:
    async def test_ambient_packs_history_into_envelope(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Raw, unprojected records ride on ``deps["history"]``."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        records = [
            _record(content="hi", author_display_name="ryan"),
            _record(content="hey", author_display_name="bob"),
        ]
        fetcher = _make_fake_fetcher(records)
        ingress.set_fetcher(fetcher)

        await ingress.handle(_ambient_wire())

        kw = client.invoke_node.call_args.kwargs
        deps = kw["deps"]
        assert "history" in deps
        history = deps["history"]
        assert len(history) == 2
        assert history[0]["content"] == "hi"
        assert history[1]["content"] == "hey"

    async def test_ambient_router_history_projects_as_observer(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Router POV: every record becomes ModelRequest (no self)."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        records = [
            _record(content="hi", author_display_name="ryan"),
            _record(
                content="prior",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        fetcher = _make_fake_fetcher(records)
        ingress.set_fetcher(fetcher)

        await ingress.handle(_ambient_wire())

        kw = client.invoke_node.call_args.kwargs
        # message_history is passed straight through to invoke_node.
        for m in kw["message_history"]:
            assert isinstance(m, ModelRequest)

    async def test_ambient_fetches_at_max_history_turns(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """The ambient fetch limit is max(all_agents' history_turns)."""
        registry = _registry_with_router(
            scribe_history_turns=20,
            scheduler_history_turns=40,
        )
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([])
        ingress.set_fetcher(fetcher)

        await ingress.handle(_ambient_wire())

        kw = fetcher.fetch.call_args.kwargs
        # max(20, 40, router=10) == 40
        assert kw["limit"] == 40

    async def test_ambient_skips_fetch_when_all_history_turns_zero(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Edge case: every agent opted out of history → no Discord call."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scribe",
                    display_name="Scribe",
                    description="Notes.",
                    provider="openai",
                    history_turns=0,
                    system_prompt="OpenAI scribe.",
                ),
                # Build a router with history_turns=0 (override via direct construction).
                AgentDefinition(
                    agent_id="_router",
                    display_name="Router",
                    description="Internal routing agent (not user-invocable)",
                    avatar_url=None,
                    provider="openai",
                    model="gpt-5-nano",
                    tools=(),
                    thinking_effort="none",
                    role="router",
                    publish_topic="routing.decisions",
                    history_turns=0,
                    system_prompt="router prompt",
                ),
            ]
        )
        ingress = BridgeIngress(client, registry, pending_wires)
        fetcher = _make_fake_fetcher([])
        ingress.set_fetcher(fetcher)

        await ingress.handle(_ambient_wire())

        fetcher.fetch.assert_not_called()

    async def test_ambient_with_no_fetcher_packs_empty_history(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Pre-ready window for ambient: envelope.history is empty tuple."""
        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        # No set_fetcher call.

        await ingress.handle(_ambient_wire())

        kw = client.invoke_node.call_args.kwargs
        assert kw["deps"]["history"] == []

    async def test_ambient_router_history_trim_keeps_newest(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """The router's message_history is trimmed to its history_turns
        AFTER projection — and must keep the NEWEST entries (slice
        ``[-N:]``), not the oldest. A regression to ``[:N]`` would pass
        every previous test but produce a stale router context.
        """
        registry = _registry_with_router()
        # Build 30 records; router default history_turns is 10.
        records = [
            _record(content=f"msg-{i}", author_display_name="ryan", message_id=i)
            for i in range(30)
        ]
        fetcher = _make_fake_fetcher(records)
        ingress = BridgeIngress(client, registry, pending_wires)
        ingress.set_fetcher(fetcher)

        await ingress.handle(_ambient_wire())

        kw = client.invoke_node.call_args.kwargs
        router_msgs = kw["message_history"]
        assert len(router_msgs) == 10
        # Newest 10 are msg-20 .. msg-29; oldest in the kept slice is msg-20,
        # newest is msg-29. The full envelope still ships all 30 raw records.
        first = router_msgs[0].parts[0].content
        last = router_msgs[-1].parts[0].content
        assert "msg-20" in first
        assert "msg-29" in last


# ---------------------------------------------------------------------------
# /task thread starter — end-to-end through a REAL ChannelHistoryFetcher
# ---------------------------------------------------------------------------
#
# The tests above inject a MagicMock fetcher, so they never exercise the real
# _do_fetch → _thread_starter_message → prepend path. These compose a real
# ChannelHistoryFetcher (backed by lightweight discord fakes) with
# ``ingress.handle`` to prove the /task follow-up scenario works end-to-end:
# the thread's parent-resident starter message is recovered and projected
# into ``message_history`` as the oldest entry.


def _fake_discord_msg(
    *,
    message_id: int,
    content: str,
    author_display_name: str,
    author_id: int,
    webhook_id: int | None = None,
) -> Any:
    """A ``discord.Message`` look-alike for the real fetcher to project."""
    author = SimpleNamespace(
        display_name=author_display_name,
        name=author_display_name,
        id=author_id,
    )
    return SimpleNamespace(
        id=message_id,
        content=content,
        webhook_id=webhook_id,
        author=author,
        created_at=datetime.now(UTC),
    )


class _FakeThreadChannel:
    """A ``discord.Thread`` look-alike: in-thread ``history()`` plus the
    starter-recovery attributes the fetcher duck-types on (``parent_id``,
    ``id``, ``starter_message``, ``parent``)."""

    def __init__(
        self,
        in_thread: list[Any],
        *,
        thread_id: int,
        parent_id: int,
        parent: Any,
    ) -> None:
        self._in_thread = list(in_thread)  # newest-first, like Discord
        self.id = thread_id
        self.parent_id = parent_id
        self.starter_message = None  # force the REST recovery path
        self.parent = parent

    def history(self, *, limit: int, before: Any) -> Any:
        captured = self._in_thread[:limit]

        class _AIter:
            def __init__(self, items: list[Any]) -> None:
                self._items = items
                self._i = 0

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                if self._i >= len(self._items):
                    raise StopAsyncIteration
                v = self._items[self._i]
                self._i += 1
                return v

        return _AIter(captured)


class TestTaskThreadStarterEndToEnd:
    async def test_followup_projects_starter_as_oldest_request(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """A /task follow-up turn: the parent-resident starter is recovered by
        the real fetcher and projected as the oldest ``ModelRequest`` in the
        target agent's ``message_history``."""
        thread_id = 500
        parent_id = 200
        bot_id = 777

        # The /task anchor (starter) lives in the parent; its id == thread id.
        starter = _fake_discord_msg(
            message_id=thread_id,
            content="do the task",
            author_display_name="ryan",
            author_id=111,
        )
        parent = SimpleNamespace(
            fetch_message=AsyncMock(return_value=starter),
        )
        # One prior in-thread reply by the scribe persona (webhook author whose
        # display_name matches the registered agent, so it self-classifies).
        scribe_reply = _fake_discord_msg(
            message_id=9000,
            content="on it",
            author_display_name="Scribe",
            author_id=222,
            webhook_id=333,
        )
        thread = _FakeThreadChannel(
            [scribe_reply],  # newest-first; the triggering msg is excluded
            thread_id=thread_id,
            parent_id=parent_id,
            parent=parent,
        )

        discord_client = MagicMock()
        discord_client.user = SimpleNamespace(id=bot_id)
        discord_client.get_channel.return_value = thread

        registry = _registry_with_router()
        real_fetcher = ChannelHistoryFetcher(discord_client, registry)
        ingress = BridgeIngress(client, registry, pending_wires)
        ingress.set_fetcher(real_fetcher)

        # Slash wire shaped like a /task follow-up: source_channel_id is the
        # thread, message_id is the follow-up trigger (a snowflake > thread id).
        wire = _slash_wire(
            slash_target="scribe",
            channel_id=parent_id,
            source_channel_id=thread_id,
            message_id=12345,
        )
        await ingress.handle(wire)

        # The starter was recovered via the parent REST fetch...
        parent.fetch_message.assert_awaited_once_with(thread_id)
        # ...and projected as the OLDEST entry: a ModelRequest carrying the
        # task statement, ahead of scribe's own prior reply.
        kw = client.invoke_node.call_args.kwargs
        message_history: list[ModelMessage] = kw["message_history"]
        assert len(message_history) == 2
        assert isinstance(message_history[0], ModelRequest)
        assert message_history[0].parts[0].content == "<ryan> do the task"
        assert isinstance(message_history[1], ModelResponse)
        assert message_history[1].parts[0].content == "on it"

    async def test_first_task_turn_excludes_starter(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """On the first /task turn the trigger IS the anchor (message_id ==
        thread id == starter id); it must NOT appear in history (it arrives as
        the user_prompt instead). Empty thread + excluded starter ⇒ empty."""
        thread_id = 500
        parent_id = 200

        starter = _fake_discord_msg(
            message_id=thread_id,
            content="do the task",
            author_display_name="ryan",
            author_id=111,
        )
        parent = SimpleNamespace(fetch_message=AsyncMock(return_value=starter))
        thread = _FakeThreadChannel(
            [],  # brand-new thread: nothing posted inside it yet
            thread_id=thread_id,
            parent_id=parent_id,
            parent=parent,
        )

        discord_client = MagicMock()
        discord_client.user = SimpleNamespace(id=777)
        discord_client.get_channel.return_value = thread

        registry = _registry_with_router()
        ingress = BridgeIngress(client, registry, pending_wires)
        ingress.set_fetcher(ChannelHistoryFetcher(discord_client, registry))

        # First turn: before_message_id == thread id == starter id.
        wire = _slash_wire(
            slash_target="scribe",
            channel_id=parent_id,
            source_channel_id=thread_id,
            message_id=thread_id,
        )
        await ingress.handle(wire)

        kw = client.invoke_node.call_args.kwargs
        assert kw["message_history"] == []
