"""Discord communication layer.

Public surface:
    DiscordSettings       — env-driven configuration (bot token, IDs).
    DiscordSender         — REST-only client posting under the bot's identity.
    DiscordPersonaSender  — REST-only client posting under arbitrary
                            persona identities via per-channel webhooks.
    DiscordReceiver       — long-lived gateway consumer. Translates Discord
                            message events into IncomingMessage and dispatches
                            to registered handlers.
    Persona               — display identity (name + avatar) for persona sends.
    dicebear_avatar_url   — default DiceBear avatar URL helper, seeded per agent.
    IncomingMessage       — domain model for a received message.
    SentMessage           — identity of a posted message.
    MessageHandler        — type alias for an async message handler callable.
"""

from calfkit_organization.discord.avatar import dicebear_avatar_url
from calfkit_organization.discord.messages import IncomingMessage, SentMessage
from calfkit_organization.discord.persona import (
    DiscordPersonaSender,
    Persona,
    ReplyContext,
    ReplyStyle,
)
from calfkit_organization.discord.receiver import DiscordReceiver, MessageHandler
from calfkit_organization.discord.sender import DiscordSender
from calfkit_organization.discord.settings import DiscordSettings

__all__ = [
    "DiscordPersonaSender",
    "DiscordReceiver",
    "DiscordSender",
    "DiscordSettings",
    "IncomingMessage",
    "MessageHandler",
    "Persona",
    "ReplyContext",
    "ReplyStyle",
    "SentMessage",
    "dicebear_avatar_url",
]
