"""End-to-end test of the ambient routing topology.

Drives a single ambient wire through every seam:

    Bridge ingress (ambient publish)
        ↓ state.metadata channel
    Fan-out @consumer (synthesized publishes)
        ↓ state.metadata channel
    Bridge synthesized-in @consumer (ingress.handle invocation)
        ↓ pending_wires + channel topic publish
    Bridge slash path (the assistant-bound invocation)

The Kafka edges (``Client.invoke_node`` / ``Client._invoke``) are
faked. Everything else — the consumer machinery, the metadata
packing/unpacking, the gate, the state propagation, the
synthesized-wire mutation, the pending_wires bookkeeping — is real.

This test catches seam-mismatch regressions that the per-module tests
miss: a key-name typo on one side, a topic name drifting, a wire
field that doesn't round-trip cleanly, an event_id collision in
``PendingWires``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.models import DataPart, State, TextPart
from calfkit.models.envelope import Envelope
from calfkit.models.session_context import (
    CallFrame,
    CallFrameStack,
    Deps,
    SessionRunContext,
    WorkflowState,
)

from calfkit_organization._compat.invoke import METADATA_KEY_WIRE, MetadataEnvelope
from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.routing import RoutingDecision
from calfkit_organization.bridge.ingress import (
    _AMBIENT_INGRESS_TOPIC,
    BridgeIngress,
)
from calfkit_organization.bridge.outbox import build_outbox_consumer
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.synthesized import (
    SYNTHESIZED_INGRESS_TOPIC,
    build_synthesized_consumer,
)
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.messages import SentMessage
from calfkit_organization.router.definition import (
    ROUTER_AGENT_ID,
    build_router_definition,
)
from calfkit_organization.router.fanout import build_fanout_consumer


def _human_ambient_wire(*, event_id: str = "evt-original") -> WireMessage:
    """An ambient message from a real human — the entry point of the flow."""
    return WireMessage(
        event_id=event_id,
        kind="message",
        slash_target=None,
        message_id=98765,
        channel_id=12345,
        guild_id=99999,
        content="hey everyone, what's the weather like?",
        author=WireAuthor(
            discord_user_id=42,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/42/abc.png",
        ),
        created_at=datetime.now(UTC),
    )


def _registry() -> AgentRegistry:
    """Production registry shape: two assistants + the built-in router."""
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scribe",
                slash="/scribe",
                display_name="Scribe",
                description="Note-taking assistant.",
                provider="openai",
                system_prompt="x",
            ),
            AgentDefinition(
                agent_id="conan",
                slash="/conan",
                display_name="Conan",
                description="Conversational comedian.",
                provider="openai",
                system_prompt="x",
            ),
            build_router_definition(),
        ]
    )


def _captured_state_from_invoke(client: MagicMock, index: int = 0) -> State:
    """Extract the ``State`` from a recorded ``Client._invoke`` call."""
    return client._invoke.await_args_list[index].kwargs["state"]


def _build_fanout_envelope_from_router_output(
    *,
    ambient_state: State,
    decision: RoutingDecision,
    correlation_id: str,
) -> Envelope:
    """Construct the envelope the fan-out @consumer would see.

    Mirrors what calfkit's ``BaseAgentNodeDef._publish_action`` would
    produce for a router ReturnCall — it forwards
    ``envelope.context.deps`` AND ``state.metadata``. We carry only the
    metadata channel because the consume_fn reads from there; the wire
    rides under METADATA_KEY_WIRE thanks to the bridge ingress.
    """
    state = State()
    state.metadata = ambient_state.metadata
    # The router's terminal output is parsed as RoutingDecision via the
    # ToolOutput pattern; the consumer's output_type triggers
    # _extract_data which expects the DataPart payload.
    state.final_output_parts = [DataPart(data=decision.model_dump(mode="json"))]
    return Envelope(
        internal_workflow_state=WorkflowState(
            call_stack=CallFrameStack(
                _internal_list=[
                    CallFrame(
                        target_topic="routing.decisions",
                        callback_topic="<test-discard>",
                    )
                ]
            )
        ),
        context=SessionRunContext(
            state=state,
            deps=Deps(correlation_id=correlation_id, provided_deps={}),
        ),
    )


def _build_synthesized_envelope(
    *, synthesized_wire_dict: dict[str, Any], correlation_id: str
) -> Envelope:
    """Construct the envelope the synthesized-in @consumer would see.

    Equivalent to what ``invoke_node_with_metadata`` writes to
    ``bridge.synthesized.in`` — the wire travels in ``state.metadata``
    via :class:`MetadataEnvelope`. Build the metadata by round-tripping
    through ``MetadataEnvelope.model_dump`` rather than hand-rolling
    the dict: if the envelope ever grows a custom serializer (alias,
    computed field, key transform), a hand-rolled dict here would
    mask a real bridge-side extraction bug.
    """
    state = State()
    wire = WireMessage.model_validate(synthesized_wire_dict)
    state.metadata = MetadataEnvelope(wire=wire).model_dump(mode="json")
    return Envelope(
        internal_workflow_state=WorkflowState(
            call_stack=CallFrameStack(
                _internal_list=[
                    CallFrame(
                        target_topic=SYNTHESIZED_INGRESS_TOPIC,
                        callback_topic="<test-discard>",
                    )
                ]
            )
        ),
        context=SessionRunContext(
            state=state,
            deps=Deps(correlation_id=correlation_id, provided_deps={}),
        ),
    )


def _fresh_handle() -> MagicMock:
    handle = MagicMock()
    handle._future = asyncio.get_event_loop().create_future()
    return handle


def _fresh_client() -> MagicMock:
    c = MagicMock()
    c.invoke_node = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c._invoke = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c.reply_topic = "discord.outbox"
    return c


def _headers() -> dict[str, Any]:
    return {b"x-calf-emitter": b"test", b"x-calf-emitter-kind": b"agent"}


@pytest.mark.asyncio
class TestAmbientRoutingEndToEnd:
    async def test_full_flow_routes_to_chosen_agents(self) -> None:
        """Ambient → router → fan-out → bridge.synthesized.in →
        ingress.handle → channel topic, all wired up correctly.

        Pins the seam contract: the original ambient's channel_id /
        message_id / author survive the metadata-pack/unpack hop and
        end up on the synthesized invocations that the bridge
        republishes to ``discord.channel.{cid}.in``."""
        bridge_client = _fresh_client()
        router_client = _fresh_client()
        pending_wires = PendingWires()
        ingress = BridgeIngress(bridge_client, _registry(), pending_wires)

        # Step 1: drive an ambient wire through the bridge ingress.
        ambient_wire = _human_ambient_wire(event_id="evt-original-1")
        await ingress.handle(ambient_wire)

        # Step 2: verify the ambient publish landed on the ambient
        # topic with the wire packed into state.metadata.
        bridge_client._invoke.assert_awaited_once()
        ambient_call = bridge_client._invoke.await_args
        assert ambient_call.kwargs["topic"] == _AMBIENT_INGRESS_TOPIC
        ambient_state = ambient_call.kwargs["state"]
        assert ambient_state.metadata[METADATA_KEY_WIRE]["event_id"] == "evt-original-1"
        # Ambient publish must NOT populate pending_wires (Group C).
        assert pending_wires.get("evt-original-1") is None

        # Step 3: build the fan-out @consumer (router process) and
        # feed it an envelope mimicking the router agent's ReturnCall.
        fanout = build_fanout_consumer(router_client, router_agent_id=ROUTER_AGENT_ID)
        decision = RoutingDecision(
            agents=["scribe", "conan"], reasoning="both fit the topic"
        )
        fanout_envelope = _build_fanout_envelope_from_router_output(
            ambient_state=ambient_state,
            decision=decision,
            correlation_id="evt-original-1",
        )
        broker = MagicMock()
        broker.publish = AsyncMock()
        await fanout.handler(
            envelope=fanout_envelope,
            correlation_id="evt-original-1",
            headers=_headers(),
            broker=broker,
        )

        # Step 4: verify the fan-out emitted two publishes to the
        # synthesized topic, one per chosen agent, each with a fresh
        # event_id and the original ambient's channel/message/author
        # preserved.
        assert router_client._invoke.await_count == 2
        synth_publishes = [
            router_client._invoke.await_args_list[i].kwargs
            for i in range(2)
        ]
        assert all(p["topic"] == SYNTHESIZED_INGRESS_TOPIC for p in synth_publishes)
        synth_wires = [p["state"].metadata[METADATA_KEY_WIRE] for p in synth_publishes]
        slash_targets = sorted(w["slash_target"] for w in synth_wires)
        assert slash_targets == ["conan", "scribe"]
        # Fresh event_ids (not the original).
        event_ids = {w["event_id"] for w in synth_wires}
        assert "evt-original-1" not in event_ids
        assert len(event_ids) == 2  # two distinct fresh ids
        # Channel / message / author preserved.
        for w in synth_wires:
            assert w["channel_id"] == 12345
            assert w["message_id"] == 98765
            assert w["author"]["display_name"] == "alice"
            assert w["kind"] == "slash"

        # Step 5: build the synthesized-in @consumer wired to the
        # same BridgeIngress, feed each synthesized envelope, and
        # verify the bridge re-publishes each as a slash wire on the
        # channel topic with pending_wires populated.
        synthesized_consumer = build_synthesized_consumer(ingress)
        for synth_wire_dict in synth_wires:
            envelope = _build_synthesized_envelope(
                synthesized_wire_dict=synth_wire_dict,
                correlation_id=synth_wire_dict["event_id"],
            )
            await synthesized_consumer.handler(
                envelope=envelope,
                correlation_id=synth_wire_dict["event_id"],
                headers=_headers(),
                broker=broker,
            )

        # Step 6: the bridge's ingress should have published two slash
        # invocations (one per chosen agent) on top of the original
        # ambient publish. invoke_node is the slash branch.
        assert bridge_client.invoke_node.await_count == 2
        slash_publishes = [
            bridge_client.invoke_node.await_args_list[i].kwargs
            for i in range(2)
        ]
        assert all(
            p["topic"] == "discord.channel.12345.in" for p in slash_publishes
        )
        # PendingWires should have entries for both synthesized
        # event_ids (so the outbox can correlate replies back).
        # Per-iteration assertion pins identity, not just set
        # membership: a regression that wrote scribe's wire under
        # conan's event_id would pass an ``in {...}`` check but
        # silently misattribute replies. Pinning the *exact*
        # slash_target per entry catches that.
        for synth_wire_dict in synth_wires:
            entry = pending_wires.get(synth_wire_dict["event_id"])
            assert entry is not None
            assert entry.wire.channel_id == 12345
            assert entry.wire.message_id == 98765
            assert entry.wire.slash_target == synth_wire_dict["slash_target"]

        # Step 7: drive the outbox consumer for each synthesized
        # event_id and verify the agent reply is anchored to the
        # ORIGINAL ambient's Discord message_id, not a fresh
        # synthesized event_id.
        #
        # This pins the multi-hop seam contract: ``router/fanout.py``'s
        # ``wire.model_copy(update={"event_id": ..., "kind": "slash",
        # "slash_target": ...})`` preserves ``message_id`` by
        # construction (the update dict deliberately excludes it). If
        # a future refactor adds ``message_id`` to that update dict,
        # the outbox would anchor replies to a fresh-but-nonexistent
        # message id, breaking the inline-reply UI in Discord. This
        # test fails immediately on that regression.
        persona_sender = AsyncMock()
        persona_sender.send = AsyncMock(
            return_value=SentMessage(id=11111, channel_id=12345)
        )
        # build_outbox_consumer now requires a calfkit Client (for the
        # retry-on-Discord-error path). The outbox only invokes it on
        # failure; this e2e test exercises the happy path so a bare
        # MagicMock satisfies the signature without behavior.
        outbox = build_outbox_consumer(
            persona_sender, _registry(), pending_wires, MagicMock()
        )

        # The outbox reads ``emitter_node_id`` from the envelope
        # headers; map each synthesized wire to its target agent so
        # the persona resolves correctly.
        for synth_wire_dict in synth_wires:
            target = synth_wire_dict["slash_target"]
            synth_event_id = synth_wire_dict["event_id"]
            reply_state = State()
            reply_state.final_output_parts = [
                TextPart(text=f"{target}'s reply")
            ]
            reply_envelope = Envelope(
                internal_workflow_state=WorkflowState(
                    call_stack=CallFrameStack(
                        _internal_list=[
                            CallFrame(
                                target_topic="discord.outbox",
                                callback_topic="discord.outbox",
                            )
                        ]
                    )
                ),
                context=SessionRunContext(
                    state=reply_state,
                    deps=Deps(
                        correlation_id=synth_event_id, provided_deps={}
                    ),
                ),
            )
            await outbox.handler(
                envelope=reply_envelope,
                correlation_id=synth_event_id,
                headers={
                    "x-calf-emitter": target,
                    "x-calf-emitter-kind": "agent",
                },
                broker=broker,
            )

        # One send per synthesized wire reply — no dedupe, no
        # silent-drop.
        assert persona_sender.send.await_count == 2

        # Every reply is anchored to the ORIGINAL ambient's Discord
        # message_id (98765 from the human ambient fixture), NOT a
        # fresh synthesized event_id. This is the load-bearing
        # invariant of the multi-hop seam.
        send_calls = persona_sender.send.await_args_list
        for call in send_calls:
            reply_to = call.kwargs["reply_to"]
            assert reply_to.message_id == 98765, (
                f"reply anchored to {reply_to.message_id!r}; "
                f"expected the original ambient message_id 98765 — "
                f"a future refactor likely added message_id to the "
                f"fanout's wire.model_copy update dict"
            )
            assert reply_to.channel_id == 12345

        # Persona name resolves from the registry's display_name for
        # each agent — scribe's reply goes under "Scribe", conan's
        # under "Conan". This pins that ``emitter_node_id`` (from the
        # outbox headers) correctly drives persona projection at the
        # end of the chain.
        persona_names_by_content = {
            call.kwargs["content"]: call.kwargs["persona"].name
            for call in send_calls
        }
        assert persona_names_by_content["scribe's reply"] == "Scribe"
        assert persona_names_by_content["conan's reply"] == "Conan"
