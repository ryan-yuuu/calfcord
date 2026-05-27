"""Publish helpers for the control plane.

These functions reach into ``client._connection`` (a FastStream KafkaBroker)
because calfkit's public ``Client`` API exposes only ``invoke_node`` / ``execute_node``,
both of which are agent invocations -- not what we want for plain control-plane
messages. The private-attribute access is documented here in one place so a
future calfkit upgrade that exposes a public ``Client.publish`` is a single-
file swap.

The same broker is used for calfkit's own agent invocation traffic; both flows
coexist on different topics.

Partition keys
--------------
All per-agent publishes (state events, departures, control commands) use the
``agent_id`` as the Kafka partition key. This pins every message for a given
agent to a single partition, which is the only way Kafka guarantees ordering
across multiple events. Without a key, a multi-partition deployment could
deliver an agent's ``command_applied`` state event AFTER its departure event,
leaving the bridge with a stale resurrected entry. Discovery pings are
broadcast and not keyed -- each agent is in its own consumer group, so every
agent reads every partition of ``bridge.discovery`` regardless.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from calfkit.client import Client

from calfkit_organization.control_plane.schema import (
    AgentControlCommand,
    AgentControlEnvelope,
    AgentDepartureEvent,
    AgentStateEvent,
    DiscoveryPingOp,
)
from calfkit_organization.control_plane.topics import (
    AGENT_STATE_TOPIC,
    BRIDGE_DISCOVERY_TOPIC,
    control_topic_for,
)


def _agent_key(agent_id: str) -> bytes:
    """Encode ``agent_id`` as a Kafka partition key.

    Kafka requires keys to be bytes (or null). Encoding here so callers don't
    have to repeat themselves; UTF-8 is the conventional choice and matches
    how the agent_id is serialized inside the payload itself.
    """
    return agent_id.encode("utf-8")


async def publish_control_command(
    client: Client, agent_id: str, command: AgentControlCommand
) -> None:
    """Publish a targeted control command to the agent's control topic.

    Fire-and-forget: returns when the broker has accepted the message;
    does not wait for the agent to consume or apply.

    Partition-keyed by ``agent_id`` so that, on a multi-partition deployment
    of ``agent.<id>.control.in``, successive operator commands stay ordered.
    """
    envelope = AgentControlEnvelope(command=command)
    await client._connection.publish(
        envelope.model_dump(mode="json"),
        topic=control_topic_for(agent_id),
        key=_agent_key(agent_id),
    )


async def publish_discovery_ping(client: Client) -> None:
    """Broadcast a discovery ping. Every running agent's control sink re-announces.

    Not partition-keyed: this is broadcast traffic, and each agent's control
    sink is the sole member of its own consumer group, so it consumes from
    every partition regardless of how the producer routed the message.
    """
    envelope = AgentControlEnvelope(
        command=DiscoveryPingOp(
            issued_at=datetime.now(UTC),
            request_id=str(uuid.uuid4()),
        ),
    )
    await client._connection.publish(
        envelope.model_dump(mode="json"),
        topic=BRIDGE_DISCOVERY_TOPIC,
    )


async def publish_state_event(client: Client, event: AgentStateEvent) -> None:
    """Agent publishes its current state on startup, on each applied command,
    or in response to a discovery ping.

    Partition-keyed by ``event.agent_id`` so the bridge consumes all events
    for a given agent in publish order. Without this, a multi-partition
    ``agent.state`` topic could deliver a ``command_applied`` event AFTER
    the agent's departure event, leaving the bridge with a stale entry.
    """
    await client._connection.publish(
        event.model_dump(mode="json"),
        topic=AGENT_STATE_TOPIC,
        key=_agent_key(event.agent_id),
    )


async def publish_departure(client: Client, agent_id: str) -> None:
    """Best-effort graceful goodbye on agent shutdown.

    Bridge consumes from agent.state, dispatches on the ``kind`` discriminator,
    and removes the agent from its registry projection. Crashes / SIGKILL leave
    the bridge with a stale entry until the agent next restarts.

    Partition-keyed by ``agent_id`` -- same partition as the agent's state
    events so the bridge sees the departure AFTER any prior state events,
    not before or interleaved.
    """
    event = AgentDepartureEvent(
        agent_id=agent_id,
        departed_at=datetime.now(UTC),
    )
    await client._connection.publish(
        event.model_dump(mode="json"),
        topic=AGENT_STATE_TOPIC,
        key=_agent_key(agent_id),
    )
