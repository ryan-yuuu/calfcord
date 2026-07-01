"""Unit tests for the operator slash commands (``/thinking-effort`` + ``/clear``).

Post-0.12 the :class:`SlashCommandManager` is registry-free and control-plane-free:
``/thinking-effort`` writes a per-agent effort tier straight through an injected
:class:`~calfcord.bridge.overrides.EffortOverrides` (persisted in SQLite, D-8) —
there is no in-memory registry to optimistically mutate and nothing published to
Kafka. ``agent`` is free text (no live roster to build a choice list from). The
callbacks are exercised via the internal ``_on_thinking_effort`` / ``_on_clear``
entry points so no live ``discord.Interaction`` or network sync is needed; the
fake interaction is a ``SimpleNamespace`` with ``AsyncMock`` response methods.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord

from calfcord.bridge.history import CLEAR_MARKER_TEXT
from calfcord.bridge.slash import (
    _CLEAR_COMMAND_NAME,
    _THINKING_EFFORT_COMMAND_NAME,
    _THINKING_EFFORT_VALUES,
    SlashCommandManager,
)

_OWNER_USER_ID = 9999


class _FakeOverrides:
    """A stand-in for :class:`EffortOverrides` recording ``set()`` / ``clear()``.

    Tracks the persisted ``{agent → tier}`` state plus the two call lists, so a
    test can assert which method the slash callback invoked and the resulting row.
    When ``fail`` is set both writes raise, standing in for a store write error so
    the callback's guarded "couldn't save" path is observable.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.sets: list[tuple[str, str]] = []
        self.clears: list[str] = []
        self.state: dict[str, str] = {}
        self._fail = fail

    async def set(self, agent_id: str, effort: str) -> None:
        if self._fail:
            raise RuntimeError("store write failed")
        self.sets.append((agent_id, effort))
        self.state[agent_id] = effort

    async def clear(self, agent_id: str) -> None:
        if self._fail:
            raise RuntimeError("store write failed")
        self.clears.append(agent_id)
        self.state.pop(agent_id, None)


def _fake_discord_client() -> MagicMock:
    """A ``discord.Client`` mock that doesn't trip ``CommandTree``'s duplicate-tree guard."""
    client = MagicMock()
    client._connection._command_tree = None
    return client


def _make_manager(
    *,
    owner_user_id: int | None = _OWNER_USER_ID,
    guild_id: int | None = None,
    overrides: _FakeOverrides | None = None,
) -> tuple[SlashCommandManager, _FakeOverrides]:
    ovr = overrides or _FakeOverrides()
    manager = SlashCommandManager(
        client=_fake_discord_client(),
        overrides=ovr,  # type: ignore[arg-type]
        owner_user_id=owner_user_id,
        guild_id=guild_id,
    )
    return manager, ovr


def _interaction(*, user_id: int = _OWNER_USER_ID) -> Any:
    """A fake interaction for ``/thinking-effort`` with an ``AsyncMock`` reply."""
    response = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=user_id, name="alice", display_name="alice")
    return SimpleNamespace(id=42, user=user, response=response)


def _clear_interaction(*, user_id: int = _OWNER_USER_ID, channel: Any = None) -> Any:
    """A fake interaction for ``/clear`` — adds ``channel`` plus the silent-ack mocks.

    Carries ``response.defer`` + ``delete_original_response`` so the success path's
    silent ack (defer ephemerally, then delete the placeholder) is observable;
    guard/error paths still use ``response.send_message``.
    """
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    user = SimpleNamespace(id=user_id, name="alice", display_name="alice")
    return SimpleNamespace(
        id=42,
        user=user,
        response=response,
        channel=channel,
        delete_original_response=AsyncMock(),
    )


def _httpexception(status: int = 500) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="x")
    return discord.HTTPException(response, "synthetic")  # type: ignore[arg-type]


class TestRegister:
    """Both operator commands land on the tree with the right shape."""

    def test_register_adds_both_commands(self) -> None:
        manager, _ = _make_manager()
        manager.register_thinking_effort()
        manager.register_clear()
        te = manager._tree.get_command(_THINKING_EFFORT_COMMAND_NAME)
        cl = manager._tree.get_command(_CLEAR_COMMAND_NAME)
        assert te is not None and te.name == _THINKING_EFFORT_COMMAND_NAME
        assert cl is not None and cl.name == _CLEAR_COMMAND_NAME

    def test_thinking_effort_has_free_text_agent_and_effort_choices(self) -> None:
        manager, _ = _make_manager()
        manager.register_thinking_effort()
        te = manager._tree.get_command(_THINKING_EFFORT_COMMAND_NAME)
        assert set(te._params) == {"agent", "effort"}
        # ``agent`` is free text: no live roster to build a choice list from.
        assert te._params["agent"].type == discord.AppCommandOptionType.string
        # ``effort`` is a bounded choice of the thinking-effort tiers.
        assert tuple(c.value for c in te._params["effort"].choices) == _THINKING_EFFORT_VALUES

    def test_clear_takes_no_parameters(self) -> None:
        manager, _ = _make_manager()
        manager.register_clear()
        cl = manager._tree.get_command(_CLEAR_COMMAND_NAME)
        assert cl._params == {}


class TestThinkingEffortAuthorization:
    async def test_non_owner_is_rejected_no_write(self) -> None:
        manager, overrides = _make_manager()
        interaction = _interaction(user_id=_OWNER_USER_ID + 1)
        await manager._on_thinking_effort(interaction, "scribe", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "owner" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        assert overrides.sets == []

    async def test_owner_unset_permits_any_caller(self) -> None:
        manager, overrides = _make_manager(owner_user_id=None)
        interaction = _interaction(user_id=123456)
        await manager._on_thinking_effort(interaction, "scribe", "low")
        assert overrides.sets == [("scribe", "low")]


class TestThinkingEffortWrite:
    async def test_valid_effort_lower_cases_agent_and_persists(self) -> None:
        manager, overrides = _make_manager()
        interaction = _interaction()
        # Mixed-case + surrounding whitespace: the key must be normalized so it
        # matches the lower-cased mention the handler resolves.
        await manager._on_thinking_effort(interaction, "  Scribe  ", "high")

        assert overrides.sets == [("scribe", "high")]
        assert overrides.clears == []  # a real tier writes through set(), never clear()
        msg = interaction.response.send_message.call_args[0][0]
        assert "set" in msg.lower()
        assert "scribe" in msg and "high" in msg

    async def test_effort_none_clears_row_and_replies_cleared(self) -> None:
        manager, overrides = _make_manager()
        # Seed an existing override so we can prove ``none`` removes the row.
        overrides.state["scribe"] = "high"
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "none")

        # ``none`` means "no override": it clears the row rather than persisting a
        # ``none`` tier, so the persisted state agrees with the "Cleared" reply.
        assert overrides.clears == ["scribe"]
        assert overrides.sets == []
        assert "scribe" not in overrides.state
        msg = interaction.response.send_message.call_args[0][0]
        assert "cleared" in msg.lower()

    async def test_unknown_effort_rejected_no_write(self) -> None:
        manager, overrides = _make_manager()
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "bananas")

        msg, kwargs = interaction.response.send_message.call_args
        assert "unknown effort" in msg[0].lower() and "bananas" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        assert overrides.sets == []

    async def test_blank_agent_rejected_no_write(self) -> None:
        manager, overrides = _make_manager()
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "   ", "high")

        msg = interaction.response.send_message.call_args[0][0]
        assert "specify" in msg.lower()
        assert overrides.sets == []

    async def test_store_write_failure_replies_with_error(self) -> None:
        manager, _ = _make_manager(overrides=_FakeOverrides(fail=True))
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        msg, kwargs = interaction.response.send_message.call_args
        assert "couldn't save" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_reply_http_failure_is_swallowed(self) -> None:
        # The ack is best-effort: a Discord rejection when sending the reply must
        # not escape (there is nothing actionable left to do).
        manager, overrides = _make_manager()
        interaction = _interaction()
        interaction.response.send_message = AsyncMock(side_effect=_httpexception())
        await manager._on_thinking_effort(interaction, "scribe", "high")
        # The override still persisted before the (failed) ack.
        assert overrides.sets == [("scribe", "high")]


class TestClear:
    """``/clear`` is owner-gated and posts the per-channel context marker."""

    async def test_non_owner_rejected_no_marker(self) -> None:
        manager, _ = _make_manager()
        channel = SimpleNamespace(id=12345, send=AsyncMock())
        interaction = _clear_interaction(user_id=_OWNER_USER_ID + 1, channel=channel)
        await manager._on_clear(interaction)

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "owner" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        channel.send.assert_not_awaited()

    async def test_owner_posts_marker_and_silently_acks(self) -> None:
        manager, _ = _make_manager()
        channel = SimpleNamespace(id=12345, send=AsyncMock())
        interaction = _clear_interaction(channel=channel)
        await manager._on_clear(interaction)

        channel.send.assert_awaited_once_with(CLEAR_MARKER_TEXT)
        # Success posts only the public marker; the interaction is acked silently
        # (deferred ephemerally, then the placeholder deleted).
        interaction.response.send_message.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()

    async def test_owner_unset_permits_any_caller(self) -> None:
        manager, _ = _make_manager(owner_user_id=None)
        channel = SimpleNamespace(id=12345, send=AsyncMock())
        interaction = _clear_interaction(user_id=123456, channel=channel)
        await manager._on_clear(interaction)
        channel.send.assert_awaited_once_with(CLEAR_MARKER_TEXT)

    async def test_no_channel_replies_ephemeral_no_send(self) -> None:
        manager, _ = _make_manager()
        interaction = _clear_interaction(channel=None)
        await manager._on_clear(interaction)

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "channel" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_marker_http_failure_reports_not_cleared(self) -> None:
        manager, _ = _make_manager()
        channel = SimpleNamespace(id=12345, send=AsyncMock(side_effect=_httpexception()))
        interaction = _clear_interaction(channel=channel)
        await manager._on_clear(interaction)

        channel.send.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "not cleared" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_marker_unexpected_error_reports_not_cleared(self) -> None:
        # A non-HTTPException from ``channel.send`` (e.g. a connector error) must
        # still surface "not cleared" rather than escape into the dispatcher.
        manager, _ = _make_manager()
        channel = SimpleNamespace(id=12345, send=AsyncMock(side_effect=RuntimeError("connector died")))
        interaction = _clear_interaction(channel=channel)
        await manager._on_clear(interaction)

        channel.send.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "not cleared" in msg[0].lower()
        assert kwargs.get("ephemeral") is True


class TestSync:
    """``sync`` pushes the tree to Discord (guild-scoped or global)."""

    async def test_guild_sync_copies_global_then_syncs(self) -> None:
        manager, _ = _make_manager(guild_id=5678)
        manager._tree.copy_global_to = MagicMock()  # type: ignore[method-assign]
        manager._tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]

        await manager.sync(5678)

        manager._tree.copy_global_to.assert_called_once()
        manager._tree.sync.assert_awaited_once()
        # Guild-scoped: the sync targets a concrete guild object, not global.
        assert manager._tree.sync.await_args.kwargs["guild"] is not None

    async def test_global_sync_skips_copy(self) -> None:
        manager, _ = _make_manager(guild_id=None)
        manager._tree.copy_global_to = MagicMock()  # type: ignore[method-assign]
        manager._tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]

        await manager.sync(None)

        manager._tree.copy_global_to.assert_not_called()
        assert manager._tree.sync.await_args.kwargs["guild"] is None
