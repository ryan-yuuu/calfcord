"""Shared fixtures for bridge tests.

Hand-built fakes for ``discord.Message`` / ``discord.Interaction`` etc. Each
fake is a plain ``SimpleNamespace`` carrying only the attributes the
normalizer reads. The normalizer uses duck-typing (``getattr`` / attribute
access) rather than ``isinstance``, so this is sufficient.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.registry import AgentRegistry


@pytest.fixture
def agent_registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                slash="/scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar.",
                system_prompt="Test scheduler.",
            ),
            AgentDefinition(
                agent_id="finance",
                slash="/finance",
                display_name="Finn (Finance)",
                description="Bookkeeping.",
                system_prompt="Test finance.",
            ),
        ]
    )


@pytest.fixture
def fake_message():
    """Factory: build a ``SimpleNamespace`` that quacks like ``discord.Message``."""

    def _build(
        *,
        message_id: int = 1000,
        channel_id: int = 2000,
        thread_parent_id: int | None = None,
        guild_id: int | None = 3000,
        author_id: int = 4000,
        author_name: str = "alice",
        author_display_name: str | None = None,
        author_is_bot: bool = False,
        author_avatar_url: str = "https://cdn.discordapp.com/embed/avatars/0.png",
        webhook_id: int | None = None,
        content: str = "hello",
        created_at: datetime | None = None,
    ) -> Any:
        if thread_parent_id is not None:
            channel = SimpleNamespace(id=channel_id, parent_id=thread_parent_id)
        else:
            channel = SimpleNamespace(id=channel_id)
        guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        author = SimpleNamespace(
            id=author_id,
            name=author_name,
            display_name=author_display_name or author_name,
            bot=author_is_bot,
            display_avatar=SimpleNamespace(url=author_avatar_url),
        )
        return SimpleNamespace(
            id=message_id,
            channel=channel,
            guild=guild,
            author=author,
            webhook_id=webhook_id,
            content=content,
            created_at=created_at or datetime.now(UTC),
        )

    return _build


@pytest.fixture
def fake_interaction():
    """Factory: build a ``SimpleNamespace`` that quacks like ``discord.Interaction``."""

    def _build(
        *,
        interaction_id: int = 5000,
        channel_id: int = 2000,
        thread_parent_id: int | None = None,
        guild_id: int = 3000,
        user_id: int = 4000,
        user_name: str = "alice",
        user_display_name: str | None = None,
        user_is_bot: bool = False,
        user_avatar_url: str = "https://cdn.discordapp.com/embed/avatars/0.png",
        created_at: datetime | None = None,
    ) -> Any:
        if thread_parent_id is not None:
            channel = SimpleNamespace(id=channel_id, parent_id=thread_parent_id)
        else:
            channel = SimpleNamespace(id=channel_id)
        user = SimpleNamespace(
            id=user_id,
            name=user_name,
            display_name=user_display_name or user_name,
            bot=user_is_bot,
            display_avatar=SimpleNamespace(url=user_avatar_url),
        )
        return SimpleNamespace(
            id=interaction_id,
            channel=channel,
            guild_id=guild_id,
            guild=SimpleNamespace(id=guild_id),
            user=user,
            created_at=created_at or datetime.now(UTC),
        )

    return _build
