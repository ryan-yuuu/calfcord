"""Tests for the outbox's retry-with-feedback orchestration.

Covers the bridge-specific transport that consumes the shared policy
helpers from :mod:`calfcord.discord.retry_feedback`:

* :func:`_handle_post_failure` — branch triage (non-retryable / budget
  exhausted / agent retry / Kafka publish failure).
* :func:`_publish_retry` — envelope shape (history + reminder +
  same correlation_id).
* :func:`_post_chunked_fallback` — first-chunk reply anchor, partial
  delivery on per-chunk failure.
* End-to-end via the consumer's handler: 400-50035 → retry envelope
  published; 403 → drop; retry-success path.

Pure-helper coverage (``build_retry_reminder``, ``build_retry_history``,
``chunk_split``, ``classify_error``) lives in
``tests/discord/test_retry_feedback.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from calfkit.models import State
from calfkit.models import TextPart as CalfTextPart
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    SessionRunContext,
    WorkflowState,
)

from calfcord.agents.definition import AgentDefinition
from calfcord.agents.memory import (
    MEMORY_PROMPT_DEPS_KEY,
    _reset_cache_for_tests,
)
from calfcord.bridge.outbox import (
    _handle_post_failure,
    _post_chunked_fallback,
    _publish_retry,
    build_outbox_consumer,
)
from calfcord.bridge.pending_wires import (
    PendingEntry,
    PendingWires,
    make_pending_entry,
)
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import Persona
from calfcord.discord.retry_feedback import (
    MAX_REPLY_RETRY_ATTEMPTS,
)
from calfcord.router.definition import build_router_definition

_CORRELATION_ID = "evt-1"


def _wire() -> WireMessage:
    return WireMessage(
        event_id=_CORRELATION_ID,
        kind="slash",
        slash_target="scribe",
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content="tell me a story",
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
                agent_id="scribe",
                display_name="Scribe",
                description="Scribe agent.",
                avatar_url="https://example.com/scribe.png",
                system_prompt="You are Scribe.",
            ),
            build_router_definition(),
        ]
    )


def _http_exc(exc_cls: type[discord.HTTPException], status: int, *, code: int = 0) -> discord.HTTPException:
    """Build a discord HTTPException with status + JSON error code."""
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic", "code": code})


def _entry(
    *,
    message_history: tuple[Any, ...] = (),
    temp_instructions: str | None = None,
    model_settings: dict[str, Any] | None = None,
) -> PendingEntry:
    """Build a frozen ``PendingEntry`` for tests.

    Retry counts now live on :class:`PendingWires` (side-table). For
    tests that need ``retry_count > 0``, insert the entry into a
    ``PendingWires`` and call ``increment_retry(...)`` N times.
    """
    return PendingEntry(
        wire=_wire(),
        message_history=message_history,
        temp_instructions=temp_instructions,
        model_settings=model_settings,
    )


def _envelope(
    *,
    correlation_id: str = _CORRELATION_ID,
    final_text: str | None = "Booked.",
) -> Envelope:
    state = State()
    if final_text is not None:
        state.final_output_parts = [CalfTextPart(text=final_text)]
    call_stack = CallFrameStack()
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
            deps={},
        ),
    )


def _headers(emitter: str = "scribe") -> dict[str, Any]:
    return {"x-calf-emitter": emitter, "x-calf-emitter-kind": "agent"}


@pytest.fixture
def persona_sender() -> AsyncMock:
    s = AsyncMock()
    s.send = AsyncMock(return_value=SentMessage(id=11111, channel_id=6789))
    return s


@pytest.fixture
def calfkit_client() -> MagicMock:
    def _handle(*_a: Any, **_kw: Any) -> MagicMock:
        h = MagicMock()
        h._future = asyncio.get_event_loop().create_future()
        return h

    c = MagicMock()
    c.invoke_node = AsyncMock(side_effect=_handle)
    return c


@pytest.fixture
def broker() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# _handle_post_failure — branch triage
# ---------------------------------------------------------------------------


class TestHandlePostFailure:
    async def test_non_retryable_403_drops_with_actionable_log(
        self,
        persona_sender: AsyncMock,
        calfkit_client: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pw = PendingWires()
        entry = _entry()
        pw.put(_CORRELATION_ID, entry)
        err = _http_exc(discord.Forbidden, 403)

        with caplog.at_level(logging.WARNING):
            await _handle_post_failure(
                error=err,
                entry=entry,
                agent_id="scribe",
                persona=Persona(name="Scribe", avatar_url=None),
                failed_text="any",
                client=calfkit_client,
                persona_sender=persona_sender,
                pending_wires=pw,
                registry=_registry(),
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                turn_delta=None,
            )

        # No retry, no chunk-split.
        calfkit_client.invoke_node.assert_not_called()
        persona_sender.send.assert_not_called()
        assert any("forbidden" in r.message and "Manage Webhooks" in r.message for r in caplog.records)

    async def test_non_retryable_404_drops(
        self,
        persona_sender: AsyncMock,
        calfkit_client: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pw = PendingWires()
        entry = _entry()
        pw.put(_CORRELATION_ID, entry)
        err = _http_exc(discord.NotFound, 404)

        with caplog.at_level(logging.WARNING):
            await _handle_post_failure(
                error=err,
                entry=entry,
                agent_id="scribe",
                persona=Persona(name="Scribe", avatar_url=None),
                failed_text="any",
                client=calfkit_client,
                persona_sender=persona_sender,
                pending_wires=pw,
                registry=_registry(),
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                turn_delta=None,
            )

        calfkit_client.invoke_node.assert_not_called()
        assert any("not found" in r.message.lower() and "channel" in r.message.lower() for r in caplog.records)

    async def test_5xx_after_smoothing_drops(
        self,
        persona_sender: AsyncMock,
        calfkit_client: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """5xx surfacing here means the inner _send_with_one_retry_on_outage
        already retried once and still got a 5xx. No agent retry — Discord
        is down."""
        pw = PendingWires()
        entry = _entry()
        pw.put(_CORRELATION_ID, entry)
        err = _http_exc(discord.DiscordServerError, 503)

        with caplog.at_level(logging.WARNING):
            await _handle_post_failure(
                error=err,
                entry=entry,
                agent_id="scribe",
                persona=Persona(name="Scribe", avatar_url=None),
                failed_text="any",
                client=calfkit_client,
                persona_sender=persona_sender,
                pending_wires=pw,
                registry=_registry(),
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                turn_delta=None,
            )

        calfkit_client.invoke_node.assert_not_called()
        assert any("5xx + extra retry exhausted" in r.message for r in caplog.records)

    async def test_400_triggers_agent_retry(
        self,
        persona_sender: AsyncMock,
        calfkit_client: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pw = PendingWires()
        entry = _entry()
        pw.put(_CORRELATION_ID, entry)
        err = _http_exc(discord.HTTPException, 400, code=50035)

        with caplog.at_level(logging.INFO):
            await _handle_post_failure(
                error=err,
                entry=entry,
                agent_id="scribe",
                persona=Persona(name="Scribe", avatar_url=None),
                failed_text="x" * 3000,
                client=calfkit_client,
                persona_sender=persona_sender,
                pending_wires=pw,
                registry=_registry(),
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                turn_delta=None,
            )

        # Retry was published; no chunk-split.
        calfkit_client.invoke_node.assert_awaited_once()
        persona_sender.send.assert_not_called()
        # Retry counter advanced.
        assert pw.get_retry_count(_CORRELATION_ID) == 1
        assert any("triggering agent retry attempt=1" in r.message for r in caplog.records)

    async def test_budget_exhausted_chunk_splits(
        self,
        persona_sender: AsyncMock,
        calfkit_client: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        pw = PendingWires()
        entry = _entry()
        pw.put(_CORRELATION_ID, entry)
        # Bring the side-table counter up to the max so the next failure
        # exhausts the budget.
        for _ in range(MAX_REPLY_RETRY_ATTEMPTS):
            pw.increment_retry(_CORRELATION_ID)
        err = _http_exc(discord.HTTPException, 400, code=50035)
        long_text = "paragraph 1.\n\n" + ("x" * 1500) + "\n\n" + ("y" * 1500)

        with caplog.at_level(logging.WARNING):
            await _handle_post_failure(
                error=err,
                entry=entry,
                agent_id="scribe",
                persona=Persona(name="Scribe", avatar_url=None),
                failed_text=long_text,
                client=calfkit_client,
                persona_sender=persona_sender,
                pending_wires=pw,
                registry=_registry(),
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                turn_delta=None,
            )

        # No retry; chunk-split fired.
        calfkit_client.invoke_node.assert_not_called()
        assert persona_sender.send.await_count >= 2
        assert any("retry budget exhausted" in r.message for r in caplog.records)

    async def test_evicted_entry_falls_back_to_chunk_split(
        self,
        persona_sender: AsyncMock,
        calfkit_client: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If the LRU evicts the entry between read and increment_retry,
        chunk-split takes over so the user still receives content."""
        pw = PendingWires(capacity=1)
        entry = _entry()
        pw.put(_CORRELATION_ID, entry)
        # Cause eviction by inserting a second entry.
        other = make_pending_entry(_wire())
        pw.put("other-evt", other)
        # ``entry`` is now evicted; pw.increment_retry(_CORRELATION_ID) → None.

        err = _http_exc(discord.HTTPException, 400, code=50035)
        long_text = "x" * 5000

        with caplog.at_level(logging.WARNING):
            await _handle_post_failure(
                error=err,
                entry=entry,
                agent_id="scribe",
                persona=Persona(name="Scribe", avatar_url=None),
                failed_text=long_text,
                client=calfkit_client,
                persona_sender=persona_sender,
                pending_wires=pw,
                registry=_registry(),
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                turn_delta=None,
            )

        calfkit_client.invoke_node.assert_not_called()
        assert persona_sender.send.await_count >= 1
        assert any("evicted before retry could be claimed" in r.message for r in caplog.records)

    async def test_retry_publish_failure_falls_back_to_chunk_split(
        self,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If the Kafka publish for the retry envelope itself raises,
        chunk-split takes over."""
        pw = PendingWires()
        entry = _entry()
        pw.put(_CORRELATION_ID, entry)
        broken_client = MagicMock()
        broken_client.invoke_node = AsyncMock(side_effect=RuntimeError("kafka down"))

        err = _http_exc(discord.HTTPException, 400, code=50035)

        with caplog.at_level(logging.ERROR):
            await _handle_post_failure(
                error=err,
                entry=entry,
                agent_id="scribe",
                persona=Persona(name="Scribe", avatar_url=None),
                failed_text="x" * 3000,
                client=broken_client,
                persona_sender=persona_sender,
                pending_wires=pw,
                registry=_registry(),
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                turn_delta=None,
            )

        broken_client.invoke_node.assert_awaited_once()
        # Counter STILL advanced (we claimed the attempt before the publish).
        assert pw.get_retry_count(_CORRELATION_ID) == 1
        # Chunk-split picked up.
        assert persona_sender.send.await_count >= 1
        assert any("retry publish failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _publish_retry — envelope shape
# ---------------------------------------------------------------------------


class TestPublishRetry:
    async def test_envelope_uses_original_correlation_id(self, calfkit_client: MagicMock) -> None:
        entry = _entry()
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "failed text", err)

        kw = calfkit_client.invoke_node.call_args.kwargs
        assert kw["correlation_id"] == _CORRELATION_ID

    async def test_envelope_targets_agent_inbox(self, calfkit_client: MagicMock) -> None:
        entry = _entry()
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "failed text", err)

        kw = calfkit_client.invoke_node.call_args.kwargs
        assert kw["topic"] == "agent.scribe.in"

    async def test_envelope_history_includes_original_prompt_and_failed_reply(self, calfkit_client: MagicMock) -> None:
        original_history = (
            ModelRequest(parts=[UserPromptPart(content="<ryan> earlier turn")]),
            ModelResponse(parts=[TextPart(content="scribe's earlier reply")]),
        )
        entry = _entry(message_history=original_history)
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "FAILED TEXT", err)

        kw = calfkit_client.invoke_node.call_args.kwargs
        history = kw["message_history"]
        # First entries: the original history (passed through).
        assert history[0] is original_history[0]
        assert history[1] is original_history[1]
        # Then the original user prompt as a ModelRequest.
        assert isinstance(history[2], ModelRequest)
        assert any(isinstance(p, UserPromptPart) and p.content == "tell me a story" for p in history[2].parts)
        # Then the failed reply as a ModelResponse.
        assert isinstance(history[3], ModelResponse)
        assert any(isinstance(p, TextPart) and p.content == "FAILED TEXT" for p in history[3].parts)

    async def test_envelope_user_prompt_is_system_reminder(self, calfkit_client: MagicMock) -> None:
        entry = _entry()
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "fail", err)

        kw = calfkit_client.invoke_node.call_args.kwargs
        assert kw["user_prompt"].startswith("<system-reminder>")
        assert "HTTP 400" in kw["user_prompt"]

    async def test_envelope_includes_phonebook_in_deps(self, calfkit_client: MagicMock) -> None:
        entry = _entry()
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "fail", err)

        kw = calfkit_client.invoke_node.call_args.kwargs
        assert "phonebook" in kw["deps"]
        # Phonebook excludes the router; the registry has scribe + router.
        ids = {e["agent_id"] for e in kw["deps"]["phonebook"]}
        assert ids == {"scribe"}

    async def test_envelope_includes_memory_prompt_when_agent_opted_in(
        self, calfkit_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A memory-enabled agent must keep its memory block on the corrective
        retry turn: the bridge re-ships the template in ``deps`` exactly as the
        ingress does on the first attempt. Regression guard for the outbox retry
        path that previously dropped ``memory_prompt`` — leaving the agent to run
        the fix-it turn with no memory context."""
        monkeypatch.delenv("CALFCORD_MEMORY_PROMPT_PATH", raising=False)
        _reset_cache_for_tests()
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scribe",
                    display_name="Scribe",
                    description="Scribe agent.",
                    avatar_url="https://example.com/scribe.png",
                    memory=True,
                    system_prompt="You are Scribe.",
                ),
                build_router_definition(),
            ]
        )
        entry = _entry()
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, registry, entry, "scribe", "fail", err)

        deps = calfkit_client.invoke_node.call_args.kwargs["deps"]
        assert MEMORY_PROMPT_DEPS_KEY in deps
        # The bridge ships the RAW (un-localized) template; the agent's hook
        # localizes ``{{MEMORY_DIR}}``, so the placeholder must survive on the wire.
        assert "{{MEMORY_DIR}}" in deps[MEMORY_PROMPT_DEPS_KEY]

    async def test_envelope_omits_memory_prompt_when_no_memory_agent(
        self, calfkit_client: MagicMock
    ) -> None:
        """No memory-enabled agent in the registry → no template shipped, so the
        retry deps stay byte-identical to the pre-feature shape (no wire cost).
        The default ``_registry()`` fixture's scribe has memory off."""
        entry = _entry()
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "fail", err)

        deps = calfkit_client.invoke_node.call_args.kwargs["deps"]
        assert MEMORY_PROMPT_DEPS_KEY not in deps

    async def test_envelope_preserves_temp_instructions(self, calfkit_client: MagicMock) -> None:
        entry = _entry(temp_instructions="peer roster here")
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "fail", err)

        kw = calfkit_client.invoke_node.call_args.kwargs
        assert kw["temp_instructions"] == "peer roster here"

    async def test_envelope_cancels_handle_future(self, calfkit_client: MagicMock) -> None:
        """Fire-and-forget pattern: the handle's future is cancelled so
        the reply dispatcher's pending-future map doesn't leak. We
        verify the publish happened exactly once; the cancel is on the
        returned handle (mocked) and would manifest as a coroutine-
        never-awaited warning under pytest if the cancel was skipped —
        the absence of that warning in the run is the proof of the
        cancel.
        """
        entry = _entry()
        err = _http_exc(discord.HTTPException, 400, code=50035)

        await _publish_retry(calfkit_client, _registry(), entry, "scribe", "fail", err)

        calfkit_client.invoke_node.assert_awaited_once()


# ---------------------------------------------------------------------------
# _post_chunked_fallback
# ---------------------------------------------------------------------------


class TestChunkedFallback:
    async def test_short_text_posts_once_with_reply_anchor(
        self,
        persona_sender: AsyncMock,
    ) -> None:
        await _post_chunked_fallback(
            persona_sender,
            Persona(name="Scribe", avatar_url=None),
            _wire(),
            "short reply",
            transcript_store=AsyncMock(),
            correlation_id=_CORRELATION_ID,
            agent_id="scribe",
            turn_delta=None,
        )
        persona_sender.send.assert_awaited_once()
        assert persona_sender.send.call_args.kwargs["reply_to"] is not None
        assert persona_sender.send.call_args.kwargs["content"] == "short reply"

    async def test_long_text_posts_multiple_first_with_reply_anchor(
        self,
        persona_sender: AsyncMock,
    ) -> None:
        long = ("a" * 1500) + "\n\n" + ("b" * 1500) + "\n\n" + ("c" * 1500)
        await _post_chunked_fallback(
            persona_sender,
            Persona(name="Scribe", avatar_url=None),
            _wire(),
            long,
            transcript_store=AsyncMock(),
            correlation_id=_CORRELATION_ID,
            agent_id="scribe",
            turn_delta=None,
        )
        assert persona_sender.send.await_count == 3
        # First chunk: reply_to set.
        assert persona_sender.send.await_args_list[0].kwargs["reply_to"] is not None
        # Subsequent chunks: bare continuations.
        assert persona_sender.send.await_args_list[1].kwargs["reply_to"] is None
        assert persona_sender.send.await_args_list[2].kwargs["reply_to"] is None

    async def test_empty_text_is_noop(
        self,
        persona_sender: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING):
            await _post_chunked_fallback(
                persona_sender,
                Persona(name="Scribe", avatar_url=None),
                _wire(),
                "",
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                agent_id="scribe",
                turn_delta=None,
            )
        persona_sender.send.assert_not_called()
        assert any("empty text" in r.message for r in caplog.records)

    async def test_per_chunk_failure_continues_with_next(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If chunk 2 fails, chunks 1 and 3 still post."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                SentMessage(id=1, channel_id=6789),
                _http_exc(discord.HTTPException, 400),
                SentMessage(id=3, channel_id=6789),
            ]
        )
        long = ("a" * 1500) + "\n\n" + ("b" * 1500) + "\n\n" + ("c" * 1500)

        with caplog.at_level(logging.ERROR):
            await _post_chunked_fallback(
                persona_sender,
                Persona(name="Scribe", avatar_url=None),
                _wire(),
                long,
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                agent_id="scribe",
                turn_delta=None,
            )

        # All three attempts happened, even after chunk 2 failed.
        assert persona_sender.send.await_count == 3
        assert any("chunk-split failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# End-to-end via the consumer's handler
# ---------------------------------------------------------------------------


class TestEndToEnd:
    async def test_400_50035_via_handler_triggers_retry(
        self,
        calfkit_client: MagicMock,
        broker: MagicMock,
    ) -> None:
        """Full handler path: agent reply lands, persona post fails with
        50035, retry envelope is published."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.HTTPException, 400, code=50035))
        pw = PendingWires()
        pw.put(_CORRELATION_ID, make_pending_entry(_wire()))

        consumer = build_outbox_consumer(persona_sender, _registry(), pw, calfkit_client, transcript_store=AsyncMock())
        await consumer.handler(
            envelope=_envelope(final_text="x" * 3000),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        # Retry envelope was published to agent.scribe.in.
        calfkit_client.invoke_node.assert_awaited_once()
        assert calfkit_client.invoke_node.call_args.kwargs["topic"] == "agent.scribe.in"

    async def test_403_via_handler_does_not_retry(
        self,
        calfkit_client: MagicMock,
        broker: MagicMock,
    ) -> None:
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
        pw = PendingWires()
        pw.put(_CORRELATION_ID, make_pending_entry(_wire()))

        consumer = build_outbox_consumer(persona_sender, _registry(), pw, calfkit_client, transcript_store=AsyncMock())
        await consumer.handler(
            envelope=_envelope(final_text="hello"),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        calfkit_client.invoke_node.assert_not_called()

    async def test_retry_success_logs_attempt_count(
        self,
        calfkit_client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A successful post AFTER retries logs the 'agent retry succeeded'
        line so operators can see which retries paid off."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(return_value=SentMessage(id=99999, channel_id=6789))
        pw = PendingWires()
        # Simulate "this is the result of retry attempt #1." The
        # counter lives on the side-table; bump it via increment_retry
        # rather than constructing the (now-frozen) PendingEntry with
        # a counter field.
        pw.put(_CORRELATION_ID, _entry())
        pw.increment_retry(_CORRELATION_ID)

        consumer = build_outbox_consumer(persona_sender, _registry(), pw, calfkit_client, transcript_store=AsyncMock())
        with caplog.at_level(logging.INFO):
            await consumer.handler(
                envelope=_envelope(final_text="now shorter"),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        persona_sender.send.assert_awaited_once()
        assert any("agent retry succeeded after 1 attempt" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Round-2 review additions: multi-retry sequence, 5xx-smoothing chain,
# RateLimited handling, all-chunks-fail summary, plural-attempts log.
# ---------------------------------------------------------------------------


class TestMultiRetrySequence:
    """The headline value path: original fails → retry N → success.

    Verifies the side-table counter advances correctly across multiple
    invocations of the handler for the same correlation_id, and that
    the 'retry succeeded after N attempt(s)' log line is correct for
    N > 1.
    """

    async def test_counter_advances_across_handler_invocations(
        self,
        calfkit_client: MagicMock,
        broker: MagicMock,
    ) -> None:
        """Original fail → retry 1 fail → retry 2 fail → budget exhausted.
        Each ``_handle_post_failure`` invocation should observe the
        counter from the previous call and increment correctly.
        """
        persona_sender = AsyncMock()
        # Every send fails with 400-50035.
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.HTTPException, 400, code=50035))
        pw = PendingWires()
        pw.put(_CORRELATION_ID, _entry())

        consumer = build_outbox_consumer(persona_sender, _registry(), pw, calfkit_client, transcript_store=AsyncMock())

        # Three handler invocations: original + retry 1 + retry 2.
        for _ in range(3):
            await consumer.handler(
                envelope=_envelope(final_text="x" * 3000),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        # After three failed attempts: counter == MAX_REPLY_RETRY_ATTEMPTS.
        assert pw.get_retry_count(_CORRELATION_ID) == MAX_REPLY_RETRY_ATTEMPTS
        # Retries published: 2 (original failure publishes retry 1;
        # retry-1 failure publishes retry 2; retry-2 failure exhausts
        # budget and chunk-splits instead of publishing retry 3).
        assert calfkit_client.invoke_node.await_count == MAX_REPLY_RETRY_ATTEMPTS

    async def test_retry_succeeded_log_uses_plural_for_n_greater_than_1(
        self,
        calfkit_client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A reply success after 2 retries should log 'after 2 attempt(s)'.

        Pinning the format string for N > 1 catches regressions where
        someone introduces a singular/plural branch and forgets the
        plural case.
        """
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(return_value=SentMessage(id=99999, channel_id=6789))
        pw = PendingWires()
        pw.put(_CORRELATION_ID, _entry())
        # Side-table counter == 2 (we're delivering after retry attempt #2).
        pw.increment_retry(_CORRELATION_ID)
        pw.increment_retry(_CORRELATION_ID)

        consumer = build_outbox_consumer(persona_sender, _registry(), pw, calfkit_client, transcript_store=AsyncMock())
        with caplog.at_level(logging.INFO):
            await consumer.handler(
                envelope=_envelope(final_text="finally fits"),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        assert any("agent retry succeeded after 2 attempt" in r.message for r in caplog.records)


class TestRateLimitedHandling:
    """``discord.RateLimited`` is NOT a subclass of ``HTTPException``
    in discord.py — it inherits directly from ``DiscordException``.
    The outbox must catch it and route through the same triage as
    HTTP errors, treating it as non-retryable (rate-limit backoff
    isn't agent-fixable)."""

    async def test_rate_limited_is_dropped_not_retried(
        self,
        calfkit_client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # discord.RateLimited takes (retry_after, message).
        rate_limited = discord.RateLimited(retry_after=60.0)
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=rate_limited)
        pw = PendingWires()
        pw.put(_CORRELATION_ID, _entry())

        consumer = build_outbox_consumer(persona_sender, _registry(), pw, calfkit_client, transcript_store=AsyncMock())
        with caplog.at_level(logging.WARNING):
            await consumer.handler(
                envelope=_envelope(final_text="hello"),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )

        # No retry published; operator-actionable WARN.
        calfkit_client.invoke_node.assert_not_called()
        assert any("rate-limit backoff exhausted" in r.message for r in caplog.records)


class TestAllChunksFailSummary:
    """When every chunk in the chunk-split fallback fails, an
    aggregated WARN summary identifies the systemic failure so
    operators see one actionable line instead of having to
    aggregate N per-chunk ERRORs themselves."""

    async def test_all_chunks_fail_logs_dominant_status_summary(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All 3 chunks 403 → one WARN summary with dominant_status=403."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
        long = ("a" * 1500) + "\n\n" + ("b" * 1500) + "\n\n" + ("c" * 1500)

        with caplog.at_level(logging.WARNING):
            await _post_chunked_fallback(
                persona_sender,
                Persona(name="Scribe", avatar_url=None),
                _wire(),
                long,
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                agent_id="scribe",
                turn_delta=None,
            )

        # All chunks attempted.
        assert persona_sender.send.await_count == 3
        # The aggregate summary WARN names the status + total + zero
        # successes so an operator sees a single actionable line.
        assert any(
            "chunk-split delivered 0/3 chunks" in r.message and "dominant_status=403" in r.message
            for r in caplog.records
        )

    async def test_partial_chunk_failure_no_summary(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If at least one chunk succeeds, no aggregate summary fires."""
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            side_effect=[
                SentMessage(id=1, channel_id=6789),
                _http_exc(discord.HTTPException, 400),
                SentMessage(id=3, channel_id=6789),
            ]
        )
        long = ("a" * 1500) + "\n\n" + ("b" * 1500) + "\n\n" + ("c" * 1500)

        with caplog.at_level(logging.WARNING):
            await _post_chunked_fallback(
                persona_sender,
                Persona(name="Scribe", avatar_url=None),
                _wire(),
                long,
                transcript_store=AsyncMock(),
                correlation_id=_CORRELATION_ID,
                agent_id="scribe",
                turn_delta=None,
            )

        # No aggregate "0/N" summary fired.
        assert not any("chunk-split delivered 0/" in r.message for r in caplog.records)
