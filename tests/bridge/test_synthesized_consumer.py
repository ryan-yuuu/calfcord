"""Unit tests for the bridge's synthesized-wire consumer (Phase 4).

The consumer subscribes to ``bridge.synthesized.in`` and feeds every
synthesized wire (from the router's fan-out) through
:meth:`BridgeIngress.handle`. Same testing approach as
``test_outbox.py``: drive ``ConsumerNode.handler`` directly with a
synthetic ``Envelope``.

The synthesized wire and forwarded history ride on the envelope's
``context.deps`` and reach the consumer via ``result.deps`` (calfkit
≥ 0.4.0 exposes inbound producer deps on ``ConsumerContext.deps``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.models import State
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    SessionRunContext,
    WorkflowState,
)

from calfcord.bridge.history import HistoryRecord
from calfcord.bridge.synthesized import (
    SYNTHESIZED_INGRESS_TOPIC,
    build_synthesized_consumer,
)
from calfcord.bridge.wire import WireAuthor, WireMessage

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
    deps: Any = "<use-wire>",
    history: Sequence[HistoryRecord] | None = None,
) -> Envelope:
    """Build a synthetic envelope mimicking a fan-out publish.

    ``deps="<use-wire>"`` populates ``deps["discord"]`` from
    :func:`_wire` (the shape the fan-out publishes) and, when
    ``history`` is supplied, packs it as a JSON list under
    ``deps["history"]`` (the same opaque list the fan-out forwards).
    Pass ``None`` or any other value to ``deps`` to exercise error paths.
    """
    state = State()
    if deps == "<use-wire>":
        deps = {"discord": _wire().model_dump(mode="json")}
        if history is not None:
            deps["history"] = [r.model_dump(mode="json") for r in history]
    # Error-path callers pass deps=None / a non-dict; the envelope's
    # context.deps must be a dict, so normalize to {} (the consumer then
    # reads a missing "discord" key and fails closed).
    context_deps = deps if isinstance(deps, dict) else {}

    call_stack = CallFrameStack()
    call_stack.push(
        CallFrame(
            target_topic=SYNTHESIZED_INGRESS_TOPIC,
            callback_topic="calfkit.router.reply",
        )
    )
    return Envelope(
        internal_workflow_state=WorkflowState(call_stack=call_stack),
        context=SessionRunContext(state=state, deps=context_deps),
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
        """The dict in deps["discord"] is validated through
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
        with caplog.at_level(logging.INFO, logger="calfcord.bridge.synthesized"):
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert any("synthesized-in arrival" in r.message for r in caplog.records)


class TestErrorPaths:
    async def test_no_discord_dep_logs_infra_error(
        self, ingress: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing ``deps["discord"]`` is an infrastructure contract
        violation (the fan-out always packs the wire on deps).
        ``raise_routing_contract_error`` logs at ERROR and raises; the
        calfkit Worker uses ``AckPolicy.ACK_FIRST`` so the envelope is
        already ACKed regardless, but the operator-visible ERROR log
        surfaces the violation."""
        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(
            logging.ERROR, logger="calfcord.bridge.synthesized"
        ):
            await consumer.handler(
                envelope=_envelope(deps={}),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        ingress.handle.assert_not_called()
        assert any(
            "infra error" in r.message
            and "deps['discord']" in r.message
            for r in caplog.records
        )

    async def test_deps_without_discord_key_logs_infra_error(
        self, ingress: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(
            logging.ERROR, logger="calfcord.bridge.synthesized"
        ):
            await consumer.handler(
                envelope=_envelope(deps={"unrelated": "stuff"}),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        ingress.handle.assert_not_called()
        assert any(
            "infra error" in r.message
            and "deps['discord']" in r.message
            for r in caplog.records
        )


class TestHistoryForwarding:
    """The synthesized-in consumer validates the envelope's forwarded
    history records and hands them straight to
    ``ingress.handle(prefetched_history=...)`` so the slash branch
    doesn't re-fetch per fan-out target.
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

        consumer = build_synthesized_consumer(ingress)
        await consumer.handler(
            envelope=_envelope(history=records),
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
        """A rolling-deploy producer that doesn't pack history leaves
        ``deps["history"]`` absent. The consumer must forward an empty
        tuple (NOT None — None would make handle fall back to a fresh
        fetch, defeating the single-fetch invariant)."""
        consumer = build_synthesized_consumer(ingress)
        await consumer.handler(
            envelope=_envelope(),  # no history key in deps
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
        (framework swallows the raise). ``WireMessage.model_validate``
        raises on the bad dict, so the infra error message identifies
        the failing ``deps["discord"]`` key."""
        consumer = build_synthesized_consumer(ingress)
        with caplog.at_level(
            logging.ERROR, logger="calfcord.bridge.synthesized"
        ):
            await consumer.handler(
                envelope=_envelope(deps={"discord": {"missing_required_fields": True}}),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        ingress.handle.assert_not_called()
        assert any(
            "infra error" in r.message and "deps['discord']" in r.message
            for r in caplog.records
        )

    async def test_malformed_history_logs_infra_error(
        self, ingress: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed ``deps["history"]`` entry (the fan-out forwarding
        the wrong shape) fails ``HistoryRecord.model_validate`` and
        surfaces as an infra error before ``ingress.handle`` runs."""
        consumer = build_synthesized_consumer(ingress)
        deps = {
            "discord": _wire().model_dump(mode="json"),
            "history": [{"not_a_record": True}],
        }
        with caplog.at_level(
            logging.ERROR, logger="calfcord.bridge.synthesized"
        ):
            await consumer.handler(
                envelope=_envelope(deps=deps),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        ingress.handle.assert_not_called()
        assert any(
            "infra error" in r.message and "deps['history']" in r.message
            for r in caplog.records
        )

    async def test_contract_error_carries_typed_attributes(self) -> None:
        """The typed :class:`RoutingContractError` exposes structured
        context (``correlation_id``, ``site``, ``reason``) so callers
        can assert on attributes rather than substring-matching log
        messages. Pins the contract that the synthesized-in site emits
        ``site="synthesized-in"`` on every deps-read failure."""
        from calfcord.ambient_routing import (
            RoutingContractError,
            raise_routing_contract_error,
        )

        with pytest.raises(RoutingContractError) as exc_info:
            raise_routing_contract_error(
                correlation_id="evt-test",
                site="synthesized-in",
                reason="bogus deps shape",
            )
        assert exc_info.value.site == "synthesized-in"
        assert exc_info.value.correlation_id == "evt-test"
        assert exc_info.value.reason == "bogus deps shape"
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
        with caplog.at_level(logging.ERROR, logger="calfcord.bridge.synthesized"):
            # No raise — the consumer's catch-and-log path keeps it
            # alive for the next envelope.
            await consumer.handler(
                envelope=_envelope(),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert any("ingress.handle failed" in r.message for r in caplog.records)
