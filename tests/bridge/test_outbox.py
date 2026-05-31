"""Unit tests for the outbox consumer built by ``build_outbox_consumer``.

Drives ``ConsumerNodeDef.handler`` directly with synthetic ``Envelope``s
so we exercise the gate, the dep lookup against ``PendingWires``, the
emitter checks, and the persona send — all without Kafka, FastStream,
discord.py, or an LLM.
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from calfkit._vendor.pydantic_ai.messages import TextPart as PaiTextPart
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
from calfkit_organization.bridge.steps_toggle import _TOGGLE_CUSTOM_ID
from calfkit_organization.bridge.transcripts import (
    NullTranscriptStore,
    TranscriptRow,
    TranscriptStore,
)
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.messages import SentMessage


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    """Build a discord HTTPException-family instance without hitting the network."""
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


class _SpyNullStore(NullTranscriptStore):
    """A ``NullTranscriptStore`` that records its ``write_turn`` calls.

    Lets a test assert the outbox NEVER attempts a write against a disabled
    store — a precise check the no-op ``write_turn`` itself can't surface
    (it returns ``None`` whether called or not). Behaviour is otherwise
    identical to the base Null store (every read still misses, ``enabled``
    stays ``False``)."""

    def __init__(self) -> None:
        self.write_calls: list[TranscriptRow] = []

    async def write_turn(self, row: TranscriptRow) -> None:
        self.write_calls.append(row)
        return await super().write_turn(row)


_CORRELATION_ID = "evt-1"


def _wire(*, source_channel_id: int | None = None) -> WireMessage:
    return WireMessage(
        event_id=_CORRELATION_ID,
        kind="message",
        slash_target=None,
        message_id=12345,
        channel_id=6789,
        source_channel_id=source_channel_id,
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
    message_history: list[Any] | None = None,
) -> Envelope:
    """Build a synthetic envelope mimicking an agent's ``ReturnCall`` publish.

    ``final_text=None`` produces an envelope with no ``final_output_parts``
    (an intermediate hop). Anything else is wrapped in a single ``TextPart``.

    ``message_history`` seeds ``state.message_history`` (the cumulative
    pydantic_ai conversation the outbox slices for the turn's transcript).
    Default ``None`` leaves it empty — the pure-text/no-toggle behavior the
    pre-existing tests rely on.
    """
    state = State()
    if message_history is not None:
        state.message_history = list(message_history)
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


@pytest.fixture
async def transcript_store(tmp_path: pathlib.Path) -> AsyncIterator[TranscriptStore]:
    """A real (tmp-path) transcript store so tests can assert the outbox's
    SOLE-writer behavior end-to-end: the consumer writes a row, the test
    reads it back by ``final_message_id``. The toggle is verified separately
    via the ``extra_buttons`` passed to ``persona_sender.send``."""
    store = TranscriptStore(tmp_path / "state" / "transcripts.sqlite3")
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


class TestHappyPath:
    async def test_posts_under_resolved_persona(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        """Two agents reply for the same correlation_id; both post."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    display_name="Aksel",
                    description="Calendar.",
                    avatar_url="https://example.com/aksel.png",
                    system_prompt="A.",
                ),
                AgentDefinition(
                    agent_id="finance",
                    display_name="Finn",
                    description="Bookkeeping.",
                    avatar_url="https://example.com/finn.png",
                    system_prompt="B.",
                ),
            ]
        )
        consumer = build_outbox_consumer(
            persona_sender, registry, pending_wires, calfkit_client, transcript_store=transcript_store
        )

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


class TestThreadRouting:
    """Replies post into the originating thread when the wire came from one.

    A wire whose ``source_channel_id`` differs from the flattened parent
    ``channel_id`` originated in a thread, so the reply must post into that
    thread (``thread_id=``) while the persona webhook still hosts on the
    parent channel.
    """

    _THREAD_ID = 555

    async def test_main_reply_posts_into_thread(
        self,
        persona_sender: AsyncMock,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        pw = PendingWires()
        pw.put(_CORRELATION_ID, make_pending_entry(_wire(source_channel_id=self._THREAD_ID)))
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pw, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(), correlation_id=_CORRELATION_ID, headers=_headers(), broker=broker
        )
        kwargs = persona_sender.send.call_args.kwargs
        # Webhook hosts on the parent; thread_id routes the post into the thread.
        assert kwargs["channel_id"] == 6789
        assert kwargs["thread_id"] == self._THREAD_ID
        # Jump link anchors to the in-thread message (the source channel).
        assert kwargs["reply_to"].channel_id == self._THREAD_ID

    async def test_main_reply_posts_to_parent_when_not_a_thread(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        # The default fixture wire has source_channel_id=None ⇒ top-level.
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(), correlation_id=_CORRELATION_ID, headers=_headers(), broker=broker
        )
        kwargs = persona_sender.send.call_args.kwargs
        assert kwargs["channel_id"] == 6789
        assert kwargs["thread_id"] is None
        assert kwargs["reply_to"].channel_id == 6789

    async def test_chunked_fallback_posts_every_chunk_into_thread(
        self,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The last-resort chunked fallback also targets the thread: the
        failed reply send and every chunk send carry ``thread_id``."""
        monkeypatch.setattr("calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        from calfkit_organization.discord.retry_feedback import MAX_REPLY_RETRY_ATTEMPTS

        pw = PendingWires()
        pw.put(_CORRELATION_ID, make_pending_entry(_wire(source_channel_id=self._THREAD_ID)))
        # Exhaust the agent-retry budget so an agent-fixable 400 falls
        # straight through to the chunk-split fallback.
        for _ in range(MAX_REPLY_RETRY_ATTEMPTS):
            pw.increment_retry(_CORRELATION_ID)

        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 400),  # the reply fails agent-fixably
                SentMessage(id=70001, channel_id=6789),  # chunk 1
                SentMessage(id=70002, channel_id=6789),  # chunk 2
            ]
        )
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pw, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(final_text="x" * 3000),  # forces ≥2 chunks
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert persona_sender.send.await_count == 3
        for call in persona_sender.send.call_args_list:
            assert call.kwargs["channel_id"] == 6789
            assert call.kwargs["thread_id"] == self._THREAD_ID


class TestDropPaths:
    async def test_intermediate_hop_gate_rejects(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        """Envelope with empty ``final_output_parts`` is an intermediate hop — skip."""
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        """Foreign producer / pre-restart event with no wire in the map."""
        empty_pw = PendingWires()
        consumer = build_outbox_consumer(
            persona_sender, _registry(), empty_pw, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        """Discord rejects empty webhook executes; skip the post."""
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        """Forbidden = bot lost Manage Webhooks; retry won't help."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.NotFound, 404))

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        """First call hits 5xx; retry returns a SentMessage."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.DiscordServerError, 503),
                SentMessage(id=99999, channel_id=6789),
            ]
        )

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        transcript_store: TranscriptStore,
    ) -> None:
        """Both attempts hit 5xx; one warning per attempt, no exception out."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.DiscordServerError, 503),
                _http_exc(discord.DiscordServerError, 503),
            ]
        )

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
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
        assert any("retrying once" in r.message and "5xx" in r.message for r in caplog.records)
        assert any("5xx + extra retry exhausted" in r.message for r in caplog.records)

    async def test_retry_surfacing_forbidden_keeps_actionable_log(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
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

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        assert persona_sender.send.await_count == 2
        assert any("forbidden" in r.message and "Manage Webhooks" in r.message for r in caplog.records)

    async def test_other_http_exception_drops_without_retry(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        """An odd 4xx (e.g. 400 bad request) is not retried."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.HTTPException, 400))

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        assert persona_sender.send.await_count == 1


def _tool_using_history() -> list[Any]:
    """A cumulative terminal ``message_history`` where the turn used a tool.

    ``[initial_len=0 : -1]`` (the slice the outbox renders) is the first
    three messages — a ``UserPromptPart`` (not rendered) plus a
    ``ToolCallPart`` and its ``ToolReturnPart``, which render as ONE tree
    block → 1 rendered step. The trailing ``ModelResponse`` is the final
    answer the outbox posts to the channel (dropped by the ``[:-1]`` slice).
    """
    return [
        ModelRequest(parts=[UserPromptPart(content="weather in tokyo?")]),
        ModelResponse(parts=[ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1")]),
        ModelResponse(parts=[PaiTextPart(content="It's 18 degrees in Tokyo.")]),
    ]


class TestTranscriptAndToggle:
    """The outbox is the SOLE transcript writer: a tool-using terminal reply
    writes a row AND attaches the expand toggle; a pure-text reply does
    neither; the chunked fallback writes the row against the first chunk."""

    async def test_tool_using_reply_writes_row_and_attaches_toggle(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(
                final_text="It's 18 degrees in Tokyo.",
                message_history=_tool_using_history(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # Toggle attached to the reply: one secondary button carrying the
        # static toggle custom_id, labelled with the 1-step count (the call
        # and its result render as one tree block).
        persona_sender.send.assert_awaited_once()
        buttons = persona_sender.send.call_args.kwargs["extra_buttons"]
        assert buttons is not None
        assert len(buttons) == 1
        assert buttons[0].custom_id == _TOGGLE_CUSTOM_ID
        assert buttons[0].label == "⤵ 1 step"

        # Transcript row written against the posted reply id (99999), keyed
        # by correlation_id, with the round-trippable delta slice.
        row = await transcript_store.get_by_final_message_id("99999")
        assert row is not None
        assert row.correlation_id == _CORRELATION_ID
        assert row.agent_id == "scheduler"
        assert row.conversation_key == "6789"  # wire.channel_id (no thread)
        # The persisted delta deserializes back to the 3-message slice.
        messages = ModelMessagesTypeAdapter.validate_json(row.delta_json)
        assert len(messages) == 3

    async def test_pure_text_reply_writes_no_row_and_no_toggle(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        # A pure-text turn: history is just the user prompt + the final
        # answer, so the [initial_len:-1] slice renders to zero steps.
        history = [
            ModelRequest(parts=[UserPromptPart(content="hi")]),
            ModelResponse(parts=[PaiTextPart(content="Hello there.")]),
        ]
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(final_text="Hello there.", message_history=history),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # No toggle, no row.
        persona_sender.send.assert_awaited_once()
        assert persona_sender.send.call_args.kwargs["extra_buttons"] is None
        assert await transcript_store.get_by_final_message_id("99999") is None

    async def test_chunked_fallback_writes_row_with_first_chunk_id(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When agent retries are exhausted, the chunked fallback posts the
        content and writes the transcript against the FIRST chunk's id, with
        the toggle on that first chunk only."""
        monkeypatch.setattr("calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        # Drive straight to the chunk-split branch: exhaust the agent-retry
        # budget so an agent-fixable 400 falls back to chunking.
        from calfkit_organization.discord.retry_feedback import MAX_REPLY_RETRY_ATTEMPTS

        for _ in range(MAX_REPLY_RETRY_ATTEMPTS):
            pending_wires.increment_retry(_CORRELATION_ID)

        persona_sender = AsyncMock()
        first_chunk = SentMessage(id=70001, channel_id=6789)
        rest_chunk = SentMessage(id=70002, channel_id=6789)
        # First send (the reply) fails agent-fixably; subsequent sends are
        # the chunk-split posts, which succeed.
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 400),
                first_chunk,
                rest_chunk,
            ]
        )

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(
                final_text="x" * 3000,  # forces ≥2 chunks
                message_history=_tool_using_history(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # Row written against the FIRST chunk's id, not the second.
        assert await transcript_store.get_by_final_message_id("70001") is not None
        assert await transcript_store.get_by_final_message_id("70002") is None

        # Toggle rode the first chunk only.
        chunk_calls = persona_sender.send.call_args_list[1:]
        assert chunk_calls[0].kwargs["extra_buttons"] is not None
        assert chunk_calls[0].kwargs["extra_buttons"][0].custom_id == _TOGGLE_CUSTOM_ID
        assert chunk_calls[1].kwargs["extra_buttons"] is None

    async def test_disabled_store_attaches_no_toggle_and_writes_no_row(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
    ) -> None:
        """A failed-open store degrades to a ``NullTranscriptStore``
        (``enabled=False``). A tool-using terminal reply must then attach NO
        toggle and write NO row — users never get a dead button with no row
        behind it. The reply itself still posts."""
        # Spy on write_turn so we assert the outbox NEVER ATTEMPTS a write
        # against a disabled store — stronger than checking that a read
        # misses afterwards (a no-op write would also leave the read empty).
        null_store = _SpyNullStore()
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=null_store
        )
        await consumer.handler(
            envelope=_envelope(
                final_text="It's 18 degrees in Tokyo.",
                message_history=_tool_using_history(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # Reply posted, but with no toggle (extra_buttons is None) despite
        # the turn using tools.
        persona_sender.send.assert_awaited_once()
        assert persona_sender.send.call_args.kwargs["extra_buttons"] is None
        # The outbox never called write_turn on the disabled store.
        assert null_store.write_calls == []

    async def test_chunked_fallback_disabled_store_attaches_no_toggle_and_writes_no_row(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The retry-exhaustion chunked fallback honours the disabled-store
        gate too: with a ``NullTranscriptStore`` the FIRST chunk carries no
        toggle (``extra_buttons is None``) and NO transcript write is
        attempted — even though the turn used tools."""
        monkeypatch.setattr("calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        # Exhaust the agent-retry budget so an agent-fixable 400 falls
        # straight through to the chunk-split fallback.
        from calfkit_organization.discord.retry_feedback import MAX_REPLY_RETRY_ATTEMPTS

        for _ in range(MAX_REPLY_RETRY_ATTEMPTS):
            pending_wires.increment_retry(_CORRELATION_ID)

        persona_sender = AsyncMock()
        first_chunk = SentMessage(id=70001, channel_id=6789)
        rest_chunk = SentMessage(id=70002, channel_id=6789)
        # First send (the reply) fails agent-fixably; the chunk-split posts
        # then succeed.
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 400),
                first_chunk,
                rest_chunk,
            ]
        )

        null_store = _SpyNullStore()
        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=null_store
        )
        await consumer.handler(
            envelope=_envelope(
                final_text="x" * 3000,  # forces ≥2 chunks
                message_history=_tool_using_history(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # The first chunk carried no toggle (disabled store gates it off).
        chunk_calls = persona_sender.send.call_args_list[1:]
        assert chunk_calls[0].kwargs["extra_buttons"] is None
        # No transcript write was attempted against the disabled store.
        assert null_store.write_calls == []

    async def test_failed_post_writes_no_row(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tool-using turn whose reply post FAILS terminally (403, not
        retryable) must write NO transcript row: the write is reached only
        after a successful post. Otherwise the store would accumulate
        orphaned rows keyed to message ids that were never posted, and a
        later click would surface steps for a reply the user never saw."""
        monkeypatch.setattr("calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(
                    final_text="It's 18 degrees in Tokyo.",
                    message_history=_tool_using_history(),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        # The post was attempted (and the toggle WAS built for it), but no
        # row exists for the would-be reply id, nor for any other id.
        persona_sender.send.assert_awaited_once()
        assert await transcript_store.get_by_final_message_id("99999") is None

    async def test_chunked_fallback_first_chunk_failure_writes_no_row(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """In the chunked fallback, a FIRST-chunk failure writes no row (there
        is no host message id to key it on) — but the loop still attempts the
        remaining chunks (independent partial delivery). Drives a real
        ``turn_delta`` so the transcript branch is live (the existing
        chunk-2-fails test runs with ``turn_delta=None``)."""
        monkeypatch.setattr("calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        from calfkit_organization.discord.retry_feedback import MAX_REPLY_RETRY_ATTEMPTS

        for _ in range(MAX_REPLY_RETRY_ATTEMPTS):
            pending_wires.increment_retry(_CORRELATION_ID)

        persona_sender = AsyncMock()
        second_chunk = SentMessage(id=70002, channel_id=6789)
        # Main reply fails agent-fixably → chunk fallback. Chunk 0 fails;
        # chunk 1 succeeds.
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 400),
                _http_exc(discord.HTTPException, 403),
                second_chunk,
            ]
        )

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(
                final_text="x" * 3000,  # forces ≥2 chunks
                message_history=_tool_using_history(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # No row written: chunk 0 (the only toggle-bearing chunk) never
        # landed a message id, and chunk 1 doesn't carry the toggle/write.
        assert await transcript_store.get_by_final_message_id("70002") is None
        # The loop still attempted the second chunk after the first failed
        # (main + 2 chunks = 3 sends) — partial delivery preserved.
        assert persona_sender.send.await_count == 3

    async def test_write_transcript_failure_is_swallowed(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
        calfkit_client: MagicMock,
        tmp_path: pathlib.Path,
    ) -> None:
        """A ``write_turn`` that RAISES (e.g. ``sqlite3.OperationalError`` on a
        full disk, or the deliberate ``IntegrityError`` on a final-message-id
        collision) must never crash the consumer or undo the already-posted
        reply: ``_write_transcript`` swallows + logs it. The reply is treated
        as successfully posted; only the (toggle's) row is missing."""

        class _RaisingStore(TranscriptStore):
            async def write_turn(self, row: TranscriptRow) -> None:
                raise sqlite3.OperationalError("disk I/O error")

        store = _RaisingStore(tmp_path / "state" / "transcripts.sqlite3")
        await store.connect()
        try:
            consumer = build_outbox_consumer(
                persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=store
            )
            with caplog.at_level(logging.ERROR):
                # Must not raise out of the handler.
                await consumer.handler(
                    envelope=_envelope(
                        final_text="It's 18 degrees in Tokyo.",
                        message_history=_tool_using_history(),
                    ),
                    correlation_id=_CORRELATION_ID,
                    headers=_headers(),
                    broker=broker,
                )

            # The reply posted (with the toggle); the write failure was
            # logged but did not escape.
            persona_sender.send.assert_awaited_once()
            assert persona_sender.send.call_args.kwargs["extra_buttons"] is not None
            assert any("step toggle will have no row to expand" in r.message for r in caplog.records)
        finally:
            await store.close()

    async def test_thread_reply_uses_source_channel_id_as_conversation_key(
        self,
        persona_sender: AsyncMock,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        """``conversation_key`` is the replay read scope: for a thread-origin
        wire it must be ``source_channel_id`` (not the parent ``channel_id``),
        or tool-call replay silently fails to hydrate in threads. Every other
        outbox test exercises only the ``channel_id`` fallback."""
        thread_pw = PendingWires()
        thread_pw.put(_CORRELATION_ID, make_pending_entry(_wire(source_channel_id=55555)))

        consumer = build_outbox_consumer(
            persona_sender, _registry(), thread_pw, calfkit_client, transcript_store=transcript_store
        )
        await consumer.handler(
            envelope=_envelope(
                final_text="It's 18 degrees in Tokyo.",
                message_history=_tool_using_history(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        row = await transcript_store.get_by_final_message_id("99999")
        assert row is not None
        assert row.conversation_key == "55555"  # source_channel_id wins over channel_id (6789)

    async def test_render_failure_posts_reply_without_toggle_or_row(
        self,
        persona_sender: AsyncMock,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The outbox-side ``_render_step_count`` guard: if
        ``_render_tree_blocks`` raises (``ToolCallPart.args_as_json_str`` blows
        up on malformed args) the step count degrades to 0 — so the reply STILL
        posts, just without the toggle and without a transcript row (degraded,
        not fatal). Mirrors the toggle callback's render guard."""

        def _boom(_messages: object) -> list[str]:
            raise ValueError("malformed tool-call args")

        monkeypatch.setattr("calfkit_organization.bridge.outbox._render_tree_blocks", _boom)

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        with caplog.at_level(logging.ERROR):
            await consumer.handler(
                envelope=_envelope(
                    final_text="It's 18 degrees in Tokyo.",
                    message_history=_tool_using_history(),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        # Reply posted, but with no toggle and no row.
        persona_sender.send.assert_awaited_once()
        assert persona_sender.send.call_args.kwargs["extra_buttons"] is None
        assert await transcript_store.get_by_final_message_id("99999") is None
        assert any("posting reply without toggle/transcript" in r.message for r in caplog.records)


class TestNonDiscordSenderErrors:
    """``DiscordPersonaSender.send`` raises two NON-Discord, operator-actionable
    errors that the Discord-only catch must not let escape into the calfkit
    consumer: ``TypeError`` (``wire.channel_id`` is not a text channel) and
    ``RuntimeError`` (sender not started). They are dropped with a loud log on
    the main path, and tolerated per-chunk on the last-resort fallback."""

    @pytest.fixture(autouse=True)
    def _no_retry_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("calfkit_organization.bridge.outbox._SERVER_ERROR_RETRY_DELAY_SECONDS", 0)

    @pytest.mark.parametrize(
        ("exc", "type_name"),
        [
            (TypeError("Channel 6789 is a ForumChannel, not a TextChannel"), "TypeError"),
            (RuntimeError("DiscordPersonaSender not started"), "RuntimeError"),
        ],
    )
    async def test_non_discord_sender_error_drops_without_raising(
        self,
        exc: Exception,
        type_name: str,
        pending_wires: PendingWires,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        """A non-Discord sender error drops with an operator-actionable ERROR
        (not a raw traceback into calfkit), writes no row, and never raises —
        upholding the outbox's best-effort never-raises contract for a class
        ``except discord.DiscordException`` does not cover."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=exc)

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        with caplog.at_level(logging.ERROR):
            # Must not raise.
            await consumer.handler(
                envelope=_envelope(
                    final_text="It's 18 degrees in Tokyo.",
                    message_history=_tool_using_history(),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        persona_sender.send.assert_awaited_once()
        assert await transcript_store.get_by_final_message_id("99999") is None
        assert any("non-retryable sender error" in r.message and type_name in r.message for r in caplog.records)

    async def test_chunked_fallback_continues_past_non_discord_chunk_error(
        self,
        pending_wires: PendingWires,
        broker: MagicMock,
        calfkit_client: MagicMock,
        transcript_store: TranscriptStore,
    ) -> None:
        """A non-Discord error on an EARLY chunk must not abort the loop: the
        broadened per-chunk catch records it and the remaining chunks still
        post (independent partial delivery). Regression guard for the
        ``(DiscordException, TypeError, RuntimeError)`` chunk catch."""
        from calfkit_organization.discord.retry_feedback import MAX_REPLY_RETRY_ATTEMPTS

        for _ in range(MAX_REPLY_RETRY_ATTEMPTS):
            pending_wires.increment_retry(_CORRELATION_ID)

        persona_sender = AsyncMock()
        second_chunk = SentMessage(id=70002, channel_id=6789)
        # Main reply 400 → chunk fallback. Chunk 0 raises a non-Discord
        # TypeError; chunk 1 still posts.
        persona_sender.send = AsyncMock(
            side_effect=[
                _http_exc(discord.HTTPException, 400),
                TypeError("Channel 6789 is a ForumChannel, not a TextChannel"),
                second_chunk,
            ]
        )

        consumer = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, calfkit_client, transcript_store=transcript_store
        )
        # Must not raise even though a chunk send raised a non-Discord error.
        await consumer.handler(
            envelope=_envelope(final_text="x" * 3000),  # forces ≥2 chunks
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # main + 2 chunks: the loop continued past the TypeError on chunk 0.
        assert persona_sender.send.await_count == 3
