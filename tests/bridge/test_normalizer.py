"""Unit tests for MessageNormalizer and SlashNormalizer."""

from __future__ import annotations

import pytest

from calfkit_organization.bridge.normalizer import (
    MessageNormalizer,
    SlashNormalizer,
    UnknownAgentMentionError,
)
from calfkit_organization.bridge.registry import AgentRegistry

_BOT_USER_ID = 99
_OWNER_USER_ID = 1234


class TestMessageNormalizer:
    def test_top_level_message_from_human_owner(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        msg = fake_message(
            channel_id=200,
            author_id=_OWNER_USER_ID,
            author_name="ryan",
            content="hi all",
        )
        wire = normalizer.normalize(msg)
        assert wire.kind == "message"
        assert wire.slash_target is None
        assert wire.channel_id == 200
        assert wire.message_id == msg.id
        assert wire.content == "hi all"
        assert wire.author.is_human_owner is True
        assert wire.author.is_bot is False
        assert wire.author.is_webhook is False
        assert wire.author.agent_id is None

    def test_thread_message_collapses_to_parent_channel(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        msg = fake_message(channel_id=500, thread_parent_id=200)
        wire = normalizer.normalize(msg)
        assert wire.channel_id == 200, "thread messages must use the parent channel ID"

    def test_persona_webhook_resolves_agent_id(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        msg = fake_message(
            author_name="anything",
            author_display_name="Aksel (Scheduler)",
            author_is_bot=True,
            webhook_id=777,
        )
        wire = normalizer.normalize(msg)
        assert wire.author.is_webhook is True
        assert wire.author.webhook_id == 777
        assert wire.author.is_bot is True
        assert wire.author.agent_id == "scheduler"

    def test_webhook_with_unknown_display_name(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        msg = fake_message(
            author_display_name="Mystery Bot",
            author_is_bot=True,
            webhook_id=888,
        )
        wire = normalizer.normalize(msg)
        assert wire.author.is_webhook is True
        assert wire.author.agent_id is None

    def test_bot_own_non_webhook_message(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        msg = fake_message(
            author_id=_BOT_USER_ID,
            author_name="calfkit-bot",
            author_is_bot=True,
            webhook_id=None,
        )
        wire = normalizer.normalize(msg)
        assert wire.author.is_bot is True
        assert wire.author.is_webhook is False
        assert wire.author.agent_id is None
        assert wire.author.is_human_owner is False

    def test_dm_raises(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        msg = fake_message(guild_id=None)
        with pytest.raises(ValueError, match="DM"):
            normalizer.normalize(msg)

    def test_non_owner_human_is_not_owner(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        msg = fake_message(author_id=_OWNER_USER_ID + 1)
        wire = normalizer.normalize(msg)
        assert wire.author.is_human_owner is False

    def test_owner_set_to_none_means_no_one_is_owner(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, human_owner_id=None)
        msg = fake_message(author_id=_OWNER_USER_ID)
        wire = normalizer.normalize(msg)
        assert wire.author.is_human_owner is False


class TestMentionParsing:
    """@<agent_id> prefix detection in plain channel messages."""

    def test_plain_message_no_mention(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="hello world"))
        assert wire.kind == "message"
        assert wire.slash_target is None
        assert wire.content == "hello world"

    def test_at_known_agent_marks_as_slash(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="@scheduler book me a haircut"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"
        # The prefix is intentionally retained in content.
        assert wire.content == "@scheduler book me a haircut"

    def test_at_unknown_agent_raises(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        with pytest.raises(UnknownAgentMentionError) as exc_info:
            normalizer.normalize(fake_message(content="@unknown_agent hi"))
        assert exc_info.value.unknown_names == ["unknown_agent"]

    def test_at_router_mention_treated_as_unknown(self, fake_message):
        """The built-in router agent is registered (so the bridge can
        find it for routing) but is NOT user-invocable via @-mention.
        ``@_router foo`` surfaces as ``UnknownAgentMentionError`` so
        the gateway sends the standard fail-fast reply rather than
        silently routing to a topic with no consumer."""
        from calfkit_organization.agents.definition import AgentDefinition

        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    slash="/scheduler",
                    display_name="Aksel",
                    description="x",
                    system_prompt="x",
                ),
                AgentDefinition(
                    agent_id="_router",
                    slash="/_router",
                    display_name="Router",
                    description="Internal routing agent",
                    role="router",
                    publish_topic="routing.decisions",
                    system_prompt="x",
                ),
            ]
        )
        normalizer = MessageNormalizer(registry, _BOT_USER_ID, _OWNER_USER_ID)
        with pytest.raises(UnknownAgentMentionError) as exc_info:
            normalizer.normalize(fake_message(content="@_router hello"))
        assert exc_info.value.unknown_names == ["_router"]

    def test_mention_is_case_insensitive(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="@SCHEDULER hi"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"

    def test_mention_anywhere_in_message(self, agent_registry, fake_message):
        """@<agent> need not be the first token; it just needs to start a token."""
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="hey @scheduler what's up"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"
        assert wire.content == "hey @scheduler what's up"

    def test_leading_whitespace_is_allowed(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="   @scheduler hi"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"

    def test_punctuation_after_mention_still_matches(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="@scheduler: book a meeting"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"

    def test_mention_only_no_message(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="@scheduler"))
        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"
        assert wire.content == "@scheduler"

    def test_bare_at_sign_is_message(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(fake_message(content="@ hi"))
        assert wire.kind == "message"
        assert wire.slash_target is None

    def test_email_address_is_not_a_mention(self, agent_registry, fake_message):
        """Embedded @s (e.g. emails) are excluded — they don't start a token."""
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(
            fake_message(content="please email me at foo@bar.com thanks")
        )
        assert wire.kind == "message"
        assert wire.slash_target is None

    def test_multiple_mentions_all_known_uses_first_as_target(
        self, agent_registry, fake_message
    ):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        wire = normalizer.normalize(
            fake_message(content="@scheduler please coordinate with @finance")
        )
        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"

    def test_multiple_mentions_with_one_unknown_raises(
        self, agent_registry, fake_message
    ):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        with pytest.raises(UnknownAgentMentionError) as exc_info:
            normalizer.normalize(
                fake_message(content="@scheduler please ping @nonexistent_agent")
            )
        assert exc_info.value.unknown_names == ["nonexistent_agent"]

    def test_multiple_unknown_mentions_all_reported(self, agent_registry, fake_message):
        normalizer = MessageNormalizer(agent_registry, _BOT_USER_ID, _OWNER_USER_ID)
        with pytest.raises(UnknownAgentMentionError) as exc_info:
            normalizer.normalize(fake_message(content="@foo and @bar"))
        assert exc_info.value.unknown_names == ["foo", "bar"]


class TestSlashNormalizer:
    def test_slash_in_top_level_channel(self, agent_registry, fake_interaction):
        normalizer = SlashNormalizer(agent_registry, _OWNER_USER_ID)
        interaction = fake_interaction(channel_id=200, user_id=_OWNER_USER_ID)
        spec = agent_registry.by_id("scheduler")
        assert spec is not None

        wire = normalizer.normalize(
            interaction=interaction,
            slash_target=spec,
            message_arg="book me a haircut",
            followup_message_id=42,
        )

        assert wire.kind == "slash"
        assert wire.slash_target == "scheduler"
        assert wire.message_id == 42, "message_id should be the followup ID, not the interaction ID"
        assert wire.channel_id == 200
        assert wire.content == "book me a haircut"
        assert wire.author.is_human_owner is True
        assert wire.author.is_webhook is False

    def test_slash_inside_thread_uses_parent_channel(self, agent_registry, fake_interaction):
        normalizer = SlashNormalizer(agent_registry, _OWNER_USER_ID)
        interaction = fake_interaction(channel_id=500, thread_parent_id=200)
        spec = agent_registry.by_id("scheduler")
        assert spec is not None

        wire = normalizer.normalize(
            interaction=interaction,
            slash_target=spec,
            message_arg="x",
            followup_message_id=42,
        )

        assert wire.channel_id == 200

    def test_slash_from_non_owner(self, agent_registry, fake_interaction):
        normalizer = SlashNormalizer(agent_registry, _OWNER_USER_ID)
        interaction = fake_interaction(user_id=_OWNER_USER_ID + 1)
        spec = agent_registry.by_id("scheduler")
        assert spec is not None

        wire = normalizer.normalize(
            interaction=interaction,
            slash_target=spec,
            message_arg="x",
            followup_message_id=42,
        )

        assert wire.author.is_human_owner is False
