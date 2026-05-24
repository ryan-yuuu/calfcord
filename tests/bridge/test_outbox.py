"""Unit tests for the outbox consumer built by ``build_outbox_consumer``.

Drives ``ConsumerNodeDef.handler`` directly with synthetic ``Envelope``s
so we exercise the gate, the dep lookup against ``PendingWires``, the
emitter checks, and the persona send — all without Kafka, FastStream,
discord.py, or an LLM.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from calfkit.models import State, TextPart
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    Deps,
    SessionRunContext,
    WorkflowState,
)

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.outbox import build_outbox_consumer
from calfkit_organization.bridge.pending_wires import PendingWires, make_pending_entry
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.messages import SentMessage


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    """Build a discord HTTPException-family instance without hitting the network."""
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


_CORRELATION_ID = "evt-1"


def _wire() -> WireMessage:
    return WireMessage(
        event_id=_CORRELATION_ID,
        kind="message",
        slash_target=None,
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content="hello",
        author=WireAuthor(
            discord_user_id=111,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
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
            ),
        ]
    )


def _envelope(
    *,
    correlation_id: str = _CORRELATION_ID,
    final_text: str | None = "Booked.",
    deps_provided: dict[str, Any] | None = None,
) -> Envelope:
    """Build a synthetic envelope mimicking an agent's ``ReturnCall`` publish.

    ``final_text=None`` produces an envelope with no ``final_output_parts``
    (an intermediate hop). Anything else is wrapped in a single ``TextPart``.
    """
    state = State()
    if final_text is not None:
        state.final_output_parts = [TextPart(text=final_text)]
    call_stack = CallFrameStack()
    # The consumer doesn't read the call stack, but ``Envelope`` requires
    # a non-empty ``WorkflowState`` to validate.
    call_stack.push(
        CallFrame(
            target_topic="discord.outbox",
            callback_topic="discord.outbox",
        )
    )
    return Envelope(
        internal_workflow_state=WorkflowState(call_stack=call_stack),
        context=SessionRunContext(
            state=state,
            deps=Deps(
                correlation_id=correlation_id,
                provided_deps=deps_provided or {},
            ),
        ),
    )


def _headers(
    *,
    emitter: str | None = "scheduler",
    emitter_kind: str | None = "agent",
) -> dict[str, Any]:
    h: dict[str, Any] = {}
    if emitter is not None:
        h["x-calf-emitter"] = emitter
    if emitter_kind is not None:
        h["x-calf-emitter-kind"] = emitter_kind
    return h


@pytest.fixture
def persona_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=SentMessage(id=99999, channel_id=6789))
    return sender


@pytest.fixture
def pending_wires() -> PendingWires:
    pw = PendingWires()
    pw.put(_CORRELATION_ID, make_pending_entry(_wire()))
    return pw


@pytest.fixture
def calfkit_client() -> MagicMock:
    """Fake calfkit Client for the outbox's retry-publish path.

    ``invoke_node`` returns an ``InvocationHandle``-shaped mock whose
    ``_future`` is a real ``asyncio.Future``; the outbox cancels this
    future after publishing (fire-and-forget pattern matches
    :class:`BridgeIngress`), so it must be cancellable to avoid
    ``RuntimeWarning: coroutine never awaited``.
    """
    import asyncio as _asyncio

    def _make_handle(*_a: Any, **_kw: Any) -> MagicMock:
        h = MagicMock()
        h._future = _asyncio.get_event_loop().create_future()
        return h

    c = MagicMock()
    c.invoke_node = AsyncMock(side_effect=_make_handle)
    return c


@pytest.fixture
def broker() -> MagicMock:
    return MagicMock()


class TestHappyPath:
    async def test_posts_under_resolved_persona(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        persona_sender.send.assert_awaited_once()
        kwargs = persona_sender.send.call_args.kwargs
        assert kwargs["persona"].name == "Aksel (Scheduler)"
        assert kwargs["persona"].avatar_url == "https://example.com/aksel.png"
        assert kwargs["channel_id"] == 6789
        assert kwargs["content"] == "Booked."
        # ReplyContext built from wire — anchored to the inbound message id.
        assert kwargs["reply_to"].message_id == 12345
        assert kwargs["reply_to"].guild_id == 4242

    async def test_strips_whitespace_around_output(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        await consumer.handler(
            envelope=_envelope(final_text="  Booked.\n\n"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert persona_sender.send.call_args.kwargs["content"] == "Booked."

    async def test_multiple_agents_reuse_the_same_wire(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        """Two agents reply for the same correlation_id; both post."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    slash="/scheduler",
                    display_name="Aksel",
                    description="Calendar.",
                    avatar_url="https://example.com/aksel.png",
                    system_prompt="A.",
                ),
                AgentDefinition(
                    agent_id="finance",
                    slash="/finance",
                    display_name="Finn",
                    description="Bookkeeping.",
                    avatar_url="https://example.com/finn.png",
                    system_prompt="B.",
                ),
            ]
        )
        consumer = build_outbox_consumer(persona_sender, registry, pending_wires, calfkit_client)

        await consumer.handler(
            envelope=_envelope(final_text="A1"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="scheduler"),
            broker=broker,
        )
        await consumer.handler(
            envelope=_envelope(final_text="B1"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter="finance"),
            broker=broker,
        )

        assert persona_sender.send.await_count == 2
        names = [c.kwargs["persona"].name for c in persona_sender.send.call_args_list]
        assert names == ["Aksel", "Finn"]


class TestDropPaths:
    async def test_intermediate_hop_gate_rejects(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        """Envelope with empty ``final_output_parts`` is an intermediate hop — skip."""
        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        await consumer.handler(
            envelope=_envelope(final_text=None),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.send.assert_not_awaited()

    async def test_wire_missing_drops_silently(
        self,
        persona_sender: AsyncMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
            calfkit_client: MagicMock,
    ) -> None:
        """Foreign producer / pre-restart event with no wire in the map."""
        empty_pw = PendingWires()
        consumer = build_outbox_consumer(persona_sender, _registry(), empty_pw, calfkit_client)
        with caplog.at_level(logging.DEBUG):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        persona_sender.send.assert_not_awaited()
        assert any("no pending entry" in r.message for r in caplog.records)

    async def test_non_agent_emitter_kind_drops(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
            calfkit_client: MagicMock,
    ) -> None:
        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(emitter_kind="client"),
                broker=broker,
            )
        persona_sender.send.assert_not_awaited()
        assert any("non-agent emitter" in r.message for r in caplog.records)

    async def test_missing_emitter_id_drops(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(emitter=None),
            broker=broker,
        )
        persona_sender.send.assert_not_awaited()

    async def test_unknown_emitter_id_drops(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
            calfkit_client: MagicMock,
    ) -> None:
        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(emitter="ghost"),
                broker=broker,
            )
        persona_sender.send.assert_not_awaited()
        assert any("unknown agent emitter" in r.message for r in caplog.records)

    async def test_empty_output_drops(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        """Discord rejects empty webhook executes; skip the post."""
        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        await consumer.handler(
            envelope=_envelope(final_text="   \n  "),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        persona_sender.send.assert_not_awaited()


class TestDiscordErrorHandling:
    """The outbox runs with ``re_raise=False``; a bubbled HTTPException would
    be logged at ERROR by calfkit with a full stack trace, drowning out the
    operationally-useful "channel is missing permissions" signal. The
    consumer catches HTTPException explicitly and logs structurally."""

    @pytest.fixture(autouse=True)
    def _no_retry_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Skip the 2s retry delay in tests."""
        monkeypatch.setattr(
            "calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS",
            0,
        )

    async def test_forbidden_drops_without_retry(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
            calfkit_client: MagicMock,
    ) -> None:
        """Forbidden = bot lost Manage Webhooks; retry won't help."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))

        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        assert persona_sender.send.await_count == 1
        assert any("Manage Webhooks" in r.message for r in caplog.records)

    async def test_not_found_drops_without_retry(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
            calfkit_client: MagicMock,
    ) -> None:
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.NotFound, 404))

        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        assert persona_sender.send.await_count == 1
        assert any("channel exists" in r.message for r in caplog.records)

    async def test_server_error_retries_once_and_succeeds(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        """First call hits 5xx; retry returns a SentMessage."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.DiscordServerError, 503),
                SentMessage(id=99999, channel_id=6789),
            ]
        )

        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        assert persona_sender.send.await_count == 2

    async def test_server_error_retries_once_then_gives_up(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
            calfkit_client: MagicMock,
    ) -> None:
        """Both attempts hit 5xx; one warning per attempt, no exception out."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.DiscordServerError, 503),
                _http_exc(discord.DiscordServerError, 503),
            ]
        )

        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        assert persona_sender.send.await_count == 2
        # Two log lines expected: the 5xx-retry-warn from
        # _send_with_one_retry_on_outage, and the final-5xx-exhausted
        # log from _handle_post_failure.
        assert any(
            "retrying once" in r.message and "5xx" in r.message
            for r in caplog.records
        )
        assert any(
            "5xx + extra retry exhausted" in r.message
            for r in caplog.records
        )

    async def test_retry_surfacing_forbidden_keeps_actionable_log(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
            calfkit_client: MagicMock,
    ) -> None:
        """First attempt 5xx, second attempt 403 — operator-actionable
        Manage-Webhooks language is preserved (now emitted by
        :func:`_handle_post_failure` rather than the sender wrapper)."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.DiscordServerError, 503),
                _http_exc(discord.Forbidden, 403),
            ]
        )

        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        assert persona_sender.send.await_count == 2
        assert any(
            "forbidden" in r.message and "Manage Webhooks" in r.message
            for r in caplog.records
        )

    async def test_other_http_exception_drops_without_retry(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
            calfkit_client: MagicMock,
    ) -> None:
        """An odd 4xx (e.g. 400 bad request) is not retried."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=_http_exc(discord.HTTPException, 400)
        )

        consumer = build_outbox_consumer(persona_sender, _registry(), pending_wires, calfkit_client)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        assert persona_sender.send.await_count == 1
