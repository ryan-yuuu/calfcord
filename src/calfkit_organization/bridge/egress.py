"""Agent-to-agent channel resolution.

Provides a small helper for agents that want to message another agent.
Channels are named deterministically as ``a2a-{x}-{y}`` with the two agent
IDs sorted alphabetically, so any pair has exactly one canonical channel
regardless of who initiates contact. The resolver caches name → channel-ID
lookups in-memory; on cache miss it queries Discord, and on full miss it
creates the channel with default permissions (per locked decision #13).

The resolver intentionally does not validate agent identities against a
registry: callers run in deployments that may not have an
:class:`AgentRegistry` (e.g. the ``calfkit-tools`` process, which can't
read ``agents/*.md``). Caller code is expected to validate ids against
a phonebook or registry before reaching here; the resolver only refuses
the degenerate self-pair.
"""

from __future__ import annotations

import logging

import discord

from calfkit_organization.discord.sender import DiscordSender

logger = logging.getLogger(__name__)


class A2AChannelResolver:
    """Resolves (and lazily creates) the relationship channel between two agents."""

    def __init__(
        self,
        sender: DiscordSender,
        guild_id: int,
    ) -> None:
        self._sender = sender
        self._guild_id = guild_id
        self._cache: dict[tuple[str, str], int] = {}

    async def resolve_or_create(self, agent_a_id: str, agent_b_id: str) -> int:
        """Return the channel ID of the ``a2a-{x}-{y}`` channel for this pair.

        Pair canonicalization: the two IDs are sorted alphabetically before
        lookup, so ``("scheduler", "finance")`` and ``("finance",
        "scheduler")`` map to the same channel.

        Raises:
            ValueError: If both agent ids are equal.
            discord.Forbidden: If the bot lacks ``Manage Channels`` in the
                guild and the channel needs to be created.
        """
        pair = self._canonical_pair(agent_a_id, agent_b_id)
        if pair in self._cache:
            return self._cache[pair]

        channel_id = await self._discover(pair)
        if channel_id is None:
            channel_id = await self._create(pair)
        self._cache[pair] = channel_id
        return channel_id

    @staticmethod
    def _canonical_pair(a: str, b: str) -> tuple[str, str]:
        if a == b:
            raise ValueError(f"agent cannot have an a2a channel with itself: {a!r}")
        first, second = sorted([a, b])
        return first, second

    @staticmethod
    def _channel_name(pair: tuple[str, str]) -> str:
        return f"a2a-{pair[0]}-{pair[1]}"

    async def _discover(self, pair: tuple[str, str]) -> int | None:
        """Look for an existing channel by name. Returns its ID or None.

        Discord errors (e.g. ``discord.Forbidden`` if the bot loses guild
        access, ``HTTPException`` on Discord 5xx) propagate to the caller;
        we log them here so the failure surfaces in resolver-side logs,
        not just at the downstream call site that has to cross-reference
        which pair was being resolved.
        """
        name = self._channel_name(pair)
        try:
            guild = await self._sender.client.fetch_guild(self._guild_id)
            channels = await guild.fetch_channels()
        except discord.DiscordException:
            logger.warning(
                "a2a channel discovery failed pair=%s name=%s",
                pair,
                name,
                exc_info=True,
            )
            raise
        for channel in channels:
            if isinstance(channel, discord.TextChannel) and channel.name == name:
                logger.info("resolved a2a channel name=%s id=%s", name, channel.id)
                return channel.id
        return None

    async def _create(self, pair: tuple[str, str]) -> int:
        """Create the channel with default permissions. No overwrites.

        ``discord.Forbidden`` (no Manage Channels permission), ``HTTPException``,
        and any other Discord error propagate to the caller. Logged here
        so the resolver-side log records the cause, mirroring
        :meth:`_discover`.
        """
        name = self._channel_name(pair)
        try:
            guild = await self._sender.client.fetch_guild(self._guild_id)
            channel = await guild.create_text_channel(
                name=name,
                reason=f"calfkit a2a channel for agents {pair[0]} and {pair[1]}",
            )
        except discord.DiscordException:
            logger.warning(
                "a2a channel creation failed pair=%s name=%s",
                pair,
                name,
                exc_info=True,
            )
            raise
        logger.info("created a2a channel name=%s id=%s", name, channel.id)
        return channel.id
