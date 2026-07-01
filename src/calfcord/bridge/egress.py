"""Unified agent-to-agent audit channel resolution.

Provides a small helper that resolves (and lazily creates) the single
Discord text channel where every agent-to-agent conversation lives, plus
a helper for anchoring a public thread on a specific message in that
channel. There is one such channel per guild, sourced from
``CALFKIT_A2A_CHANNEL_NAME`` (default ``private-a2a-chats``) — collapsing the
former per-pair ``a2a-{x}-{y}`` design to a flat directory of threads
inside one channel.

The unified design eliminates the O(N²) channel-count growth of the
per-pair scheme: ten agents now require one channel plus one permission
overwrite, not 45 channels with 45 overwrites. Each A2A call posts its
caller's request as a normal message in the unified channel, then
anchors a thread on that message; subsequent turns in the same
conversation post into the thread. Humans see a clean per-channel
directory of conversation starters and can drill into any thread to
audit.

When constructed with a ``category_name`` (sourced from
``CALFKIT_A2A_CHANNEL_CATEGORY`` by the bridge, whose gateway constructs the
resolver), the unified
channel is placed under that Discord category on first creation — and
the category itself is created lazily on the first miss. The child
channel inherits the category's permission overwrites, so locking down
audit visibility is a one-time Discord-UI operation on the category
rather than a per-channel chore. An existing channel with the
configured name is reused regardless of its current category, so
operator overrides and migrations are non-disruptive.

The resolver intentionally does not validate agent identities: identity
validation is the caller's job. The bridge is now its only caller — it
validates ids against the live mesh roster before reaching here.
"""

from __future__ import annotations

import logging

import discord

from calfcord.discord.sender import DiscordSender

logger = logging.getLogger(__name__)


class A2AChannelResolver:
    """Resolves the unified A2A audit channel and anchors A2A threads on it."""

    def __init__(
        self,
        sender: DiscordSender,
        guild_id: int,
        *,
        channel_name: str,
        category_name: str | None = None,
    ) -> None:
        self._sender = sender
        self._guild_id = guild_id
        self._channel_name = channel_name
        self._category_name = category_name
        self._unified_channel_id: int | None = None
        self._category: discord.CategoryChannel | None = None

    async def resolve_unified_channel(self) -> int:
        """Return the Discord channel ID of the unified A2A audit channel.

        Cached after the first resolution. On a full cache miss, scans
        the guild for an existing channel matching ``channel_name``; on
        full miss, creates one (under ``category_name`` if configured).

        Raises:
            discord.Forbidden: If the bot lacks ``Manage Channels`` in
                the guild and the channel needs to be created.
            discord.HTTPException: On Discord 5xx during discovery or
                creation.
        """
        if self._unified_channel_id is not None:
            return self._unified_channel_id

        channel_id = await self._discover()
        if channel_id is None:
            channel_id = await self._create()
        self._unified_channel_id = channel_id
        return channel_id

    async def create_anchored_thread(
        self,
        channel_id: int,
        anchor_message_id: int,
        *,
        name: str,
    ) -> int:
        """Create a public thread anchored on a message in the unified channel.

        The thread is rooted at ``anchor_message_id`` — the message
        becomes the thread's "starter" so anyone scrolling the parent
        channel sees the conversation's first line and can click into
        the thread for the rest. ``discord.py``'s
        :meth:`TextChannel.create_thread` accepts any
        :class:`discord.abc.Snowflake` as ``message=``, so we pass a
        synthetic :class:`discord.Object` (no ``fetch_message``
        round-trip needed).

        Args:
            channel_id: ID of the unified A2A channel (typically the
                value returned by :meth:`resolve_unified_channel`).
            anchor_message_id: ID of the message to anchor the thread
                on — typically the caller's just-posted request.
            name: Thread title. The caller is responsible for keeping
                this within Discord's 100-character limit.

        Returns:
            The newly-created thread's snowflake ID.

        Raises:
            discord.Forbidden: If the bot lacks ``Create Public Threads``
                on the parent channel.
            discord.NotFound: If ``anchor_message_id`` no longer exists
                (race: the message was deleted between post and anchor).
            discord.HTTPException: On any other Discord-side failure.

        Each error is logged at WARN with ``channel_id`` /
        ``anchor_message_id`` / ``name`` context before re-raising so
        operators have an actionable record without having to
        cross-reference call sites.
        """
        try:
            channel = await self._sender.client.fetch_channel(channel_id)
        except discord.DiscordException:
            logger.warning(
                "a2a anchor thread channel fetch failed channel_id=%d anchor_message_id=%d name=%r",
                channel_id,
                anchor_message_id,
                name,
                exc_info=True,
            )
            raise

        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "a2a anchor thread target is not a TextChannel channel_id=%d type=%s anchor_message_id=%d name=%r",
                channel_id,
                type(channel).__name__,
                anchor_message_id,
                name,
            )
            raise TypeError(f"channel_id={channel_id} resolved to {type(channel).__name__}, expected TextChannel")

        try:
            thread = await channel.create_thread(
                name=name,
                message=discord.Object(id=anchor_message_id),
            )
        except discord.DiscordException:
            logger.warning(
                "a2a anchor thread creation failed channel_id=%d anchor_message_id=%d name=%r",
                channel_id,
                anchor_message_id,
                name,
                exc_info=True,
            )
            raise

        logger.info(
            "created a2a anchored thread channel_id=%d anchor_message_id=%d thread_id=%d name=%r",
            channel_id,
            anchor_message_id,
            thread.id,
            name,
        )
        return thread.id

    async def _discover(self) -> int | None:
        """Look for an existing unified channel by name. Returns its ID or None.

        Discord errors (e.g. ``discord.Forbidden`` if the bot loses
        guild access, ``HTTPException`` on Discord 5xx) propagate to
        the caller; we log them here so the failure surfaces in
        resolver-side logs, not just at the downstream call site.
        """
        try:
            guild = await self._sender.client.fetch_guild(self._guild_id)
            channels = await guild.fetch_channels()
        except discord.DiscordException:
            logger.warning(
                "a2a unified-channel discovery failed name=%s",
                self._channel_name,
                exc_info=True,
            )
            raise
        for channel in channels:
            if isinstance(channel, discord.TextChannel) and channel.name == self._channel_name:
                logger.info(
                    "resolved a2a unified channel name=%s id=%s",
                    self._channel_name,
                    channel.id,
                )
                return channel.id
        return None

    async def _create(self) -> int:
        """Create the unified channel with default permissions.

        ``discord.Forbidden`` (no Manage Channels permission),
        ``HTTPException``, and any other Discord error propagate to the
        caller. Logged here so the resolver-side log records the cause,
        mirroring :meth:`_discover`.

        If ``category_name`` was configured, the channel is placed under
        the resolved category (created lazily on first miss).
        """
        try:
            category = await self._resolve_category()
            guild = await self._sender.client.fetch_guild(self._guild_id)
            channel = await guild.create_text_channel(
                name=self._channel_name,
                category=category,
                reason="calfkit unified a2a audit channel",
            )
        except discord.DiscordException:
            logger.warning(
                "a2a unified-channel creation failed name=%s",
                self._channel_name,
                exc_info=True,
            )
            raise
        logger.info(
            "created a2a unified channel name=%s id=%s category_id=%s",
            self._channel_name,
            channel.id,
            category.id if category else None,
        )
        return channel.id

    async def _resolve_category(self) -> discord.CategoryChannel | None:
        """Return the configured A2A category, discovering or creating it.

        Returns ``None`` (and short-circuits with no Discord I/O) when
        no ``category_name`` was supplied at construction — the original
        "uncategorized at root" behavior.

        On first miss, scans the guild for a :class:`discord.CategoryChannel`
        whose name matches, creating it if none exists. The result is
        cached for the resolver's lifetime: a missing category is created
        at most once per process, and subsequent channel creations reuse
        the cached object with no additional Discord roundtrips.

        Discord errors (``Forbidden`` if the bot lacks Manage Channels,
        ``HTTPException`` on 5xx) propagate to the caller and are logged
        here, mirroring :meth:`_discover` and :meth:`_create`.
        """
        if self._category_name is None:
            return None
        if self._category is not None:
            return self._category
        try:
            guild = await self._sender.client.fetch_guild(self._guild_id)
            channels = await guild.fetch_channels()
            for channel in channels:
                if isinstance(channel, discord.CategoryChannel) and channel.name == self._category_name:
                    logger.info(
                        "resolved a2a category name=%s id=%s",
                        self._category_name,
                        channel.id,
                    )
                    self._category = channel
                    return channel
            category = await guild.create_category(
                name=self._category_name,
                reason="calfkit a2a channel category",
            )
        except discord.DiscordException:
            logger.warning(
                "a2a category resolution failed name=%s",
                self._category_name,
                exc_info=True,
            )
            raise
        logger.info(
            "created a2a category name=%s id=%s",
            self._category_name,
            category.id,
        )
        self._category = category
        return category
