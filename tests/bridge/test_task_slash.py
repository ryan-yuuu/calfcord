"""Unit tests for the ``/task`` slash command.

``/task`` posts the supplied message into the invoking channel, opens a
public thread anchored on it, and routes the message ambiently so the
router summons the agents the task needs — whose replies and live-step
progress land in the new thread (the thread-aware reply path is exercised
in ``test_outbox.py`` / ``test_steps.py``).

The handler is driven via the internal ``_on_task`` entry point with a
fake ``discord.Interaction`` (``SimpleNamespace`` + ``AsyncMock``s), a
``MagicMock(spec=discord.TextChannel)`` channel, and a
``MagicMock(spec=BridgeIngress)`` ingress (whose async ``handle`` is an
``AsyncMock``). The normalizer is a real :class:`SlashNormalizer` so the
hand-built wire is asserted end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.ingress import AmbientRosterEmptyError, BridgeIngress
from calfkit_organization.bridge.normalizer import SlashNormalizer
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.slash import (
    _TASK_COMMAND_NAME,
    SlashCommandManager,
    _thread_name_from_text,
)

_CHANNEL_ID = 6789
_STARTER_ID = 11111
_THREAD_ID = 22222
_GUILD_ID = 12345
_USER_ID = 42


def _registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scribe",
                display_name="Scribe",
                description="Writer.",
                avatar_url="https://example.com/scribe.png",
                system_prompt="Test scribe.",
            ),
        ]
    )


def _fake_discord_client() -> MagicMock:
    """A discord.Client mock that doesn't trip CommandTree's duplicate-tree guard."""
    client = MagicMock()
    client._connection._command_tree = None
    return client


def _make_manager() -> tuple[SlashCommandManager, MagicMock]:
    """Build a manager with a real :class:`SlashNormalizer` and a mock ingress.

    Returns ``(manager, ingress)`` so tests can assert / configure
    ``ingress.handle`` (an ``AsyncMock`` via spec auto-detection)."""
    registry = _registry()
    ingress = MagicMock(spec=BridgeIngress)
    normalizer = SlashNormalizer(registry=registry, human_owner_id=None)
    manager = SlashCommandManager(
        client=_fake_discord_client(),
        registry=registry,
        ingress=ingress,
        slash_normalizer=normalizer,
        calfkit_client=MagicMock(),
        owner_user_id=None,
        guild_id=_GUILD_ID,
    )
    return manager, ingress


def _text_channel() -> MagicMock:
    """A ``discord.TextChannel`` mock that posts a starter and opens a thread."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _CHANNEL_ID
    channel.send = AsyncMock(return_value=SimpleNamespace(id=_STARTER_ID))
    thread = SimpleNamespace(
        id=_THREAD_ID,
        jump_url=f"https://discord.com/channels/{_GUILD_ID}/{_THREAD_ID}/0",
    )
    channel.create_thread = AsyncMock(return_value=thread)
    return channel


def _interaction(*, channel: Any) -> Any:
    user = SimpleNamespace(id=_USER_ID, name="alice", display_name="alice")
    return SimpleNamespace(
        id=999,
        user=user,
        channel=channel,
        guild_id=_GUILD_ID,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        created_at=datetime.now(UTC),
    )


def _httpexception(status: int = 403) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="x")
    return discord.HTTPException(response, "synthetic")  # type: ignore[arg-type]


class TestThreadNameFromText:
    def test_short_text_passes_through(self) -> None:
        assert _thread_name_from_text("Fix the bug") == "Fix the bug"

    def test_collapses_whitespace(self) -> None:
        assert _thread_name_from_text("  fix   the\n\nbug ") == "fix the bug"

    def test_truncates_long_text_with_ellipsis(self) -> None:
        name = _thread_name_from_text("x" * 250)
        assert len(name) == 100
        assert name.endswith("…")

    def test_empty_falls_back(self) -> None:
        assert _thread_name_from_text("   ") == "Task"


class TestNormalizeTask:
    def test_builds_ambient_thread_wire(self) -> None:
        normalizer = SlashNormalizer(registry=_registry(), human_owner_id=None)
        interaction = _interaction(channel=_text_channel())
        wire = normalizer.normalize_task(
            interaction,
            "do the thing",
            anchor_message_id=_STARTER_ID,
            thread_id=_THREAD_ID,
        )
        # Always ambient — the router decides respondents.
        assert wire.kind == "message"
        assert wire.slash_target is None
        # Replies/steps post into the thread (source), webhook hosts on parent.
        assert wire.channel_id == _CHANNEL_ID
        assert wire.source_channel_id == _THREAD_ID
        assert wire.thread_id == _THREAD_ID
        # Reply anchor is the just-posted starter; content is the raw message.
        assert wire.message_id == _STARTER_ID
        assert wire.content == "do the thing"
        assert wire.guild_id == _GUILD_ID
        assert wire.author.discord_user_id == _USER_ID
        assert wire.author.is_bot is False
        assert wire.author.is_webhook is False

    def test_no_channel_raises(self) -> None:
        normalizer = SlashNormalizer(registry=_registry(), human_owner_id=None)
        interaction = _interaction(channel=None)
        with pytest.raises(ValueError, match="no channel"):
            normalizer.normalize_task(
                interaction, "x", anchor_message_id=1, thread_id=2
            )


class TestRegister:
    def test_register_adds_task_command_to_tree(self) -> None:
        manager, _ = _make_manager()
        manager.register_task()
        cmd = manager._tree.get_command(_TASK_COMMAND_NAME)
        assert cmd is not None
        assert cmd.name == _TASK_COMMAND_NAME
        # Single text parameter.
        assert "message" in cmd._params


class TestOnTaskHappyPath:
    async def test_posts_thread_and_routes_ambiently(self) -> None:
        manager, ingress = _make_manager()
        channel = _text_channel()
        interaction = _interaction(channel=channel)

        await manager._on_task(interaction, "do the thing")

        # Deferred ephemerally before any slow Discord work.
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        # Starter posted verbatim as the thread anchor.
        channel.send.assert_awaited_once_with("do the thing")
        # Thread opened anchored on the starter, titled from the message.
        channel.create_thread.assert_awaited_once()
        ct_kwargs = channel.create_thread.call_args.kwargs
        assert ct_kwargs["name"] == "do the thing"
        assert ct_kwargs["message"].id == _STARTER_ID
        # Routed ambiently with a thread-scoped wire.
        ingress.handle.assert_awaited_once()
        wire = ingress.handle.call_args.args[0]
        assert wire.kind == "message"
        assert wire.source_channel_id == _THREAD_ID
        assert wire.channel_id == _CHANNEL_ID
        assert wire.message_id == _STARTER_ID
        assert wire.content == "do the thing"
        # Ephemeral confirmation carries the thread jump link.
        interaction.followup.send.assert_awaited_once()
        msg, kwargs = interaction.followup.send.call_args
        assert str(_THREAD_ID) in msg[0]
        assert kwargs.get("ephemeral") is True


class TestOnTaskGuardsAndFailures:
    async def test_rejects_when_not_a_text_channel(self) -> None:
        """Inside a thread (or a forum/voice channel) ``/task`` is rejected
        before posting anything."""
        manager, ingress = _make_manager()
        thread_channel = MagicMock(spec=discord.Thread)
        thread_channel.id = _THREAD_ID
        interaction = _interaction(channel=thread_channel)

        await manager._on_task(interaction, "do the thing")

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        msg, kwargs = interaction.followup.send.call_args
        assert "text channel" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        ingress.handle.assert_not_awaited()

    async def test_starter_post_failure_reports_and_stops(self) -> None:
        manager, ingress = _make_manager()
        channel = _text_channel()
        channel.send = AsyncMock(side_effect=_httpexception())
        interaction = _interaction(channel=channel)

        await manager._on_task(interaction, "do the thing")

        channel.create_thread.assert_not_awaited()
        ingress.handle.assert_not_awaited()
        msg, kwargs = interaction.followup.send.call_args
        assert "couldn't post" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_thread_create_failure_reports_after_posting(self) -> None:
        manager, ingress = _make_manager()
        channel = _text_channel()
        channel.create_thread = AsyncMock(side_effect=_httpexception())
        interaction = _interaction(channel=channel)

        await manager._on_task(interaction, "do the thing")

        # Message was posted, but the thread couldn't open; not routed.
        channel.send.assert_awaited_once()
        ingress.handle.assert_not_awaited()
        msg, kwargs = interaction.followup.send.call_args
        assert "thread" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_empty_roster_reports_thread_but_no_agents(self) -> None:
        manager, ingress = _make_manager()
        ingress.handle.side_effect = AmbientRosterEmptyError(
            event_id="evt", channel_id=_CHANNEL_ID
        )
        channel = _text_channel()
        interaction = _interaction(channel=channel)

        await manager._on_task(interaction, "do the thing")

        # Thread was created (jump link present) but the task can't be worked.
        msg, kwargs = interaction.followup.send.call_args
        assert str(_THREAD_ID) in msg[0]
        assert "no assistant" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_generic_ingress_failure_reports(self) -> None:
        manager, ingress = _make_manager()
        ingress.handle.side_effect = RuntimeError("broker down")
        channel = _text_channel()
        interaction = _interaction(channel=channel)

        await manager._on_task(interaction, "do the thing")

        msg, kwargs = interaction.followup.send.call_args
        assert str(_THREAD_ID) in msg[0]
        assert "something went wrong" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
