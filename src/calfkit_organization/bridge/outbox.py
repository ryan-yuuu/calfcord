"""Discord outbox consumer — posts every agent reply landing on the outbox.

A long-lived calfkit :class:`ConsumerNodeDef` subscribed to
``discord.outbox`` in its own Kafka consumer group. Every agent
:class:`ReturnCall` landing on the outbox topic produces one invocation
of :func:`build_outbox_consumer`'s closure, so multi-agent flows
(ambient channel messages, team slashes) get all their replies posted
to Discord rather than just the first to win calfkit's reply-dispatcher
race (the dispatcher dedupes by ``correlation_id`` and would silently
drop every reply after the first — see
``calfkit.client.reply_dispatcher._ReplyDispatcher``).

How the wire is recovered: the consumer receives a
:class:`~calfkit.NodeResult`, which carries ``output``, ``state``,
``correlation_id``, ``emitter_node_id``, and ``emitter_node_kind`` —
but **not** ``Envelope.context.deps``. So the original
:class:`WireMessage` (which holds ``channel_id``, ``message_id``, and
the author info needed for the inline-reply UI) is not on the result.
We recover it from the bridge-local :class:`PendingWires` map that
:class:`BridgeIngress` populates on the way in. The map and the
consumer share a process; this works as long as both live in the
bridge daemon.

Co-existence with the calfkit reply dispatcher: the bridge's
:class:`~calfkit.Client` is connected with
``reply_topic="discord.outbox"`` so the dispatcher's subscriber and
this consumer's subscriber sit in different consumer groups on the
same topic. Kafka multicasts each envelope to both. The dispatcher's
"no pending future" WARNING is therefore expected on every agent reply
(no caller has registered a future); it's noise from a benign code
path, not a defect.

Gate semantics: a single ``final_output_parts`` non-emptiness gate
filters out intermediate hops (tool completions, mid-loop state
transitions) — calfkit's consumer-node docstring recommends this
exact idiom (see ``calfkit.nodes.consumer.ConsumerNodeDef``). Other
filtering (non-agent emitter, unknown agent_id, empty output) happens
inside the closure since those checks need :class:`NodeResult`
fields, not just ``ctx``.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from calfkit import ConsumerNodeDef, NodeResult
from calfkit.models import SessionRunContext

from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.discord.messages import SentMessage
from calfkit_organization.discord.persona import (
    DiscordPersonaSender,
    Persona,
    ReplyContext,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTBOX_TOPIC = "discord.outbox"
DEFAULT_CONSUMER_NODE_ID = "discord-outbox-sink"

# Backoff between our one extra retry attempt. discord.py already does 5
# internal retries for 429/5xx with its own escalating sleep (see
# ``discord.webhook.async_.AsyncWebhookAdapter.request``); our second pass
# is best-effort cleanup for the case where its budget was exhausted by a
# longer-than-usual burst, and usually won't succeed against a multi-hour
# outage. Kept short on purpose: the Worker's default ``max_workers=1``
# means a long retry stalls the entire outbox queue, which directly
# undermines the multi-agent burst case this consumer was added to handle.
_SERVER_ERROR_RETRY_DELAY_SECONDS = 2.0


def build_outbox_consumer(
    persona_sender: DiscordPersonaSender,
    registry: AgentRegistry,
    pending_wires: PendingWires,
    *,
    subscribe_topic: str = DEFAULT_OUTBOX_TOPIC,
    node_id: str = DEFAULT_CONSUMER_NODE_ID,
) -> ConsumerNodeDef[str]:
    """Construct the bridge's outbox consumer node.

    Args:
        persona_sender: The bridge's REST-only Discord client. Used to
            post the reply under the responding agent's persona via
            its per-channel webhook.
        registry: Roster of agents. Resolves
            ``NodeResult.emitter_node_id`` to a :class:`Persona`. An
            unknown emitter id is logged and skipped (defensive — the
            bridge is the only producer to ``discord.channel.*.in``,
            so this should only fire if an agent's ``node_id`` drifts
            from its ``.md`` ``name``).
        pending_wires: Bridge-local store of in-flight wires; see the
            module docstring.
        subscribe_topic: Topic the consumer listens on. Defaults to
            the project-wide ``discord.outbox``. Overridable for tests.
        node_id: Identifier the Worker uses as the Kafka consumer
            ``group_id``. Stable across restarts so offsets persist —
            two bridge processes running in parallel would load-balance
            the egress (one would handle each partition), which is not
            recommended but not catastrophic.

    Returns:
        A :class:`ConsumerNodeDef` ready to register on a
        :class:`~calfkit.Worker`. The Worker subscribes it with
        FastStream's default ``auto_offset_reset="latest"`` — the
        consumer ignores any backlog that pre-dates its boot, which
        matches the reply dispatcher's behavior.
    """

    def _final_output_parts_gate(ctx: SessionRunContext) -> bool:
        # Skip intermediate hops (mid-loop transitions, tool completions).
        # See ``calfkit.nodes.consumer.ConsumerNodeDef`` docstring.
        return bool(ctx.state.final_output_parts)

    async def _post_reply(result: NodeResult[str]) -> None:
        wire = pending_wires.get(result.correlation_id)
        if wire is None:
            # Foreign producer on the topic, or a reply landed after the
            # bridge restarted and lost the pending entry. DEBUG because
            # the latter is a normal-operations scenario.
            logger.debug(
                "outbox saw correlation_id=%s emitter=%s with no pending "
                "wire; skipping",
                result.correlation_id,
                result.emitter_node_id,
            )
            return

        if result.emitter_node_kind != "agent" or not result.emitter_node_id:
            logger.warning(
                "non-agent emitter on outbox event_id=%s id=%s kind=%s",
                wire.event_id,
                result.emitter_node_id,
                result.emitter_node_kind,
            )
            return

        spec = registry.by_id(result.emitter_node_id)
        if spec is None:
            logger.warning(
                "unknown agent emitter=%s event_id=%s",
                result.emitter_node_id,
                wire.event_id,
            )
            return

        text = (result.output or "").strip()
        if not text:
            logger.info(
                "agent %s returned empty output event_id=%s; skipping post",
                result.emitter_node_id,
                wire.event_id,
            )
            return

        sent = await _send_with_one_retry_on_outage(
            persona_sender,
            persona=Persona(name=spec.display_name, avatar_url=spec.avatar_url),
            channel_id=wire.channel_id,
            content=text,
            reply_to=ReplyContext.from_wire(wire),
            event_id=wire.event_id,
            agent_id=result.emitter_node_id,
        )
        if sent is None:
            return
        logger.info(
            "posted reply event_id=%s agent=%s reply_id=%s channel=%s",
            wire.event_id,
            result.emitter_node_id,
            sent.id,
            wire.channel_id,
        )

    return ConsumerNodeDef[str](
        node_id=node_id,
        subscribe_topics=subscribe_topic,
        consume_fn=_post_reply,
        output_type=str,
        gates=[_final_output_parts_gate],
    )


async def _send_with_one_retry_on_outage(
    persona_sender: DiscordPersonaSender,
    *,
    persona: Persona,
    channel_id: int,
    content: str,
    reply_to: ReplyContext,
    event_id: str,
    agent_id: str,
) -> SentMessage | None:
    """Wrap a persona post with structured failure logging + one outage retry.

    Returns ``None`` when the post couldn't be made; the caller skips the
    success log. Never raises ``discord.HTTPException`` — the consumer
    runs with ``re_raise=False`` and any bubbled exception would be logged
    at ERROR by calfkit with a full stack trace, drowning out the
    operationally-useful "channel X is missing permissions" signal.

    Retry policy:
        - ``discord.NotFound`` / ``discord.Forbidden`` → no retry; channel
          was deleted or the bot lost Manage Webhooks. The operator must
          intervene; another attempt won't help.
        - ``discord.DiscordServerError`` (5xx, raised after discord.py's
          own 5 internal retries) → exactly one extra attempt after a
          short sleep. A multi-hour Discord outage is not something we
          can paper over from inside a single-threaded consumer, so
          there's no point burning more attempts.
        - Other ``discord.HTTPException`` (e.g. odd 4xx) → no retry.
    """
    try:
        return await persona_sender.send(
            persona=persona,
            channel_id=channel_id,
            content=content,
            reply_to=reply_to,
        )
    except discord.NotFound as e:
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s: "
            "channel or webhook not found (%s); operator must check the channel exists",
            channel_id, event_id, agent_id, e,
        )
        return None
    except discord.Forbidden as e:
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s: "
            "forbidden (%s); operator must verify Manage Webhooks permission",
            channel_id, event_id, agent_id, e,
        )
        return None
    except discord.DiscordServerError as e:
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s status=%s: "
            "discord.py retries exhausted; retrying once in %.1fs",
            channel_id, event_id, agent_id, e.status, _SERVER_ERROR_RETRY_DELAY_SECONDS,
        )
    except discord.HTTPException as e:
        logger.warning(
            "outbox post failed channel_id=%s event_id=%s agent=%s status=%s: %s",
            channel_id, event_id, agent_id, e.status, e,
        )
        return None

    await asyncio.sleep(_SERVER_ERROR_RETRY_DELAY_SECONDS)
    try:
        return await persona_sender.send(
            persona=persona,
            channel_id=channel_id,
            content=content,
            reply_to=reply_to,
        )
    except discord.NotFound as e:
        # Branched (rather than collapsed under HTTPException) so a
        # permission/channel change that surfaces only on the second
        # attempt still gets the operator-actionable log line.
        logger.warning(
            "outbox post failed after retry channel_id=%s event_id=%s agent=%s: "
            "channel or webhook not found (%s); operator must check the channel exists",
            channel_id, event_id, agent_id, e,
        )
        return None
    except discord.Forbidden as e:
        logger.warning(
            "outbox post failed after retry channel_id=%s event_id=%s agent=%s: "
            "forbidden (%s); operator must verify Manage Webhooks permission",
            channel_id, event_id, agent_id, e,
        )
        return None
    except discord.HTTPException as e:
        logger.warning(
            "outbox post failed after retry channel_id=%s event_id=%s agent=%s status=%s: %s",
            channel_id, event_id, agent_id, e.status, e,
        )
        return None
