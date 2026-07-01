"""Unit tests for :class:`DiscordPersonaSender` identity helpers.

:meth:`DiscordPersonaSender.owns_webhook` is the predicate the history fetcher
uses to recognize an agent turn (R-A3): a fetched message whose ``webhook_id``
is one of this sender's persona webhooks came from a bridge persona post (its
username *is* the agent name under C8), so it is stamped as a ModelResponse.
Matching is by id — not by display name, not against a live roster — so it is
liveness-independent. These tests inject fake webhooks into the sender's cache
and check membership without touching Discord.
"""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr

from calfcord.discord.persona import DiscordPersonaSender
from calfcord.discord.settings import DiscordSettings


def _settings() -> DiscordSettings:
    """Minimal valid settings (only the two required fields)."""
    return DiscordSettings(bot_token=SecretStr("test-bot-token"), application_id=1234)


class TestOwnsWebhook:
    def test_recognizes_own_cached_webhooks(self) -> None:
        """True for any id in the sender's cached webhook set. ``_webhooks`` is
        keyed by channel id; each value is a ``discord.Webhook`` look-alike
        exposing only the ``.id`` the predicate reads."""
        sender = DiscordPersonaSender(_settings())
        sender._webhooks = {
            111: SimpleNamespace(id=999),
            222: SimpleNamespace(id=888),
        }
        assert sender.owns_webhook(999) is True
        assert sender.owns_webhook(888) is True

    def test_rejects_foreign_webhook(self) -> None:
        """False for an id that is not one of the sender's webhooks — a
        third-party webhook is never mis-read as an agent."""
        sender = DiscordPersonaSender(_settings())
        sender._webhooks = {
            111: SimpleNamespace(id=999),
            222: SimpleNamespace(id=888),
        }
        assert sender.owns_webhook(123) is False

    def test_empty_sender_owns_nothing(self) -> None:
        """A sender that has not discovered/created any webhook this process
        lifetime owns nothing (agent history in a channel degrades to
        human-attributed until the first persona send there)."""
        sender = DiscordPersonaSender(_settings())
        assert sender.owns_webhook(999) is False
