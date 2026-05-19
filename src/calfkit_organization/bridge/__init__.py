"""Discord ↔ Calfkit-topic bridge.

Public surface:
    AgentRegistry                   — agent roster index (definitions live in
                                      :mod:`calfkit_organization.agents`)
    WireMessage, WireAuthor         — typed Discord event payload on Kafka
    MessageNormalizer, SlashNormalizer — discord types → WireMessage
    BridgeIngress                   — fire-and-forget agent invocation
    PendingWires                    — process-local correlation_id → wire map
    build_outbox_consumer           — long-lived consumer that posts every
                                      agent reply to Discord
    SlashCommandManager             — registers, syncs, dispatches per-agent slashes
    A2AChannelResolver              — egress helper for agent-to-agent channels
    DiscordIngressGateway, main     — the bridge daemon and CLI entry
"""

from calfkit_organization.bridge.egress import A2AChannelResolver
from calfkit_organization.bridge.gateway import DiscordIngressGateway, main
from calfkit_organization.bridge.ingress import BridgeIngress
from calfkit_organization.bridge.normalizer import (
    MessageNormalizer,
    SlashNormalizer,
    UnknownAgentMentionError,
)
from calfkit_organization.bridge.outbox import build_outbox_consumer
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.slash import SlashCommandManager
from calfkit_organization.bridge.wire import WireAuthor, WireMessage

__all__ = [
    "A2AChannelResolver",
    "AgentRegistry",
    "BridgeIngress",
    "DiscordIngressGateway",
    "MessageNormalizer",
    "PendingWires",
    "SlashCommandManager",
    "SlashNormalizer",
    "UnknownAgentMentionError",
    "WireAuthor",
    "WireMessage",
    "build_outbox_consumer",
    "main",
]
