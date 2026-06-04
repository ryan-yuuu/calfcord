"""End-to-end test of the ambient routing topology.

Drives a single ambient wire through every seam:

    Bridge ingress (ambient publish)
        ↓ deps channel
    Fan-out @consumer (synthesized publishes)
        ↓ deps channel
    Bridge synthesized-in @consumer (ingress.handle invocation)
        ↓ pending_wires + channel topic publish
    Bridge slash path (the assistant-bound invocation)

The Kafka edges (``Client.invoke_node``) are faked. Everything else —
the consumer machinery, the deps packing/unpacking, the gate, the deps
propagation, the synthesized-wire mutation, the pending_wires
bookkeeping — is real.

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
    SessionRunContext,
    WorkflowState,
)

from calfcord.agents.definition import AgentDefinition
from calfcord.agents.routing import RoutingDecision
from calfcord.bridge.ingress import (
    _AMBIENT_INGRESS_TOPIC,
    BridgeIngress,
)
from calfcord.bridge.outbox import build_outbox_consumer
from calfcord.bridge.pending_wires import PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.synthesized import (
    SYNTHESIZED_INGRESS_TOPIC,
    build_synthesized_consumer,
)
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage
from calfcord.router.definition import (
    ROUTER_AGENT_ID,
    build_router_definition,
)
from calfcord.router.fanout import build_fanout_consumer


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
                display_name="Scribe",
                description="Note-taking assistant.",
                provider="openai",
                system_prompt="x",
            ),
            AgentDefinition(
                agent_id="conan",
                display_name="Conan",
                description="Conversational comedian.",
                provider="openai",
                system_prompt="x",
            ),
            build_router_definition(),
        ]
    )


def _build_fanout_envelope_from_router_output(
    *,
    ambient_deps: dict[str, Any],
    decision: RoutingDecision,
) -> Envelope:
    """Construct the envelope the fan-out @consumer would see.

    Mirrors what calfkit's ``BaseAgentNodeDef._publish_action`` would
    produce for a router ReturnCall — it forwards ``envelope.context.deps``
    on every publish, so the consumer reads the original wire / phonebook
    / history from ``result.deps``. We carry the SAME deps dict the bridge
    ingress published on the ambient hop.
    """
    state = State()
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
        context=SessionRunContext(state=state, deps=ambient_deps),
    )


def _build_synthesized_envelope(*, synth_deps: dict[str, Any]) -> Envelope:
    """Construct the envelope the synthesized-in @consumer would see.

    Equivalent to what the fan-out's ``invoke_node`` writes to
    ``bridge.synthesized.in`` — the synthesized wire and forwarded
    history travel on ``deps``. We carry the SAME deps dict the fan-out
    published, so any key-name drift between the fan-out and the
    synthesized-in consumer surfaces here.
    """
    state = State()
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
        context=SessionRunContext(state=state, deps=synth_deps),
    )


def _fresh_handle() -> MagicMock:
    handle = MagicMock()
    handle._future = asyncio.get_event_loop().create_future()
    return handle


def _fresh_client() -> MagicMock:
    c = MagicMock()
    c.invoke_node = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c.reply_topic = "discord.outbox"
    return c


def _headers() -> dict[str, Any]:
    return {b"x-calf-emitter": b"test", b"x-calf-emitter-kind": b"agent"}


@pytest.mark.asyncio
class TestAmbientRoutingEndToEnd:
    async def test_full_flow_routes_to_chosen_agent(self) -> None:
        """Ambient → router → fan-out → bridge.synthesized.in →
        ingress.handle → channel topic, all wired up correctly.

        Pins the seam contract: the original ambient's channel_id /
        message_id / author survive the deps pack/unpack hops and
        end up on the synthesized invocation that the bridge
        republishes to ``discord.channel.{cid}.in``. Under the
        single-agent routing policy, exactly one synthesized publish
        flows through the chain — earlier versions of this test
        exercised multi-agent fan-out, which the schema no longer
        permits."""
        bridge_client = _fresh_client()
        router_client = _fresh_client()
        pending_wires = PendingWires()
        ingress = BridgeIngress(bridge_client, _registry(), pending_wires)

        # Step 1: drive an ambient wire through the bridge ingress.
        ambient_wire = _human_ambient_wire(event_id="evt-original-1")
        await ingress.handle(ambient_wire)

        # Step 2: verify the ambient publish landed on the ambient
        # topic with the wire packed onto deps. (Only the ambient hop
        # has fired so far — the slash branch also uses invoke_node but
        # runs later, in Step 5.)
        bridge_client.invoke_node.assert_awaited_once()
        ambient_call = bridge_client.invoke_node.await_args
        assert ambient_call.kwargs["topic"] == _AMBIENT_INGRESS_TOPIC
        ambient_deps = ambient_call.kwargs["deps"]
        assert ambient_deps["discord"]["event_id"] == "evt-original-1"
        # Ambient publish must NOT populate pending_wires (Group C).
        assert pending_wires.get("evt-original-1") is None

        # Step 3: build the fan-out @consumer (router process) and
        # feed it an envelope mimicking the router agent's ReturnCall —
        # carrying forward the ambient deps.
        fanout = build_fanout_consumer(router_client, router_agent_id=ROUTER_AGENT_ID)
        decision = RoutingDecision(agent_id="scribe", reasoning="scribe is the addressee")
        fanout_envelope = _build_fanout_envelope_from_router_output(
            ambient_deps=ambient_deps,
            decision=decision,
        )
        broker = MagicMock()
        broker.publish = AsyncMock()
        await fanout.handler(
            envelope=fanout_envelope,
            correlation_id="evt-original-1",
            headers=_headers(),
            broker=broker,
        )

        # Step 4: verify the fan-out emitted exactly one publish to
        # the synthesized topic for the chosen agent, with a fresh
        # event_id and the original ambient's channel/message/author
        # preserved on deps["discord"].
        assert router_client.invoke_node.await_count == 1
        synth_publish = router_client.invoke_node.await_args_list[0].kwargs
        assert synth_publish["topic"] == SYNTHESIZED_INGRESS_TOPIC
        synth_deps = synth_publish["deps"]
        synth_wire_dict = synth_deps["discord"]
        assert synth_wire_dict["slash_target"] == "scribe"
        # Fresh event_id (not the original).
        synth_event_id = synth_wire_dict["event_id"]
        assert synth_event_id != "evt-original-1"
        # Channel / message / author preserved.
        assert synth_wire_dict["channel_id"] == 12345
        assert synth_wire_dict["message_id"] == 98765
        assert synth_wire_dict["author"]["display_name"] == "alice"
        assert synth_wire_dict["kind"] == "slash"

        # Step 5: build the synthesized-in @consumer wired to the
        # same BridgeIngress, feed it the synthesized envelope (carrying
        # the fan-out's exact deps), and verify the bridge re-publishes
        # it as a slash wire on the channel topic with pending_wires
        # populated.
        synthesized_consumer = build_synthesized_consumer(ingress)
        envelope = _build_synthesized_envelope(synth_deps=synth_deps)
        await synthesized_consumer.handler(
            envelope=envelope,
            correlation_id=synth_event_id,
            headers=_headers(),
            broker=broker,
        )

        # Step 6: the bridge's ingress should have published exactly
        # one slash invocation for the chosen agent on the channel
        # topic. Both the ambient (Step 1) and slash hops use
        # invoke_node, so filter by the channel-topic prefix to isolate
        # the slash publish.
        slash_calls = [
            c.kwargs
            for c in bridge_client.invoke_node.await_args_list
            if c.kwargs["topic"].startswith("discord.channel.")
        ]
        assert len(slash_calls) == 1
        assert slash_calls[0]["topic"] == "discord.channel.12345.in"
        # PendingWires should have an entry for the synthesized
        # event_id (so the outbox can correlate the reply back).
        # Asserting on the exact slash_target pins identity — a
        # regression that wrote the wrong wire under this event_id
        # would silently misattribute the reply.
        entry = pending_wires.get(synth_event_id)
        assert entry is not None
        assert entry.wire.channel_id == 12345
        assert entry.wire.message_id == 98765
        assert entry.wire.slash_target == "scribe"

        # Step 7: drive the outbox consumer for the synthesized
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
        persona_sender.send = AsyncMock(return_value=SentMessage(id=11111, channel_id=12345))
        # build_outbox_consumer now requires a calfkit Client (for the
        # retry-on-Discord-error path) and a transcript_store (the sole
        # transcript writer on tool-using terminal hops). The outbox only
        # invokes the client on failure, and only writes a transcript when
        # the reply's message_history slice renders to steps; this e2e test
        # exercises the pure-text happy path, so a bare ``AsyncMock`` store
        # and ``MagicMock`` client satisfy the signature without behavior.
        outbox = build_outbox_consumer(
            persona_sender,
            _registry(),
            pending_wires,
            MagicMock(),
            transcript_store=AsyncMock(),
        )

        # The outbox reads ``emitter_node_id`` from the envelope
        # headers; map the synthesized wire to its target agent so
        # the persona resolves correctly.
        reply_state = State()
        reply_state.final_output_parts = [TextPart(text="scribe's reply")]
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
            context=SessionRunContext(state=reply_state, deps={}),
        )
        await outbox.handler(
            envelope=reply_envelope,
            correlation_id=synth_event_id,
            headers={
                "x-calf-emitter": "scribe",
                "x-calf-emitter-kind": "agent",
            },
            broker=broker,
        )

        # One send for the single synthesized reply — no dedupe, no
        # silent-drop.
        assert persona_sender.send.await_count == 1

        # The reply is anchored to the ORIGINAL ambient's Discord
        # message_id (98765 from the human ambient fixture), NOT a
        # fresh synthesized event_id. This is the load-bearing
        # invariant of the multi-hop seam.
        send_call = persona_sender.send.await_args
        reply_to = send_call.kwargs["reply_to"]
        assert reply_to.message_id == 98765, (
            f"reply anchored to {reply_to.message_id!r}; "
            f"expected the original ambient message_id 98765 — "
            f"a future refactor likely added message_id to the "
            f"fanout's wire.model_copy update dict"
        )
        assert reply_to.channel_id == 12345

        # Persona name resolves from the registry's display_name for
        # the chosen agent — scribe's reply goes under "Scribe". This
        # pins that ``emitter_node_id`` (from the outbox headers)
        # correctly drives persona projection at the end of the chain.
        assert send_call.kwargs["content"] == "scribe's reply"
        assert send_call.kwargs["persona"].name == "Scribe"
