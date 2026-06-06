"""Reconstruct the live agent roster from the control plane (host-agnostic).

The bridge's in-memory ``AgentRegistry`` is a *projection of the ``agent.state``
topic* — so any host with broker access can rebuild "who's alive" without reading
the bridge's memory (the CLI may run on a different host, and the bridge may be
down). :func:`reduce_live_roster` is the pure replay of that projection: feed it
the control-plane messages collected from ``agent.state`` and it returns the
surviving roster, applying the same schema-version gate and upsert/remove dispatch
the bridge's state consumer applies one message at a time.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable

from calfkit.client import Client

from calfcord._provisioning import PROVISIONING, provision_and_start_broker
from calfcord.agents.definition import AgentDefinition
from calfcord.control_plane.builders import state_event_to_definition
from calfcord.control_plane.publish import publish_discovery_ping
from calfcord.control_plane.schema import (
    CONTROL_PLANE_SCHEMA_VERSION,
    AgentDepartureEvent,
    AgentStateEvent,
    AgentStateMessage,
)
from calfcord.control_plane.topics import AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC

_DEFAULT_PROBE_WINDOW_S = 2.0


def reduce_live_roster(messages: Iterable[AgentStateMessage]) -> list[AgentStateEvent]:
    """Replay control-plane messages into the current live roster.

    Mirrors the bridge's state-consumer dispatch as a batch reduction:

    * messages whose ``schema_version`` != :data:`CONTROL_PLANE_SCHEMA_VERSION`
      are ignored (forward/backward-incompatible — same gate the bridge applies);
    * an :class:`AgentStateEvent` upserts (replaces) the entry for its
      ``agent_id``;
    * an :class:`AgentDepartureEvent` removes its ``agent_id``.

    Replay is in arrival order — per-agent ordering is guaranteed upstream by the
    ``agent_id`` partition key — so a re-announce after a departure correctly
    re-adds the agent. Returns the surviving state events sorted by ``agent_id``
    for deterministic output.
    """
    live: dict[str, AgentStateEvent] = {}
    for message in messages:
        if message.schema_version != CONTROL_PLANE_SCHEMA_VERSION:
            continue
        if isinstance(message, AgentStateEvent):
            live[message.agent_id] = message
        elif isinstance(message, AgentDepartureEvent):
            live.pop(message.agent_id, None)
    return [live[agent_id] for agent_id in sorted(live)]


async def probe_live_roster(
    server_urls: str, *, timeout_s: float = _DEFAULT_PROBE_WINDOW_S
) -> list[AgentDefinition]:
    """Reconstruct the live agent roster by probing the control plane over Kafka.

    Host-agnostic and bridge-independent: connects a transient client to
    ``server_urls``, subscribes to ``agent.state`` at ``auto_offset_reset="latest"``
    (so only responses to *this* probe are seen, not retained history), broadcasts
    a discovery ping — exactly what the bridge does at ``on_ready`` — and collects
    the state events running agents publish in reply for ``timeout_s`` seconds.
    Only currently-running agents answer, so the result is true liveness with no
    stale entries (unlike replaying the log or reading the bridge's registry).

    The subscriber is registered before the broker starts and uses a unique
    consumer group so it never disturbs the bridge's own ``agent.state`` group.
    Returns the surviving roster (see :func:`reduce_live_roster`) as
    ``AgentDefinition``\\ s, sorted by ``agent_id``.
    """
    collected: list[AgentStateMessage] = []

    async def _collect(message: AgentStateMessage) -> None:
        collected.append(message)

    async with Client.connect(server_urls, provisioning=PROVISIONING) as client:
        # Subscribe BEFORE the broker starts (FastStream contract); a unique group
        # + latest offset means a clean, history-free read isolated from the bridge.
        client._connection.subscriber(
            AGENT_STATE_TOPIC,
            group_id=f"calfcord-probe-{uuid.uuid4().hex}",
            auto_offset_reset="latest",
        )(_collect)
        # Provision the reply topic (calf-ai/calfkit-sdk#180) plus the two control
        # topics this probe touches, so the direct start does not hang on Tansu.
        await provision_and_start_broker(
            client, extra_topics=[AGENT_STATE_TOPIC, BRIDGE_DISCOVERY_TOPIC]
        )
        await publish_discovery_ping(client)
        await asyncio.sleep(timeout_s)

    return [state_event_to_definition(event) for event in reduce_live_roster(collected)]
