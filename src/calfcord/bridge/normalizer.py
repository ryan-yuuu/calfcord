"""Discord ``MESSAGE_CREATE`` → :class:`WireMessage` translation.

Pure of any agent roster: under the 0.12 caller surface the bridge resolves which
agent an ``@mention`` targets against the live **mesh** at dispatch time (see
:class:`~calfcord.bridge.roster.MeshRoster` / the ``MentionHandler``), not against
a registry here. So this module only PARSES a Discord message into a
:class:`WireMessage` (the ``deps["discord"]`` payload the agent receives, and the
typed wire the reply poster / history reconstruct) and extracts the ordered list
of ``@<id>`` mention tokens — it never validates them.

Thread handling: a message posted inside a Discord thread carries the parent
channel's id as ``channel_id`` (via the thread's ``parent_id``); ``source_channel_id``
keeps the un-flattened id (the thread itself) so the history fetcher reads the
thread's own history.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

import uuid_utils

from calfcord.agents.identifier import AGENT_ID_CHARSET
from calfcord.bridge.wire import WireAuthor, WireMessage

logger = logging.getLogger(__name__)

# Matches "@<agent_id>" where @ starts a whitespace-delimited token — i.e.
# preceded by start-of-string or whitespace. This excludes embedded @s in
# things like email addresses ("foo@bar.com"), URLs, or markdown. The character
# class is the canonical agent-id set; case-insensitive so "@Echo" also matches.
_MENTION_RE = re.compile(rf"(?:^|\s)@([{AGENT_ID_CHARSET}]+)", re.IGNORECASE)


def extract_mention_ids(content: str) -> tuple[str, ...]:
    """Return the ordered, de-duplicated, lower-cased ``@<id>`` mention tokens.

    Order is preserved (the ``MentionHandler`` invokes the first *online* mention,
    R-A2); duplicates are collapsed so a double-mention doesn't distort the
    "no agent online" notice. Agent ids are lower-case, so tokens are lower-cased
    to match the mesh; no registry/roster validation happens here.
    """
    seen: dict[str, None] = {}
    for raw in _MENTION_RE.findall(content):
        seen.setdefault(raw.lower(), None)
    return tuple(seen)


def _resolve_channel_id(channel: Any) -> int:
    """Return the persistent channel ID for ``channel``.

    For a Discord thread, returns the thread's ``parent_id``. For a top-level
    channel, returns the channel's own ``id``. Duck-typed (checks for the
    ``parent_id`` attribute) so tests can use simple fakes.
    """
    parent_id = getattr(channel, "parent_id", None)
    return parent_id if parent_id is not None else channel.id


def _resolve_avatar_url(author: Any) -> str | None:
    """Return the author's effective avatar URL, or ``None`` if unavailable.

    discord.py exposes ``display_avatar`` on all user-like types
    (User/Member/Webhook author), preferring the per-guild member avatar
    when set and falling back to the user avatar and then Discord's
    default. Duck-typed via ``getattr`` so tests using bare
    ``SimpleNamespace`` fakes can omit the attribute.
    """
    display_avatar = getattr(author, "display_avatar", None)
    if display_avatar is None:
        return None
    return getattr(display_avatar, "url", None)


class MessageNormalizer:
    """Translates ``discord.Message`` events into :class:`WireMessage`."""

    def __init__(self, human_owner_id: int | None) -> None:
        self._human_owner_id = human_owner_id

    def normalize(self, message: Any) -> WireMessage:
        """Build a :class:`WireMessage` from a ``discord.Message``.

        ``kind`` is ``"slash"`` when the message carries any ``@<id>`` mention,
        else ``"message"``; ``slash_target`` is the first mention (lower-cased) or
        ``None``. Neither field gates anything on the caller surface (the addressing
        gate is gone — C6); they remain for wire-schema stability. The agent
        receives the full original ``content`` (the ``@`` prefix is not stripped).

        Raises:
            ValueError: If the message has no guild (DM). Callers should filter
                DMs out before calling this; enforced here defensively.
        """
        if message.guild is None:
            raise ValueError("MessageNormalizer received a DM (message.guild is None)")

        channel_id = _resolve_channel_id(message.channel)
        # ``source_channel_id`` is the un-flattened channel id (= thread id for
        # thread messages, == channel_id otherwise) so the history fetcher reads
        # the thread's own history, not the parent channel's.
        source_channel_id = message.channel.id
        author = self._build_author(message)
        mentions = extract_mention_ids(message.content)
        kind: Literal["message", "slash"] = "slash" if mentions else "message"
        slash_target = mentions[0] if mentions else None

        return WireMessage(
            event_id=uuid_utils.uuid7().hex,
            kind=kind,
            slash_target=slash_target,
            message_id=message.id,
            channel_id=channel_id,
            source_channel_id=source_channel_id,
            guild_id=message.guild.id,
            content=message.content,
            author=author,
            created_at=message.created_at,
        )

    def _build_author(self, message: Any) -> WireAuthor:
        author = message.author
        webhook_id = getattr(message, "webhook_id", None)
        is_webhook = webhook_id is not None
        is_bot = bool(getattr(author, "bot", False))
        is_human_owner = not is_bot and self._human_owner_id is not None and author.id == self._human_owner_id
        display_name = getattr(author, "display_name", None) or author.name

        # ``agent_id`` is no longer resolved here: the registry is gone, and the
        # history fetcher recognizes agent turns by bot-owned ``webhook_id`` (R-A3),
        # not by a wire field. Left ``None`` for wire-schema stability.
        return WireAuthor(
            discord_user_id=author.id,
            display_name=display_name,
            is_bot=is_bot,
            is_webhook=is_webhook,
            webhook_id=webhook_id,
            agent_id=None,
            is_human_owner=is_human_owner,
            avatar_url=_resolve_avatar_url(author),
        )
