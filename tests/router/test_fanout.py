"""Unit tests for the fan-out consumer built by :func:`build_fanout_consumer`.

Drives ``ConsumerNodeDef.handler`` directly with synthetic ``Envelope``s
so we exercise the gate, the metadata lookup, the router-self-filter,
and the per-target ``invoke_node_with_metadata`` publishes — all
without Kafka, FastStream, or an LLM.

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
    """Phonebook covering the default decision's agent_ids
    (``scribe`` + ``conan``). Tests that exercise unknown-id,
    empty-phonebook, or fail-closed paths pass ``phonebook=...``
    explicitly."""
    return [_phonebook_dict("scribe"), _phonebook_dict("conan")]


def _envelope(
    *,
    correlation_id: str = _CORRELATION_ID,
    decision: Any = _DEFAULT_DECISION_SENTINEL,
    metadata: Any = "<use-wire>",
    wire_content: str = "hello",
    phonebook: Any = _PHONEBOOK_UNSET,
) -> Envelope:
    """Build a synthetic envelope mimicking the router agent's ReturnCall.

    ``decision=None`` produces an envelope with no ``final_output_parts``
    (an intermediate hop / gate-fail case).

    ``decision`` defaults to a multi-agent RoutingDecision (sentinel
    pattern keeps ruff B008 happy — no function call in argument
    defaults).

    ``metadata`` defaults to a dict containing the original wire and
    a phonebook covering the default decision's agent_ids. This
    mirrors production: the bridge ALWAYS packs the phonebook on
    ambient publishes. Pass ``None``, ``{}``, or a malformed dict to
    exercise envelope-extract error paths. When
    ``metadata == "<use-wire>"``, the ``phonebook`` arg controls the
    phonebook field on the constructed envelope:

    * ``_PHONEBOOK_UNSET`` (default) — populates with
      :func:`_default_phonebook` so the default decision's agents
      are all "known" and the fan-out's happy path runs.
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
            agents=["scribe", "conan"], reasoning="topic spans both"
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
    async def test_publishes_one_envelope_per_chosen_agent(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 2

    async def test_publishes_to_synthesized_ingress_topic(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agents=["scribe"], reasoning="match"),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        kwargs = client._invoke.await_args_list[0].kwargs
        assert kwargs["topic"] == SYNTHESIZED_INGRESS_TOPIC
        assert kwargs["topic"] == "bridge.synthesized.in"

    async def test_each_fanout_carries_fresh_event_id(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """The synthesized wires MUST get fresh event_ids per chosen agent;
        sharing would collide on the bridge's PendingWires map and the
        second-arriving reply would be misattributed."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(
                    agents=["scribe", "conan"], reasoning="both"
                ),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        ids = [c.kwargs["correlation_id"] for c in client._invoke.await_args_list]
        assert len(ids) == 2
        assert ids[0] != ids[1]
        # And neither matches the original wire's event_id (each call
        # gets a fresh uuid7 from fanout, NOT the original).
        assert _CORRELATION_ID not in ids

    async def test_synthesized_wire_overrides_kind_and_slash_target(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agents=["scribe"], reasoning="match"),
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
                decision=RoutingDecision(agents=["scribe"], reasoning="m"),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        kwargs = client._invoke.await_args_list[0].kwargs
        assert kwargs["correlation_id"] == kwargs["state"].metadata["wire"]["event_id"]

    async def test_cancels_each_handles_future(
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
                    agents=["scribe", "conan"], reasoning="m"
                ),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )

        assert len(captured) == 2
        assert all(h._future.cancelled() for h in captured)


class TestRouterSelfFilter:
    async def test_skips_routers_own_id(
        self, client: MagicMock, broker: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A misbehaving LLM that picks the router's own id is filtered
        out — synthesizing a wire to the router would loop forever."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(logging.INFO, logger="calfkit_organization.router.fanout"):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agents=[ROUTER_AGENT_ID, "scribe"], reasoning="m"
                    ),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        # Only scribe got a synthesized publish.
        assert client._invoke.await_count == 1
        kwargs = client._invoke.await_args.kwargs
        assert kwargs["state"].metadata["wire"]["slash_target"] == "scribe"
        # And we logged the skip.
        assert any("router's own id" in r.message for r in caplog.records)

    async def test_only_routers_own_id_publishes_nothing(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agents=[ROUTER_AGENT_ID], reasoning="m"),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 0


class TestErrorPaths:
    async def test_empty_agents_list_publishes_nothing(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """The silent-ignore case: empty decision means no fan-out."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(agents=[], reasoning="small talk"),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 0

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

    async def test_publish_failure_for_one_target_doesnt_block_others(
        self,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A transient broker hiccup for one target should not drop
        the entire multi-agent reply. Strengthened to check identity:
        the second publish must target the SECOND agent (not a retry
        of the first). A regression that retried the failed target
        twice would pass the count assertion but fail this one."""
        client = MagicMock()
        client.reply_topic = "calfkit.router.reply"
        call_count = {"n": 0}

        async def _invoke(*_a: Any, **_kw: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first publish failed")
            handle = MagicMock()
            handle._future = asyncio.get_event_loop().create_future()
            return handle

        client._invoke = AsyncMock(side_effect=_invoke)

        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(logging.ERROR, logger="calfkit_organization.router.fanout"):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agents=["scribe", "conan"], reasoning="both"
                    ),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        # Both attempted; the second succeeded.
        assert client._invoke.await_count == 2
        assert any("publish failed" in r.message for r in caplog.records)
        # The second call must target the second agent (conan), not a
        # retry of the first (scribe).
        first_metadata = client._invoke.await_args_list[0].kwargs["state"].metadata
        second_metadata = client._invoke.await_args_list[1].kwargs["state"].metadata
        assert first_metadata["wire"]["slash_target"] == "scribe"
        assert second_metadata["wire"]["slash_target"] == "conan"


class TestTypedEnvelopeError:
    """The infra-error helper now raises a typed
    :class:`MetadataEnvelopeError` with structured attributes. Tests
    can assert on attributes (``site``, ``correlation_id``, ``reason``)
    instead of substring-matching log messages — same context, more
    refactor-resilient."""

    def test_raise_envelope_error_carries_attributes(self) -> None:
        from calfkit_organization._compat.invoke import (  # noqa: PLC0415
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
        from calfkit_organization._compat.invoke import (  # noqa: PLC0415
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


class TestDedupe:
    """Duplicate ``agent_id`` entries in a ``RoutingDecision`` (e.g., a
    misbehaving LLM emitting ``["scribe", "scribe"]``) MUST NOT produce
    two synthesized invocations for the same agent. The dedupe lives
    at the :class:`RoutingDecision` schema validator (see
    :mod:`tests.agents.test_routing_schema`); the fan-out's job here
    is to publish whatever ``decision.agents`` contains, having
    already been deduplicated."""

    async def test_duplicate_agent_id_published_once(
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
        # The schema dedupes at construction time: agents tuple is
        # already ("scribe", "conan") before the fan-out ever sees it.
        decision = RoutingDecision(
            agents=["scribe", "scribe", "conan"],
            reasoning="both with a dupe",
        )
        assert decision.agents == ("scribe", "conan")
        await consumer.handler(
            envelope=_envelope(decision=decision),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 2
        slash_targets = {
            client._invoke.await_args_list[i].kwargs["state"].metadata["wire"][
                "slash_target"
            ]
            for i in range(2)
        }
        assert slash_targets == {"scribe", "conan"}


class TestPhonebookValidation:
    """The fan-out validates every chosen ``agent_id`` against the
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
    def _scribe_only_phonebook() -> list[dict[str, Any]]:
        return [
            TestPhonebookValidation._entry("scribe"),
        ]

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
                        agents=["hallucinated_agent"],
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

    async def test_mixed_known_and_unknown_only_known_publish(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When some chosen ids are known and some aren't, the known
        ones publish and the unknown ones are skipped with ERROR.
        Partial degradation is correct: the LLM picked multiple
        respondents and we shouldn't drop the good ones because of
        one bad one."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.router.fanout"
        ):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agents=["scribe", "ghost", "conan"],
                        reasoning="LLM picked scribe + a fake + conan",
                    ),
                    phonebook=self._two_agent_phonebook(),
                ),
                correlation_id=_CORRELATION_ID,
                headers=_headers(),
                broker=broker,
            )
        # scribe + conan publish; ghost is skipped.
        assert client._invoke.await_count == 2
        slash_targets = {
            client._invoke.await_args_list[i].kwargs["state"].metadata["wire"][
                "slash_target"
            ]
            for i in range(2)
        }
        assert slash_targets == {"scribe", "conan"}
        # The skipped id produces a single ERROR.
        assert any(
            "unknown agent_id" in r.message and "ghost" in r.message
            for r in caplog.records
        )

    async def test_empty_phonebook_rejects_all_chosen_ids(
        self,
        client: MagicMock,
        broker: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An empty phonebook (registry has no assistants) means
        every chosen id is by definition unknown. The fan-out
        rejects all of them and publishes nothing — preventing
        wasted synthesized publishes from an empty-registry
        deployment where the router's LLM hallucinated anyway."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.router.fanout"
        ):
            await consumer.handler(
                envelope=_envelope(
                    decision=RoutingDecision(
                        agents=["scribe", "conan"],
                        reasoning="both",
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
        fan-out can validate every chosen ``agent_id``. The fan-out
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
                        agents=["scribe", "conan"],
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

    async def test_all_known_agents_publish(
        self, client: MagicMock, broker: MagicMock
    ) -> None:
        """Regression positive: when every chosen id is in the
        phonebook, the validation is silent and all publish."""
        consumer = build_fanout_consumer(client, router_agent_id=ROUTER_AGENT_ID)
        await consumer.handler(
            envelope=_envelope(
                decision=RoutingDecision(
                    agents=["scribe", "conan"], reasoning="both known"
                ),
                phonebook=self._two_agent_phonebook(),
            ),
            correlation_id=_CORRELATION_ID,
            headers=_headers(),
            broker=broker,
        )
        assert client._invoke.await_count == 2

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
                        agents=["scribe"], reasoning="just scribe"
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
    """Issue 9: the routing decision's ``reasoning`` is operator-side
    only — it must never appear in a synthesized wire's ``content``
    (otherwise the LLM's chain-of-thought would be posted as a reply
    to the human). Pin the boundary."""

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
                    agents=["scribe"], reasoning=secret
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
        from calfkit_organization.bridge.synthesized import (  # noqa: PLC0415
            SYNTHESIZED_INGRESS_TOPIC as BRIDGE_TOPIC,
        )

        assert SYNTHESIZED_INGRESS_TOPIC == BRIDGE_TOPIC

    def test_ambient_ingress_topic_matches_factory(self) -> None:
        """Same contract as above for the ambient topic — the bridge
        publishes ambient wires to this topic, and the router agent's
        factory subscribes to it. Both sites re-export
        :data:`calfkit_organization.topics.AMBIENT_INGRESS_TOPIC`."""
        from calfkit_organization.bridge.ingress import (  # noqa: PLC0415
            _AMBIENT_INGRESS_TOPIC,
        )
        from calfkit_organization.topics import (  # noqa: PLC0415
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
        from calfkit_organization._compat.invoke import METADATA_KEY_WIRE  # noqa: PLC0415

        assert METADATA_KEY_WIRE == "wire"

    def test_metadata_key_phonebook_is_canonical_string(self) -> None:
        from calfkit_organization._compat.invoke import METADATA_KEY_PHONEBOOK  # noqa: PLC0415

        assert METADATA_KEY_PHONEBOOK == "phonebook"
