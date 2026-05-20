"""Unit tests for :class:`A2AChannelResolver`.

The resolver lost its ``registry`` constructor arg in the
decoupled-deployments refactor (callers now validate agent ids against
the wire-format phonebook before reaching here). These tests pin the
new contract: no registry, no unknown-id validation, but the self-pair
invariant and canonicalization remain.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from calfkit_organization.bridge.egress import A2AChannelResolver
from calfkit_organization.discord.sender import DiscordSender


def _resolver(*, found_channel_id: int | None = None) -> A2AChannelResolver:
    """Build a resolver whose discover() returns ``found_channel_id``.

    When ``found_channel_id`` is ``None`` we wire create() to return a
    fixed id so the full discover→create path is exercisable.
    """
    sender = MagicMock(spec=DiscordSender)
    resolver = A2AChannelResolver(sender, guild_id=42)
    resolver._discover = AsyncMock(return_value=found_channel_id)  # type: ignore[method-assign]
    resolver._create = AsyncMock(return_value=999)  # type: ignore[method-assign]
    return resolver


class TestConstructor:
    def test_accepts_only_sender_and_guild_id(self) -> None:
        """The registry param was intentionally removed when the tool
        deployment lost access to agents/*.md. Pin the signature so a
        future refactor that adds it back fails this test loudly."""
        sender = MagicMock(spec=DiscordSender)
        # Positional-only invocation matching the production runner's call
        # at tools/runner.py:_amain — `A2AChannelResolver(sender, settings.guild_id)`.
        resolver = A2AChannelResolver(sender, 42)
        assert resolver is not None


class TestResolveOrCreate:
    async def test_self_pair_raises_value_error(self) -> None:
        """The one validation the resolver still owns: ``(a, a)`` is
        nonsense regardless of where caller validation lives."""
        resolver = _resolver()
        with pytest.raises(ValueError, match="cannot have an a2a channel with itself"):
            await resolver.resolve_or_create("alice", "alice")

    async def test_unknown_agent_ids_no_longer_validated(self) -> None:
        """The resolver intentionally does NOT check that the agent ids
        exist — callers validate against the phonebook before reaching
        here. Pinning this prevents a future re-add of registry
        validation from silently breaking the tool deployment."""
        resolver = _resolver(found_channel_id=555)
        # Both ids are completely made up; resolver should not care.
        channel_id = await resolver.resolve_or_create("ghost-a", "ghost-b")
        assert channel_id == 555

    async def test_canonicalizes_pair_order(self) -> None:
        """``(b, a)`` and ``(a, b)`` must resolve to the same channel —
        the cache is keyed on the sorted pair, so out-of-order calls
        share a single underlying Discord channel."""
        resolver = _resolver(found_channel_id=777)
        first = await resolver.resolve_or_create("alice", "bob")
        second = await resolver.resolve_or_create("bob", "alice")
        assert first == second == 777
        # _discover should only have been called once thanks to the cache
        # hit on the canonicalized pair.
        assert resolver._discover.await_count == 1  # type: ignore[attr-defined]

    async def test_creates_channel_when_discover_returns_none(self) -> None:
        """When no existing channel matches the canonical name, the
        resolver falls through to creating it. The created id is cached
        for subsequent lookups of the same pair."""
        resolver = _resolver(found_channel_id=None)
        first = await resolver.resolve_or_create("alice", "bob")
        assert first == 999  # the _create mock's return
        # Second call hits cache; no further _create call.
        second = await resolver.resolve_or_create("alice", "bob")
        assert second == 999
        assert resolver._create.await_count == 1  # type: ignore[attr-defined]

    async def test_forbidden_from_create_propagates(self) -> None:
        """If the bot lacks Manage Channels and discover misses, the
        Forbidden from create must bubble out — projection has nowhere
        to land, so silent fallback would lose audit entries."""
        resolver = _resolver(found_channel_id=None)
        resolver._create = AsyncMock(  # type: ignore[method-assign]
            side_effect=discord.Forbidden(MagicMock(status=403), "manage channels")
        )
        with pytest.raises(discord.Forbidden):
            await resolver.resolve_or_create("alice", "bob")
