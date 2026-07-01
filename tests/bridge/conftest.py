"""Shared fixtures for bridge tests.

Hand-built fakes for ``discord.Message``. Each fake is a plain
``SimpleNamespace`` carrying only the attributes the normalizer / gateway
read. Production code duck-types (``getattr`` / attribute access) rather than
``isinstance``, so a namespace is sufficient and keeps these tests offline —
no live discord.py connection or Kafka broker.

Post-0.12 the bridge holds no ``AgentRegistry`` (mention targeting resolves
against the live mesh at dispatch time), so the old ``agent_registry`` /
``fake_interaction`` fixtures are gone with the registry- and
``SlashNormalizer``-based tests that used them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest


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
