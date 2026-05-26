"""Unit tests for the fan-out consumer built by :func:`build_fanout_consumer`.

Drives ``ConsumerNodeDef.handler`` directly with synthetic ``Envelope``s
so we exercise the gate, the metadata lookup, the router-self-filter,
and the per-target ``invoke_node_with_metadata`` publish — all without
Kafka, FastStream, or an LLM.

The original :class:`WireMessage` rides on ``state.metadata`` (the
mechanism :mod:`calfkit_organization._compat.invoke` provides);
``deps.provided_deps`` is intentionally NOT used here because
@consumer's consume_fn never sees deps.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.models import DataPart, State
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    Deps,
    SessionRunContext,
    WorkflowState,
)

from calfkit_organization.agents.routing import RoutingDecision
from calfkit_organization.bridge.history import HistoryRecord
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.router.definition import ROUTER_AGENT_ID
from calfkit_organization.router.fanout import (
    SYNTHESIZED_INGRESS_TOPIC,
    build_fanout_consumer,
)

_CORRELATION_ID = "evt-original"


def _wire(
    *, event_id: str = _CORRELATION_ID, content: str = "hello"
) -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind="message",
        slash_target=None,
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content=content,
        author=WireAuthor(
            discord_user_id=111,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
        ),
        created_at=datetime.now(UTC),
    )


_DEFAULT_DECISION_SENTINEL = "<use-default-decision>"
_PHONEBOOK_UNSET = "<unset>"
_HISTORY_UNSET = "<unset>"


def _phonebook_dict(agent_id: str) -> dict[str, Any]:
    """Build a minimal valid :class:`PhonebookEntry`-shaped dict.

    The typed ``MetadataEnvelope`` parses ``envelope.phonebook`` entries
    through :class:`PhonebookEntry`'s validators, so a bare
    ``{"agent_id": ...}`` dict no longer suffices — ``display_name``
    and ``description`` are required. This helper pads the missing
    fields with placeholders so tests can focus on ``agent_id``
    membership behavior without re-stating persona fields they don't
    care about.
    """
    return {
        "agent_id": agent_id,
        "display_name": agent_id.title(),
        "description": f"test agent {agent_id}",
    }


def _default_phonebook() -> list[dict[str, Any]]:
    """Phonebook with two agents (``scribe`` and ``conan``).

    Covers the default decision's chosen agent (``scribe``) and
    includes a second entry so tests that exercise
    phonebook-membership behavior on a realistic multi-agent registry
    have a non-singleton roster to assert against. Tests that need
    unknown-id, empty-phonebook, or fail-closed paths pass
    ``phonebook=...`` explicitly."""
    return [_phonebook_dict("scribe"), _phonebook_dict("conan")]


def _envelope(
    *,
    correlation_id: str = _CORRELATION_ID,
    decision: Any = _DEFAULT_DECISION_SENTINEL,
    metadata: Any = "<use-wire>",
    wire_content: str = "hello",
    phonebook: Any = _PHONEBOOK_UNSET,
    history: Any = _HISTORY_UNSET,
) -> Envelope:
    """Build a synthetic envelope mimicking the router agent's ReturnCall.

    ``decision=None`` produces an envelope with no ``final_output_parts``
    (an intermediate hop / gate-fail case).

    ``decision`` defaults to a single-agent RoutingDecision picking
    ``scribe`` (sentinel pattern keeps ruff B008 happy — no function
    call in argument defaults).

    ``metadata`` defaults to a dict containing the original wire and
    a phonebook covering the default decision's agent_id. This
    mirrors production: the bridge ALWAYS packs the phonebook on
    ambient publishes. Pass ``None``, ``{}``, or a malformed dict to
    exercise envelope-extract error paths. When
    ``metadata == "<use-wire>"``, the ``phonebook`` arg controls the
    phonebook field on the constructed envelope:

    * ``_PHONEBOOK_UNSET`` (default) — populates with
      :func:`_default_phonebook` so the default decision's agent is
      "known" and the fan-out's happy path runs.
    * ``None`` — deliberately omits the phonebook key. The fan-out's
      fail-closed-on-None path treats this as an infra bug.
    * any other value — packed as ``envelope.phonebook`` verbatim.
      Use :func:`_phonebook_dict` to produce minimal valid entries.

    ``wire_content`` overrides the default content on the default
    wire used when ``metadata == "<use-wire>"`` — useful for tests
    that need to assert non-leakage of unrelated strings into the
    synthesized wire.
    """
    if decision == _DEFAULT_DECISION_SENTINEL:
        decision = RoutingDecision(
            agent_id="scribe", reasoning="topic matches scribe"
        )
    state = State()
    if decision is not None:
        # The router emits DataPart (via pydantic-ai's ToolOutput
        # pattern); the consumer's output_type=RoutingDecision triggers
        # _extract_data which validates against the model.
        state.final_output_parts = [DataPart(data=decision.model_dump(mode="json"))]
    if metadata == "<use-wire>":
        metadata = {"wire": _wire(content=wire_content).model_dump(mode="json")}
        if phonebook is _PHONEBOOK_UNSET:
            metadata["phonebook"] = _default_phonebook()
        elif phonebook is None:
            # Explicit None — caller wants to exercise the fail-closed
            # path; deliberately do NOT add the key.
            pass
        else:
            metadata["phonebook"] = phonebook
        if history is _HISTORY_UNSET:
            # Default: empty history (matches rolling-deploy default).
            pass
        else:
            metadata["history"] = history
    state.metadata = metadata

    call_stack = CallFrameStack()
    call_stack.push(
        CallFrame(
            target_topic="routing.decisions",
            callback_topic="_calf.ambient.callback-discard",
        )
    )
    return Envelope(
        internal_workflow_state=WorkflowState(call_stack=call_stack),
        context=SessionRunContext(
            state=state,
            deps=Deps(
                correlation_id=correlation_id,
                provided_deps={},
            ),
        ),
    )


def _headers(*, emitter: str = ROUTER_AGENT_ID, emitter_kind: str = "agent") -> dict[str, Any]:
    return {"x-calf-emitter": emitter, "x-calf-emitter-kind": emitter_kind}


@pytest.fixture
def client() -> MagicMock:
    """Fake calfkit Client with the attributes invoke_node_with_metadata touches.

    Attaches a fresh-future side-effect on _invoke so the consumer's
    ``handle._future.cancel()`` runs against a real awaitable.
    """
    c = MagicMock()
    c.reply_topic = "calfkit.router.reply"

    def _invoke_handle(*_a: Any, **_kw: Any) -> MagicMock:
        handle = MagicMock()
        handle._future = asyncio.get_event_loop().create_future()
        return handle

    c._invoke = AsyncMock(side_effect=_invoke_handle)
    return c


@pytest.fixture
def broker() -> MagicMock:
    return MagicMock()


class TestHappyPath:
    async def test_publishes_one_envelope_for_chosen_agent(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 1

    async def test_publishes_to_synthesized_ingress_topic(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agent_id="scribe", reasoning="match"),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        kwargs = client._invoke.await_args_list[0].kwargs
        assert kwargs["topic"] == SYNTHESIZED_INGRESS_TOPIC
        assert kwargs["topic"] == "bridge.synthesized.in"

    async def test_synthesized_wire_carries_fresh_event_id(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """The synthesized wire MUST get a fresh event_id; reusing the
        original ambient's id would collide on the bridge's PendingWires
        map and the assistant's reply would be misattributed."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(
                    agent_id="scribe", reasoning="match"
                ),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        kwargs = client._invoke.await_args_list[0].kwargs
        synth_event_id = kwargs["state"].metadata["wire"]["event_id"]
        # Fresh — not the original ambient's event_id.
        assert synth_event_id != _CORRELATION_ID

    async def test_synthesized_wire_overrides_kind_and_slash_target(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agent_id="scribe", reasoning="match"),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        kwargs = client._invoke.await_args_list[0].kwargs
        # state.metadata carries the synthesized wire dict.
        wire = kwargs["state"].metadata["wire"]
        assert wire["kind"] == "slash"
        assert wire["slash_target"] == "scribe"
        # Other fields preserved from the original.
        assert wire["channel_id"] == 6789
        assert wire["content"] == "hello"

    async def test_correlation_id_matches_synthesized_event_id(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """The correlation_id on the synthesized publish equals the new
        wire's event_id — so the bridge ingress's PendingWires keying
        and the outbox's correlation_id lookup are consistent."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agent_id="scribe", reasoning="m"),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        kwargs = client._invoke.await_args_list[0].kwargs
        assert kwargs["correlation_id"] == kwargs["state"].metadata["wire"]["event_id"]

    async def test_cancels_handle_future(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """Mirrors :meth:`BridgeIngress.handle`'s cancel-after-publish so
        the dispatcher's pending future doesn't leak."""
        captured: list[Any] = []

        async def _invoke(*_a: Any, **_kw: Any) -> Any:
            handle = MagicMock()
            handle._future = asyncio.get_event_loop().create_future()
            captured.append(handle)
            return handle

        client._invoke.side_effect = _invoke

        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(
                    agent_id="scribe", reasoning="m"
                ),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        assert len(captured) == 1
        assert captured[0]._future.cancelled()


class TestHistoryForwarding:
    """The fan-out forwards ``envelope.history`` from its INPUT envelope
    (the parent that the router consumed) to the OUTPUT envelope it
    publishes to ``bridge.synthesized.in``.

    Without this forwarding, the synthesized-in consumer would receive
    an empty ``envelope.history``, pass ``prefetched_history=()`` to
    ``BridgeIngress.handle``, and the addressed assistant would run
    with no channel history.
    """

    def _record_dicts(self) -> list[dict[str, Any]]:
        """Build well-formed HistoryRecord dicts (JSON-serializable form)."""
        return [
            HistoryRecord(
                message_id=1,
                created_at=datetime.now(UTC),
                content="hi from ryan",
                author_display_name="ryan",
                author_agent_id=None,
            ).model_dump(mode="json"),
            HistoryRecord(
                message_id=2,
                created_at=datetime.now(UTC),
                content="prior scribe turn",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ).model_dump(mode="json"),
        ]

    async def test_synthesized_envelope_carries_parent_history(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """The synthesized publish must include the parent's history
        records in its ``state.metadata["history"]``."""
        records = self._record_dicts()
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agent_id="scribe", reasoning="match"),
                history=records,
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        kwargs = client._invoke.await_args_list[0].kwargs
        envelope_metadata = kwargs["state"].metadata
        assert "history" in envelope_metadata
        forwarded = envelope_metadata["history"]
        assert len(forwarded) == 2
        assert forwarded[0]["content"] == "hi from ryan"
        assert forwarded[1]["author_agent_id"] == "scribe"

    async def test_empty_history_input_produces_empty_forward(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """If the parent envelope has no history (rolling-deploy / no
        records available), the synthesized envelope forwards an empty
        list, NOT a None or missing-key.
        """
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agent_id="scribe", reasoning="m"),
                # history defaults to _HISTORY_UNSET → no history key in metadata
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        forwarded = client._invoke.await_args_list[0].kwargs["state"].metadata["history"]
        # MetadataEnvelope.history defaults to (); model_dump emits [].
        assert forwarded == []


class TestRouterSelfFilter:
    async def test_skips_routers_own_id(
        self, client: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A misbehaving LLM that picks the router's own id is filtered
        out — synthesizing a wire to the router would loop forever.
        The skip is WARN-logged with full context (channel, author,
        correlation_id, reasoning) so an operator investigating "agent
        didn't reply" can distinguish a model error from a wiring
        bug that exposed the router's id to the roster."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(logging.WARNING, logger="calfkit_organization.router.fanout"):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agent_id=ROUTER_AGENT_ID, reasoning="picked self"
                    ),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert client._invoke.await_count == 0
        warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warn_records, "expected a WARN log line"
        msg = warn_records[0].message
        assert "router's own id" in msg
        # Full context for operator debugging.
        assert "channel=6789" in msg
        assert "author=alice" in msg
        assert "picked self" in msg


class TestErrorPaths:
    async def test_agent_id_none_publishes_nothing(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Defense-in-depth: the router's prompt requires picking
        exactly one agent, but if a misbehaving LLM emits a tool call
        with no ``agent_id`` (``None``), the fan-out consumer must
        no-op rather than crash or publish a malformed wire. The
        schema deliberately allows ``None`` (no required=True) so this
        path stays reachable — see
        :mod:`calfkit_organization.agents.routing` module docstring.

        The skip is WARN-logged (not INFO) because ``None`` is a
        prompt-disobedience signal — operators investigating "no agent
        replied to this user's ambient message" need a greppable
        signal, and the log line includes channel/author/correlation_id
        plus the LLM's reasoning so they can diagnose why the model
        declined to pick."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(logging.WARNING, logger="calfkit_organization.router.fanout"):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(reasoning="small talk; no clear addressee"),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert client._invoke.await_count == 0
        warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warn_records, "expected a WARN log line"
        msg = warn_records[0].message
        assert "no agent_id" in msg
        # Full context for operator debugging.
        assert "channel=6789" in msg
        assert "author=alice" in msg
        assert "small talk; no clear addressee" in msg

    async def test_publish_failure_logs_error_with_full_context(
        self,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A broker hiccup, serialization error, or connection drop
        during ``invoke_node_with_metadata`` would otherwise silently
        drop the user's ambient message — the envelope is ACKed under
        ``AckPolicy.ACK_FIRST`` so the consumer harness won't
        redeliver. The fan-out logs ERROR with full operator-debuggable
        context (channel, author, correlation_id, event_id, agent_id)
        and re-raises so the harness's own consume_fn-raised ERROR
        also fires (two signals: rich operator-greppable line from us
        + harness's traceback). The harness catches the re-raise
        (matches the legacy ``raise_envelope_error`` paths)."""
        client = MagicMock()
        client.reply_topic = "calfkit.router.reply"
        client._invoke = AsyncMock(
            side_effect=RuntimeError("broker hiccup")
        )

        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.router.fanout"
        ):
            # The harness swallows the raise (matches the established
            # pattern in this file — e.g. test_missing_metadata_logs_
            # infra_error). We assert the ERROR log fires instead of
            # relying on propagation.
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agent_id="scribe", reasoning="match"
                    ),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        publish_failure_records = [
            r for r in caplog.records
            if r.levelno == logging.ERROR and "publish failed" in r.message
        ]
        assert publish_failure_records, "expected our 'publish failed' ERROR log"
        msg = publish_failure_records[0].message
        assert "agent=scribe" in msg
        assert "channel=6789" in msg
        assert "author=alice" in msg
        assert f"correlation_id={_CORRELATION_ID}" in msg
        # ``exc_info=True`` was passed — pytest's caplog captures the
        # full traceback in the record's ``exc_info`` attribute.
        assert publish_failure_records[0].exc_info is not None

    async def test_missing_final_output_gate_rejects(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """Envelope with empty final_output_parts is an intermediate
        hop — the gate skips it before consume_fn runs."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(decision=None),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 0

    async def test_missing_metadata_logs_infra_error(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A missing ``state.metadata`` is an infrastructure contract
        violation (the bridge ingress always packs a
        :class:`MetadataEnvelope`). ``_raise_infra`` logs at ERROR
        and raises; calfkit's consumer framework catches the raise
        (Worker uses ``AckPolicy.ACK_FIRST`` so the envelope was
        already ACKed regardless), but the operator-visible ERROR
        log surfaces the violation. Test the log + the no-publish
        guarantee."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(logging.ERROR, logger="calfkit_organization.router.fanout"):
            await consumer.handler(
                envelope=_envelope(metadata=None),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert client._invoke.await_count == 0
        assert any(
            "infra error" in r.message
            and "MetadataEnvelope" in r.message
            for r in caplog.records
        )

    async def test_metadata_without_wire_key_logs_infra_error(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(logging.ERROR, logger="calfkit_organization.router.fanout"):
            await consumer.handler(
                envelope=_envelope(metadata={"other": "stuff"}),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert client._invoke.await_count == 0
        assert any(
            "infra error" in r.message
            and "MetadataEnvelope" in r.message
            for r in caplog.records
        )

    async def test_malformed_wire_logs_infra_error(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A malformed wire dict (the bridge serializing the wrong
        thing) is an infra contract violation — ERROR log + raise
        (framework swallows the raise). With the typed envelope,
        :class:`WireMessage` validation now happens during
        :meth:`MetadataEnvelope.extract`, so the error message
        identifies the failure at the envelope level."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(logging.ERROR, logger="calfkit_organization.router.fanout"):
            await consumer.handler(
                envelope=_envelope(metadata={"wire": {"bogus": "data"}}),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert client._invoke.await_count == 0
        assert any(
            "infra error" in r.message
            and "MetadataEnvelope" in r.message
            for r in caplog.records
        )


class TestTypedEnvelopeError:
    """The infra-error helper now raises a typed
    :class:`MetadataEnvelopeError` with structured attributes. Tests
    can assert on attributes (``site``, ``correlation_id``, ``reason``)
    instead of substring-matching log messages — same context, more
    refactor-resilient."""

    def test_raise_envelope_error_carries_attributes(self) -> None:
        from calfkit_organization._compat.invoke import (
            MetadataEnvelopeError,
            raise_envelope_error,
        )

        with pytest.raises(MetadataEnvelopeError) as exc_info:
            raise_envelope_error(
                correlation_id="evt-fanout-test",
                site="fanout",
                reason="missing phonebook on ambient envelope",
            )
        assert exc_info.value.site == "fanout"
        assert exc_info.value.correlation_id == "evt-fanout-test"
        assert exc_info.value.reason == "missing phonebook on ambient envelope"
        # Subclass of RuntimeError (not ValueError) so the consumer
        # framework treats it as an infrastructure contract violation
        # rather than an input-validation outcome.
        assert isinstance(exc_info.value, RuntimeError)

    def test_raise_envelope_error_chains_cause(self) -> None:
        """When a ``cause`` is provided, ``__cause__`` is set so the
        framework's exc_info trace surfaces the original error."""
        from calfkit_organization._compat.invoke import (
            MetadataEnvelopeError,
            raise_envelope_error,
        )

        original = ValueError("the underlying validation failure")
        with pytest.raises(MetadataEnvelopeError) as exc_info:
            raise_envelope_error(
                correlation_id="evt-test",
                site="fanout",
                reason="failed to extract MetadataEnvelope",
                cause=original,
            )
        assert exc_info.value.__cause__ is original


class TestPhonebookValidation:
    """The fan-out validates the chosen ``agent_id`` against the
    publisher's phonebook snapshot in ``envelope.phonebook``. An
    LLM-hallucinated agent_id (passes regex, not in the registry)
    is skipped at the fan-out before any synthesized publish so it
    can't orphan in :class:`PendingWires` with no operator signal.

    Phonebook entries are now typed :class:`PhonebookEntry` instances
    on the envelope — these helpers build minimum-valid dicts that
    pydantic validates into the typed model at envelope construction.
    """

    @staticmethod
    def _entry(agent_id: str, *, display_name: str | None = None) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "display_name": display_name or agent_id.capitalize(),
            "description": f"{agent_id} agent",
        }

    @staticmethod
    def _two_agent_phonebook() -> list[dict[str, Any]]:
        return [
            TestPhonebookValidation._entry("scribe"),
            TestPhonebookValidation._entry("conan"),
        ]

    async def test_unknown_agent_id_skipped_with_error_log(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An LLM-hallucinated agent_id (passes the schema's
        ``[a-z0-9_-]{1,32}`` regex but isn't in the publisher's
        phonebook) must not produce a synthesized publish. ERROR
        log carries channel + author so the operator can correlate
        to a user-visible "no reply" symptom."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.router.fanout"
        ):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agent_id="hallucinated_agent",
                        reasoning="LLM picked an id that doesn't exist",
                    ),
                    phonebook=self._two_agent_phonebook(),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert client._invoke.await_count == 0
        assert any(
            "unknown agent_id" in r.message
            and "hallucinated_agent" in r.message
            and "hallucination" in r.message.lower()
            for r in caplog.records
        )

    async def test_empty_phonebook_rejects_chosen_id(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An empty phonebook (registry has no assistants) means the
        chosen id is by definition unknown. The fan-out rejects it
        and publishes nothing — preventing wasted synthesized
        publishes from an empty-registry deployment where the
        router's LLM hallucinated anyway."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.router.fanout"
        ):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agent_id="scribe",
                        reasoning="scribe",
                    ),
                    phonebook=[],
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        assert client._invoke.await_count == 0

    async def test_missing_phonebook_logs_infra_error(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A missing phonebook on the ambient envelope is an infra
        bug — production producers ALWAYS pack the phonebook so the
        fan-out can validate the chosen ``agent_id``. The fan-out
        fails closed (logs ERROR and raises) rather than silently
        skipping validation, which would let LLM hallucinations
        through. This replaces the older "skip validation on None"
        behavior; the rolling-deploy hazard that justified the
        skip was solved by dropping ``extra="forbid"`` on the
        envelope (so envelopes still round-trip across mixed
        deployments)."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        # ``phonebook=None`` omits the phonebook key, which
        # MetadataEnvelope parses as ``phonebook=None`` — the
        # fail-closed path.
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.router.fanout"
        ):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agent_id="scribe",
                        reasoning="missing phonebook is an infra bug",
                    ),
                    phonebook=None,
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        # Fail-closed: nothing published.
        assert client._invoke.await_count == 0
        assert any(
            "missing phonebook" in r.message for r in caplog.records
        )

    async def test_known_agent_publishes(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """Regression positive: when the chosen id is in the
        phonebook, the validation is silent and the publish runs."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(
                    agent_id="scribe", reasoning="known"
                ),
                phonebook=self._two_agent_phonebook(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 1

    async def test_malformed_phonebook_entry_fails_at_envelope_extract(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A phonebook entry that fails :class:`PhonebookEntry`
        validation (missing required fields, wrong types) now fails
        at envelope extract — pydantic validates the entire
        ``list[PhonebookEntry]`` on construction. This is stricter
        than the legacy defensive-drop behavior: a malformed entry
        no longer silently disappears from the known-ids set, it
        surfaces immediately as an infra error.

        The producer-side ``PhonebookEntry`` schema validator should
        make this unreachable; this test pins the consumer-side
        symmetry."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        phonebook = [
            self._entry("scribe"),
            {"not_agent_id": "bogus"},  # missing required fields
        ]
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.router.fanout"
        ):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agent_id="scribe", reasoning="just scribe"
                    ),
                    phonebook=phonebook,
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        # Envelope extract fails → infra error → nothing published.
        assert client._invoke.await_count == 0
        assert any(
            "MetadataEnvelope" in r.message for r in caplog.records
        )


class TestReasoningIsolation:
    """The routing decision's ``reasoning`` is operator-side only — it
    must never appear in the synthesized wire's ``content`` (otherwise
    the LLM's chain-of-thought would be posted as a reply to the
    human). Pin the boundary."""

    async def test_reasoning_not_in_synthesized_wire_content(
        self, broker: MagicMock
    ) -> None:
        client = MagicMock()
        client.reply_topic = "calfkit.router.reply"

        async def _invoke(*_a: Any, **_kw: Any) -> Any:
            handle = MagicMock()
            handle._future = asyncio.get_event_loop().create_future()
            return handle

        client._invoke = AsyncMock(side_effect=_invoke)
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)

        secret = "SECRET_REASONING_DO_NOT_LEAK"
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(
                    agent_id="scribe", reasoning=secret
                ),
                # Use a distinct wire content so we can grep for it.
                wire_content="please summarize the meeting notes",
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        # The synthesized wire should carry the ORIGINAL ambient
        # content, never the reasoning.
        synth_wire = client._invoke.await_args.kwargs["state"].metadata["wire"]
        assert synth_wire["content"] == "please summarize the meeting notes"
        assert secret not in synth_wire["content"]
        # Belt-and-suspenders: nothing in the published kwargs should
        # contain the secret.
        all_kwargs_repr = repr(client._invoke.await_args.kwargs)
        assert secret not in all_kwargs_repr


class TestTopicContract:
    """Sanity check that the producer (fan-out) and the consumer
    (bridge synthesized-in) read the same Kafka topic constant. Both
    re-export :data:`calfkit_organization.topics.SYNTHESIZED_INGRESS_TOPIC`
    at module top, so divergence is impossible without a deliberate
    reassignment — but this asserts the contract explicitly rather
    than relying on the import path."""

    def test_synthesized_ingress_topic_matches_bridge(self) -> None:
        from calfkit_organization.bridge.synthesized import (
            SYNTHESIZED_INGRESS_TOPIC as BRIDGE_TOPIC,
        )

        assert SYNTHESIZED_INGRESS_TOPIC == BRIDGE_TOPIC

    def test_ambient_ingress_topic_matches_factory(self) -> None:
        """Same contract as above for the ambient topic — the bridge
        publishes ambient wires to this topic, and the router agent's
        factory subscribes to it. Both sites re-export
        :data:`calfkit_organization.topics.AMBIENT_INGRESS_TOPIC`."""
        from calfkit_organization.bridge.ingress import (
            _AMBIENT_INGRESS_TOPIC,
        )
        from calfkit_organization.topics import (
            AMBIENT_INGRESS_TOPIC,
        )

        assert _AMBIENT_INGRESS_TOPIC == AMBIENT_INGRESS_TOPIC


class TestMetadataKeyContract:
    """Drift guard for the metadata-dict keys the producer
    (``BridgeIngress._publish_ambient`` / this fan-out) and the consumers
    (this fan-out / bridge synthesized-in) all agree on.

    The keys are defined centrally in
    :mod:`calfkit_organization._compat.invoke` so all sites import from
    there. This test pins the contract at the symbol level — if the
    constants are ever renamed or split, the test surfaces the change.
    """

    def test_metadata_key_wire_is_canonical_string(self) -> None:
        from calfkit_organization._compat.invoke import METADATA_KEY_WIRE

        assert METADATA_KEY_WIRE == "wire"

    def test_metadata_key_phonebook_is_canonical_string(self) -> None:
        from calfkit_organization._compat.invoke import METADATA_KEY_PHONEBOOK

        assert METADATA_KEY_PHONEBOOK == "phonebook"
