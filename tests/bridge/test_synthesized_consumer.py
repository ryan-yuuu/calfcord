"""Unit tests for the bridge's synthesized-wire consumer (Phase 4).

The consumer subscribes to ``bridge.synthesized.in`` and feeds every
synthesized wire (from the router's fan-out) through
:meth:`BridgeIngress.handle`. Same testing approach as
``test_outbox.py``: drive ``ConsumerNodeDef.handler`` directly with a
synthetic ``Envelope``.

The synthesized wire rides on ``state.metadata["wire"]`` (the
:func:`invoke_node_with_metadata` channel) — consumers don't see
``deps``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.models import State
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    Deps,
    SessionRunContext,
    WorkflowState,
)

from calfkit_organization._compat.invoke import MetadataEnvelope
from calfkit_organization.bridge.history import HistoryRecord
from calfkit_organization.bridge.synthesized import (
    SYNTHESIZED_INGRESS_TOPIC,
    build_synthesized_consumer,
)
from calfkit_organization.bridge.wire import WireAuthor, WireMessage

_CORRELATION_ID = "evt-synthesized"


def _wire(
    *,
    event_id: str = _CORRELATION_ID,
    slash_target: str = "scribe",
) -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind="slash",
        slash_target=slash_target,
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content="please respond",
        author=WireAuthor(
            discord_user_id=111,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
        ),
        created_at=datetime.now(UTC),
    )


def _envelope(
    *,
    correlation_id: str = _CORRELATION_ID,
    metadata: Any = "<use-wire>",
) -> Envelope:
    """Build a synthetic envelope mimicking a fan-out publish.

    ``metadata="<use-wire>"`` populates the wire from :func:`_wire`.
    Pass ``None`` or any other value to exercise error paths.
    """
    state = State()
    if metadata == "<use-wire>":
        # Route through MetadataEnvelope.model_dump to pin the
        # producer/consumer seam: if MetadataEnvelope ever gets a
        # custom serializer (alias, computed field, key transform),
        # hand-rolled dicts here would mask a real consumer-side
        # extraction bug.
        metadata = MetadataEnvelope(wire=_wire()).model_dump(mode="json")
    state.metadata = metadata

    call_stack = CallFrameStack()
    call_stack.push(
        CallFrame(
            target_topic=SYNTHESIZED_INGRESS_TOPIC,
            callback_topic="calfkit.router.reply",
        )
    )
    return Envelope(
        internal_workflow_state=WorkflowState(call_stack=call_stack),
        context=SessionRunContext(
            state=state,
            deps=Deps(correlation_id=correlation_id, provided_deps={}),
        ),
    )


def _headers() -> dict[str, Any]:
    return {"x-calf-emitter": "client", "x-calf-emitter-kind": "client"}


@pytest.fixture
def ingress() -> MagicMock:
    """Fake :class:`BridgeIngress` with an AsyncMock ``handle``."""
    i = MagicMock()
    i.handle = AsyncMock()
    return i


@pytest.fixture
def broker() -> MagicMock:
    return MagicMock()


class TestHappyPath:
    async def test_feeds_wire_through_ingress_handle(
        self, ingress: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_synthesized_consumer(ingress)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        ingress.handle.assert_awaited_once()
        # ingress.handle receives a fully-deserialized WireMessage.
        passed_wire = ingress.handle.await_args.args[0]
        assert isinstance(passed_wire, WireMessage)
        assert passed_wire.event_id == _CORRELATION_ID
        assert passed_wire.slash_target == "scribe"

    async def test_wire_is_deserialized_via_pydantic(
        self, ingress: MagicMock, broker: MagicMock
    ) -> None:
        """The dict in state.metadata is validated through
        ``WireMessage.model_validate`` so a real WireMessage instance
        (frozen, with all the model invariants) lands at the ingress.
        Without this round-trip, ingress.handle would receive a raw
        dict and break on attribute access."""
        consumer = build_synthesized_consumer(ingress)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        passed_wire = ingress.handle.await_args.args[0]
        # Frozen model assertions: kind is the literal, not a string.
        assert passed_wire.kind == "slash"

    async def test_logs_arrival_at_info(
        self, ingress: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO log on every arrival is the operator-side signal that
        the router fanned out successfully. Pair with the bridge
        ingress's ambient-publish INFO log to detect a silent router."""
        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(logging.INFO, logger="calfkit_organization.bridge.synthesized"):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert any("synthesized-in arrival" in r.message for r in caplog.records)


class TestErrorPaths:
    async def test_no_metadata_logs_infra_error(
        self, ingress: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing ``state.metadata`` is an infrastructure contract
        violation (the fan-out always packs a
        :class:`MetadataEnvelope`). ``_raise_infra`` logs at ERROR
        and raises; the calfkit Worker uses ``AckPolicy.ACK_FIRST``
        so the envelope is already ACKed regardless, but the
        operator-visible ERROR log surfaces the violation."""
        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.bridge.synthesized"
        ):
            await consumer.handler(
                envelope=_envelope(metadata=None),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        ingress.handle.assert_not_called()
        assert any(
            "infra error" in r.message
            and "MetadataEnvelope" in r.message
            for r in caplog.records
        )

    async def test_metadata_without_wire_key_logs_infra_error(
        self, ingress: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.bridge.synthesized"
        ):
            await consumer.handler(
                envelope=_envelope(metadata={"unrelated": "stuff"}),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        ingress.handle.assert_not_called()
        assert any(
            "infra error" in r.message
            and "MetadataEnvelope" in r.message
            for r in caplog.records
        )


class TestHistoryForwarding:
    """The synthesized-in consumer hands the envelope's history records
    straight through to ``ingress.handle(prefetched_history=...)`` so
    the slash branch doesn't re-fetch per fan-out target.
    """

    async def test_forwards_history_records_to_ingress(
        self, ingress: MagicMock, broker: MagicMock
    ) -> None:
        records = (
            HistoryRecord(
                message_id=1,
                created_at=datetime.now(UTC),
                content="from ambient publish",
                author_display_name="ryan",
                author_agent_id=None,
            ),
            HistoryRecord(
                message_id=2,
                created_at=datetime.now(UTC),
                content="prior scribe reply",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        )
        envelope = MetadataEnvelope(wire=_wire(), history=records).model_dump(
            mode="json"
        )

        consumer = build_synthesized_consumer(ingress)
        await consumer.handler(
            envelope=_envelope(metadata=envelope),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        ingress.handle.assert_awaited_once()
        # prefetched_history is keyword-only — pulled from kwargs.
        kw = ingress.handle.await_args.kwargs
        passed_history = kw["prefetched_history"]
        assert len(passed_history) == 2
        assert passed_history[0].content == "from ambient publish"
        assert passed_history[1].author_agent_id == "scribe"

    async def test_empty_history_tuple_forwarded_as_empty(
        self, ingress: MagicMock, broker: MagicMock
    ) -> None:
        """A rolling-deploy producer that doesn't pack history defaults
        to an empty tuple. The consumer must forward that empty tuple
        verbatim (NOT translate to None — None would make handle fall
        back to a fresh fetch, defeating the single-fetch invariant)."""
        envelope = MetadataEnvelope(wire=_wire()).model_dump(mode="json")

        consumer = build_synthesized_consumer(ingress)
        await consumer.handler(
            envelope=_envelope(metadata=envelope),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        kw = ingress.handle.await_args.kwargs
        passed = kw["prefetched_history"]
        assert passed == ()

    async def test_malformed_wire_logs_infra_error(
        self, ingress: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed wire dict (the fan-out serializing the wrong
        thing) is an infra contract violation — ERROR log + raise
        (framework swallows the raise).

        The envelope's ``wire`` field is now typed against
        :class:`WireMessage`, so validation happens at envelope
        extract rather than at a separate ``WireMessage.model_validate``
        call. The infra error message therefore contains
        ``MetadataEnvelope`` (the extract failure) rather than the
        older inner ``deserialize`` phrasing."""
        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.bridge.synthesized"
        ):
            await consumer.handler(
                envelope=_envelope(metadata={"wire": {"missing_required_fields": True}}),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        ingress.handle.assert_not_called()
        assert any(
            "infra error" in r.message and "MetadataEnvelope" in r.message
            for r in caplog.records
        )

    async def test_envelope_error_carries_typed_attributes(self) -> None:
        """The typed :class:`MetadataEnvelopeError` exposes structured
        context (``correlation_id``, ``site``, ``reason``) so callers
        can assert on attributes rather than substring-matching log
        messages. Pins the contract that the synthesized-in site emits
        ``site="synthesized-in"`` on every envelope-extract failure."""
        from calfkit_organization._compat.invoke import (
            MetadataEnvelopeError,
            raise_envelope_error,
        )

        with pytest.raises(MetadataEnvelopeError) as exc_info:
            raise_envelope_error(
                correlation_id="evt-test",
                site="synthesized-in",
                reason="bogus envelope shape",
            )
        assert exc_info.value.site == "synthesized-in"
        assert exc_info.value.correlation_id == "evt-test"
        assert exc_info.value.reason == "bogus envelope shape"
        # Class hierarchy: must inherit from RuntimeError (not
        # ValueError) so the consumer framework treats it as an
        # infrastructure contract violation.
        assert isinstance(exc_info.value, RuntimeError)

    async def test_ingress_handle_failure_is_caught(
        self, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A downstream ingress failure (e.g., transient broker error
        during the synthesized publish) must be logged and swallowed
        — re-raising would put the partition into a poison-pill loop
        because the offset never advances past the bad envelope."""
        ingress = MagicMock()
        ingress.handle = AsyncMock(side_effect=RuntimeError("broker hiccup"))

        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(logging.ERROR, logger="calfkit_organization.bridge.synthesized"):
            # No raise — the consumer's catch-and-log path keeps it
            # alive for the next envelope.
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert any("ingress.handle failed" in r.message for r in caplog.records)
