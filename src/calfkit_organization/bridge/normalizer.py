"""Discord event → :class:`WireMessage` translation.

Two normalizers — one for incoming Discord messages (``MESSAGE_CREATE``), one
for slash command interactions. Both are pure functions of their inputs and
the agent registry; they hold no state across calls.

Identity resolution rules (shared):
    - ``is_bot`` mirrors Discord's ``author.bot`` flag.
    - ``is_webhook`` is true when ``message.webhook_id`` is set.
    - ``agent_id`` is set when the author is a webhook whose ``display_name``
      matches a registered :class:`AgentDefinition`. Display-name match is
      the bridge↔agent self-recognition primitive.
    - ``is_human_owner`` is set when ``message.author.id`` equals
      ``settings.owner_user_id`` AND the author is not a bot.

Thread handling: messages posted inside a Discord thread carry the parent
channel's ID as ``channel_id`` (via the thread's ``parent_id``). Thread IDs
are not part of the wire schema; thread messages and parent-channel messages
share one topic per locked decision #1.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal

import uuid_utils

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.identifier import AGENT_ID_CHARSET
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireAuthor, WireMessage

logger = logging.getLogger(__name__)

# Matches "@<agent_id>" where @ starts a whitespace-delimited token — i.e.
# preceded by start-of-string or whitespace. This excludes embedded @s in
# things like email addresses ("foo@bar.com"), URLs, or markdown.
# Character class is derived from ``AGENT_ID_CHARSET`` (the canonical
# agent_id character set) — the surrounding pattern shape differs from
# the agent_id validator (no length cap, capture group, no anchors) but
# the character set must stay in lockstep with the validator.
# Case-insensitive so "@Echo" and "@ECHO" also match.
_MENTION_RE = re.compile(rf"(?:^|\s)@([{AGENT_ID_CHARSET}]+)", re.IGNORECASE)


class UnknownAgentMentionError(ValueError):
    """Raised when a message contains an @<name> that does not resolve to a registered agent.

    The gateway catches this to send a fail-fast error reply back to the
    user in Discord, surfacing the typo or unregistered name.
    """

    def __init__(self, unknown_names: list[str]) -> None:
        self.unknown_names = list(unknown_names)
        super().__init__(f"Unknown agent mention(s): {', '.join(unknown_names)}")


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

    def __init__(
        self,
        registry: AgentRegistry,
        bot_user_id: int,
        human_owner_id: int | None,
    ) -> None:
        self._registry = registry
        self._bot_user_id = bot_user_id
        self._human_owner_id = human_owner_id

    def normalize(self, message: Any) -> WireMessage:
        """Build a :class:`WireMessage` from a ``discord.Message``.

        If the message starts with ``@<agent_id>`` matching a registered agent,
        the wire event is tagged ``kind="slash"`` with ``slash_target`` set to
        that agent's id — this lets agents' existing slash-aware gates handle
        @-mention invocations identically to native slash commands. The
        ``@<agent_id>`` prefix is **not** stripped from ``content``; the agent
        receives the full original text.

        Raises:
            ValueError: If the message has no guild (DM). Callers should filter
                DMs out before calling this, but the contract is enforced here
                as a defensive measure.
        """
        if message.guild is None:
            raise ValueError("MessageNormalizer received a DM (message.guild is None)")

        channel_id = _resolve_channel_id(message.channel)
        # ``source_channel_id`` is the un-flattened channel id (= thread id
        # for thread messages, == channel_id otherwise). The bridge's
        # history fetcher uses this so it fetches the thread's own
        # history, not the parent channel's.
        source_channel_id = message.channel.id
        author = self._build_author(message)
        kind, slash_target = self._classify(message.content)

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

    def normalize_task(self, message: Any, *, thread_id: int) -> WireMessage:
        """Build an ambient :class:`WireMessage` for a plaintext ``/task`` command.

        Mirrors :meth:`normalize` but is purpose-built for the ``/task``
        command (a ``/task <text>`` message the gateway has just opened a
        thread off of). Differences from :meth:`normalize`:

        - **Always ambient** (``kind="message"``, ``slash_target=None``): a
          ``/task`` is routed through the router regardless of any ``@<name>``
          in the text, so it does NOT scan for mentions via :meth:`_classify`.
        - ``source_channel_id`` is the freshly-created task ``thread_id``, so
          agent replies and live-step progress post *into* the thread (see
          :attr:`WireMessage.thread_id`), while ``channel_id`` stays the
          flattened parent text channel that hosts the persona webhook and is
          the Kafka topic key -- so the task inherits the parent channel's
          reachable agents.

        ``message_id`` is the user's own message -- the genuine, user-authored
        thread anchor (the thread shares this id) used as the inline-reply
        target. ``content`` is the full message text, prefix included.

        Raises:
            ValueError: If the message has no guild (DM). The gateway filters
                DMs before this is reached, but the contract is enforced here
                defensively.
        """
        if message.guild is None:
            raise ValueError("MessageNormalizer.normalize_task received a DM (message.guild is None)")

        # ``message.channel`` is the parent text channel (the gateway rejects
        # ``/task`` inside threads/forums), so ``_resolve_channel_id`` returns
        # its own id. ``source_channel_id`` is the new thread, which is what
        # makes ``WireMessage.thread_id`` resolve and replies/steps land
        # in-thread.
        channel_id = _resolve_channel_id(message.channel)
        author = self._build_author(message)

        return WireMessage(
            event_id=uuid_utils.uuid7().hex,
            kind="message",
            slash_target=None,
            message_id=message.id,
            channel_id=channel_id,
            source_channel_id=thread_id,
            guild_id=message.guild.id,
            content=message.content,
            author=author,
            created_at=message.created_at,
        )

    def _classify(self, content: str) -> tuple[Literal["message", "slash"], str | None]:
        """Return ``(kind, slash_target)`` based on @<agent_id> mention scanning.

        Scans **every** ``@<name>`` token in the message (where ``@`` starts a
        whitespace-delimited token, so emails are excluded). If any mentioned
        name does not resolve to a registered agent, raises
        :class:`UnknownAgentMentionError` so the gateway can fail fast and
        report back to the user. If all mentions resolve, the first
        mention's ``agent_id`` becomes ``slash_target``. If no mentions are
        present, returns ``("message", None)``.

        The built-in router agent (``role="router"``) is treated as
        unknown here by design: it's not user-invocable via @-mention,
        only via the ambient ingress topic. Surfacing a router
        @-mention as ``UnknownAgentMentionError`` produces the standard
        operator-actionable error reply rather than a silently
        misrouted message (the router has no Discord persona and
        wouldn't reply).
        """
        raw_names = _MENTION_RE.findall(content)
        if not raw_names:
            return "message", None

        slash_target: str | None = None
        unknown: list[str] = []
        for raw in raw_names:
            spec = self._registry.by_id(raw.lower())
            if spec is None or spec.role == "router":
                unknown.append(raw)
            elif slash_target is None:
                slash_target = spec.agent_id

        if unknown:
            raise UnknownAgentMentionError(unknown)

        assert slash_target is not None  # at least one match, all known
        return "slash", slash_target

    def _build_author(self, message: Any) -> WireAuthor:
        author = message.author
        webhook_id = getattr(message, "webhook_id", None)
        is_webhook = webhook_id is not None
        is_bot = bool(getattr(author, "bot", False))
        is_human_owner = (
            not is_bot
            and self._human_owner_id is not None
            and author.id == self._human_owner_id
        )

        display_name = getattr(author, "display_name", None) or author.name

        agent_id: str | None = None
        if is_webhook:
            spec = self._registry.by_display_name(display_name)
            if spec is not None:
                agent_id = spec.agent_id

        return WireAuthor(
            discord_user_id=author.id,
            display_name=display_name,
            is_bot=is_bot,
            is_webhook=is_webhook,
            webhook_id=webhook_id,
            agent_id=agent_id,
            is_human_owner=is_human_owner,
            avatar_url=_resolve_avatar_url(author),
        )


class SlashNormalizer:
    """Translates ``discord.Interaction`` slash invocations into :class:`WireMessage`."""

    def __init__(
        self,
        registry: AgentRegistry,
        human_owner_id: int | None,
    ) -> None:
        self._registry = registry
        self._human_owner_id = human_owner_id

    def normalize(
        self,
        interaction: Any,
        slash_target: AgentDefinition,
        message_arg: str,
        followup_message_id: int,
    ) -> WireMessage:
        """Build a :class:`WireMessage` for a slash invocation.

        Args:
            interaction: The Discord interaction (slash invocation).
            slash_target: The :class:`AgentDefinition` whose slash was invoked.
            message_arg: The text the user typed into the slash's ``message`` parameter.
            followup_message_id: The ID of the bridge's followup message — the
                visible echo posted via ``interaction.followup.send``. This is
                the anchor agents will reply-to via ``reply_to_message_id``.
        """
        if interaction.channel is None:
            raise ValueError("SlashNormalizer received an interaction with no channel")

        guild_id = getattr(interaction, "guild_id", None) or interaction.guild.id
        channel_id = _resolve_channel_id(interaction.channel)
        # ``source_channel_id`` is the un-flattened channel id (= thread id
        # for thread interactions, == channel_id otherwise). Same
        # rationale as :meth:`MessageNormalizer.normalize`.
        source_channel_id = interaction.channel.id
        user = interaction.user

        is_bot = bool(getattr(user, "bot", False))
        is_human_owner = (
            not is_bot
            and self._human_owner_id is not None
            and user.id == self._human_owner_id
        )
        display_name = getattr(user, "display_name", None) or user.name

        author = WireAuthor(
            discord_user_id=user.id,
            display_name=display_name,
            is_bot=is_bot,
            is_webhook=False,
            webhook_id=None,
            agent_id=None,
            is_human_owner=is_human_owner,
            avatar_url=_resolve_avatar_url(user),
        )

        created_at = getattr(interaction, "created_at", None) or datetime.now(UTC)

        return WireMessage(
            event_id=uuid_utils.uuid7().hex,
            kind="slash",
            slash_target=slash_target.agent_id,
            message_id=followup_message_id,
            channel_id=channel_id,
            source_channel_id=source_channel_id,
            guild_id=guild_id,
            content=message_arg,
            author=author,
            created_at=created_at,
        )
