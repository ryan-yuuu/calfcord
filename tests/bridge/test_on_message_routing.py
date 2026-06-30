"""Post-router-removal ``_on_message`` routing behaviour.

After the ambient router and the plaintext ``/task`` command are removed
(C2/C3), the gateway's ``_on_message`` routes only @mentions and slash
commands. A ``/task ...`` message is no longer intercepted — it normalizes
like any other text (``kind="message"``) and the ingress drops it. Plain
ambient text likewise flows to ``ingress.handle`` and is dropped there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from calfcord.agents.definition import AgentDefinition
from calfcord.bridge.gateway import DiscordIngressGateway
from calfcord.bridge.ingress import BridgeIngress
from calfcord.bridge.normalizer import MessageNormalizer
from calfcord.bridge.registry import AgentRegistry
from calfcord.discord.settings import DiscordSettings
from pydantic import SecretStr

_GUILD_ID = 5678
_PARENT_CHANNEL_ID = 6789
_USER_MESSAGE_ID = 11111
_USER_ID = 42
_BOT_USER_ID = 555


def _settings() -> DiscordSettings:
    return DiscordSettings(
        bot_token=SecretStr("test-bot-token"),
        application_id=1234,
        guild_id=_GUILD_ID,
        owner_user_id=9999,
    )


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


def _gateway() -> tuple[DiscordIngressGateway, MagicMock]:
    """A gateway with a real normalizer and a mock ingress.

    Returns ``(gateway, ingress)``. Constructed offline; the
    post-``_on_ready`` state (``_bot_user_id``, ``_message_normalizer``) is
    set by hand so ``_on_message`` runs its real control flow.
    """
    registry = _registry()
    ingress = MagicMock(spec=BridgeIngress)
    ingress.handle = AsyncMock()
    gateway = DiscordIngressGateway(
        settings=_settings(),
        ingress=ingress,
        registry=registry,
        calfkit_client=MagicMock(),
        transcript_store=MagicMock(),
    )
    gateway._bot_user_id = _BOT_USER_ID
    gateway._message_normalizer = MessageNormalizer(
        registry=registry,
        bot_user_id=_BOT_USER_ID,
        human_owner_id=_settings().owner_user_id,
    )
    return gateway, ingress


def _message(*, content: str) -> MagicMock:
    """A human ``discord.Message`` stand-in for ``_on_message``."""
    message = MagicMock(spec=discord.Message)
    message.id = _USER_MESSAGE_ID
    message.content = content
    message.webhook_id = None
    message.created_at = datetime.now(UTC)

    guild = MagicMock()
    guild.id = _GUILD_ID
    message.guild = guild

    author = MagicMock()
    author.id = _USER_ID
    author.bot = False
    author.name = "alice"
    author.display_name = "alice"
    author.display_avatar = SimpleNamespace(url="https://example.com/alice.png")
    message.author = author

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _PARENT_CHANNEL_ID
    channel.parent_id = None
    message.channel = channel
    message.create_thread = AsyncMock()
    message.reply = AsyncMock()
    return message


class TestOnMessageRouting:
    async def test_task_message_is_not_intercepted_and_creates_no_thread(self) -> None:
        """``/task ...`` is no longer a command (C3): the gateway opens no
        thread and the message flows through normal routing as ambient text."""
        gateway, ingress = _gateway()
        message = _message(content="/task summarize the design doc")

        await gateway._on_message(message)

        message.create_thread.assert_not_awaited()
        # It flows through as an ordinary (ambient) message.
        ingress.handle.assert_awaited_once()
        wire = ingress.handle.await_args.args[0]
        assert wire.kind == "message"

    async def test_mention_routes_as_slash(self) -> None:
        """An @mention still routes to the targeted agent (unchanged)."""
        gateway, ingress = _gateway()
        message = _message(content="@scribe take a note")

        await gateway._on_message(message)

        message.create_thread.assert_not_awaited()
        ingress.handle.assert_awaited_once()
        wire = ingress.handle.await_args.args[0]
        assert wire.kind == "slash"
        assert wire.slash_target == "scribe"
