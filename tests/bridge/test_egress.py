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


def _category(*, name: str, channel_id: int) -> MagicMock:
    """Build a ``discord.CategoryChannel``-shaped mock."""
    cat = MagicMock(spec=discord.CategoryChannel)
    cat.name = name
    cat.id = channel_id
    return cat


def _text_channel(*, name: str, channel_id: int) -> MagicMock:
    text = MagicMock(spec=discord.TextChannel)
    text.name = name
    text.id = channel_id
    return text


def _real_body_resolver(
    *,
    category_name: str | None = None,
    existing_channels: list | None = None,
    created_text_id: int = 999,
    created_category_id: int = 12345,
) -> tuple[A2AChannelResolver, MagicMock, MagicMock]:
    """Build a resolver wired to a mocked discord client chain.

    Unlike :func:`_resolver`, this fixture exercises the real bodies of
    ``_create`` and ``_resolve_category`` — the helpers it returns let
    tests assert on calls to ``create_text_channel`` / ``create_category``.
    """
    created_text = _text_channel(name="placeholder", channel_id=created_text_id)
    created_category = _category(name=category_name or "", channel_id=created_category_id)
    guild = MagicMock()
    guild.fetch_channels = AsyncMock(return_value=existing_channels or [])
    guild.create_text_channel = AsyncMock(return_value=created_text)
    guild.create_category = AsyncMock(return_value=created_category)
    sender = MagicMock(spec=DiscordSender)
    sender.client = MagicMock()
    sender.client.fetch_guild = AsyncMock(return_value=guild)
    resolver = A2AChannelResolver(sender, guild_id=42, category_name=category_name)
    return resolver, guild, created_category


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

    def test_category_name_is_keyword_only(self) -> None:
        """Positional ``category_name`` would silently shift any future
        third positional arg; pin keyword-only at the signature level."""
        sender = MagicMock(spec=DiscordSender)
        with pytest.raises(TypeError):
            A2AChannelResolver(sender, 42, "private-a2a")  # type: ignore[misc]

    def test_default_category_is_none(self) -> None:
        """Opt-in: omitting ``category_name`` keeps the pre-feature
        uncategorized-at-root behavior."""
        sender = MagicMock(spec=DiscordSender)
        resolver = A2AChannelResolver(sender, 42)
        assert resolver._category_name is None
        assert resolver._category is None


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


class TestCategoryResolution:
    """Behavior of ``_resolve_category`` and its interaction with
    ``_create``. Exercises the real method bodies rather than the
    public-API stubs used elsewhere in this file."""

    async def test_unconfigured_returns_none_without_io(self) -> None:
        """Default opt-out path must not touch Discord at all — the
        feature's zero-cost-when-unused promise lives here."""
        resolver, guild, _ = _real_body_resolver(category_name=None)
        category = await resolver._resolve_category()
        assert category is None
        guild.fetch_channels.assert_not_called()
        guild.create_category.assert_not_called()

    async def test_finds_existing_category_by_name(self) -> None:
        """When a category with the configured name already exists in
        the guild, reuse it rather than creating a duplicate."""
        existing = _category(name="private-a2a", channel_id=12345)
        resolver, guild, _ = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[existing],
        )
        result = await resolver._resolve_category()
        assert result is existing
        guild.create_category.assert_not_called()

    async def test_creates_category_when_missing(self) -> None:
        """Lazy creation: first call creates the category if no
        matching one exists in the guild."""
        resolver, guild, created_category = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[],
        )
        result = await resolver._resolve_category()
        assert result is created_category
        guild.create_category.assert_awaited_once()
        # The reason string is operator-facing audit-log context.
        kwargs = guild.create_category.await_args.kwargs
        assert kwargs["name"] == "private-a2a"
        assert "a2a" in kwargs["reason"].lower()

    async def test_existing_category_ignores_same_name_non_category(self) -> None:
        """A text/voice channel with the same name as the configured
        category must not be mistaken for the category — only
        ``discord.CategoryChannel`` instances match."""
        decoy = _text_channel(name="private-a2a", channel_id=55555)
        resolver, guild, created_category = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[decoy],
        )
        result = await resolver._resolve_category()
        assert result is created_category
        guild.create_category.assert_awaited_once()

    async def test_category_cached_across_calls(self) -> None:
        """Once resolved, subsequent invocations short-circuit without
        further Discord I/O — the resolver is intended to live for the
        process lifetime."""
        existing = _category(name="private-a2a", channel_id=12345)
        resolver, guild, _ = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[existing],
        )
        first = await resolver._resolve_category()
        second = await resolver._resolve_category()
        assert first is second
        # Only the first call should have hit the guild.
        assert guild.fetch_channels.await_count == 1
        assert resolver._sender.client.fetch_guild.await_count == 1

    async def test_category_create_forbidden_propagates(self) -> None:
        """Operator misconfiguration (no Manage Channels) must abort
        the A2A turn rather than silently fall back to root-level
        channel creation, which would defeat the category's purpose."""
        resolver, guild, _ = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[],
        )
        guild.create_category = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "manage channels")
        )
        with pytest.raises(discord.Forbidden):
            await resolver._resolve_category()


class TestCreateChannelUnderCategory:
    """``_create`` must pass the resolved category through to
    ``guild.create_text_channel``. Exercises the real ``_create`` body."""

    async def test_passes_category_when_configured(self) -> None:
        existing = _category(name="private-a2a", channel_id=12345)
        resolver, guild, _ = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[existing],
        )
        await resolver._create(("alice", "bob"))
        kwargs = guild.create_text_channel.await_args.kwargs
        assert kwargs["category"] is existing
        assert kwargs["name"] == "a2a-alice-bob"

    async def test_passes_none_when_unconfigured(self) -> None:
        """No category configured → ``category=None`` is passed
        explicitly so the channel lands at guild root."""
        resolver, guild, _ = _real_body_resolver(category_name=None)
        await resolver._create(("alice", "bob"))
        kwargs = guild.create_text_channel.await_args.kwargs
        assert kwargs["category"] is None

    async def test_creates_category_then_channel_on_full_cold_start(self) -> None:
        """End-to-end cold path: configured category, neither category
        nor channel exist → category created first, then channel placed
        under it."""
        resolver, guild, created_category = _real_body_resolver(
            category_name="private-a2a",
            existing_channels=[],
        )
        await resolver._create(("alice", "bob"))
        guild.create_category.assert_awaited_once()
        guild.create_text_channel.assert_awaited_once()
        kwargs = guild.create_text_channel.await_args.kwargs
        assert kwargs["category"] is created_category
