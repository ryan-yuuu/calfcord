"""Unit tests for the plaintext ``/task`` command.

``/task`` is NOT a Discord slash command. It is a plaintext command
(``/task <text>``) detected by the gateway's ``_on_message`` so the task's
opening message is genuinely authored by the user — a slash interaction can
only post the anchor as the bot or a webhook. The gateway opens a public
thread anchored on the user's own message and routes the message ambiently so
the router summons the agents; their replies and live-step progress land in
the new thread (the thread-aware reply path is exercised in ``test_outbox.py``
/ ``test_steps.py``).

These tests drive the gateway's internal ``_maybe_handle_task`` entry point
with a fake ``discord.Message`` (``MagicMock(spec=discord.Message)`` +
``AsyncMock``s) and a ``MagicMock(spec=BridgeIngress)`` ingress. The
normalizer is a real :class:`MessageNormalizer` so the hand-built wire is
asserted end-to-end. The pure helpers ``_parse_task_command`` and
``_thread_name_from_text`` are tested directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import SecretStr

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.gateway import (
    DiscordIngressGateway,
    _parse_task_command,
    _thread_name_from_text,
)
from calfkit_organization.bridge.ingress import AmbientRosterEmptyError, BridgeIngress
from calfkit_organization.bridge.normalizer import MessageNormalizer
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.discord.settings import DiscordSettings

_GUILD_ID = 5678
_PARENT_CHANNEL_ID = 6789
_THREAD_ID = 22222
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


def _gateway(ingress: MagicMock | None = None) -> tuple[DiscordIngressGateway, MagicMock]:
    """Build a gateway with a real :class:`MessageNormalizer` and mock ingress.

    Returns ``(gateway, ingress)``. The gateway is constructed offline (the
    ``discord.Client`` constructor is sync and does not connect), and we set
    the post-``_on_ready`` state (``_bot_user_id``, ``_message_normalizer``)
    by hand so ``_maybe_handle_task`` runs against a real normalizer.
    """
    registry = _registry()
    ingress = ingress or MagicMock(spec=BridgeIngress)
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


def _text_channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _PARENT_CHANNEL_ID
    channel.parent_id = None  # top-level channel
    return channel


def _thread() -> SimpleNamespace:
    return SimpleNamespace(
        id=_THREAD_ID,
        jump_url=f"https://discord.com/channels/{_GUILD_ID}/{_THREAD_ID}/0",
    )


def _message(
    *,
    content: str,
    channel: object | None = None,
    author_id: int = _USER_ID,
    is_bot: bool = False,
    webhook_id: int | None = None,
) -> MagicMock:
    """A ``discord.Message`` stand-in for ``_maybe_handle_task``.

    ``create_thread`` returns a thread; ``reply`` is an ``AsyncMock`` so
    usage/error replies can be asserted.
    """
    message = MagicMock(spec=discord.Message)
    message.id = _USER_MESSAGE_ID
    message.content = content
    message.webhook_id = webhook_id
    message.created_at = datetime.now(UTC)

    guild = MagicMock()
    guild.id = _GUILD_ID
    message.guild = guild

    author = MagicMock()
    author.id = author_id
    author.bot = is_bot
    author.name = "alice"
    author.display_name = "alice"
    author.display_avatar = SimpleNamespace(url="https://example.com/alice.png")
    message.author = author

    message.channel = _text_channel() if channel is None else channel
    message.create_thread = AsyncMock(return_value=_thread())
    message.reply = AsyncMock()
    return message


def _httpexception(status: int = 403) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="x")
    return discord.HTTPException(response, "synthetic")  # type: ignore[arg-type]


class TestParseTaskCommand:
    def test_bare_task_is_command_with_no_body(self) -> None:
        assert _parse_task_command("/task") == (True, None)

    def test_bare_task_trailing_whitespace_has_no_body(self) -> None:
        assert _parse_task_command("/task   ") == (True, None)

    def test_task_with_text(self) -> None:
        assert _parse_task_command("/task fix the bug") == (True, "fix the bug")

    def test_non_task_message(self) -> None:
        assert _parse_task_command("hello world") == (False, None)

    def test_task_token_must_be_bare(self) -> None:
        # "/taskfoo" is a different word, not the /task command.
        assert _parse_task_command("/taskfoo") == (False, None)

    def test_case_insensitive(self) -> None:
        assert _parse_task_command("/TASK do it") == (True, "do it")

    def test_multiline_body_is_captured_whole(self) -> None:
        # DOTALL: the full body is captured; whitespace is collapsed only when
        # deriving the thread title, not here.
        assert _parse_task_command("/task line1\nline2") == (True, "line1\nline2")

    def test_mention_mid_sentence_is_not_a_command(self) -> None:
        assert _parse_task_command("please run /task later") == (False, None)

    def test_token_then_only_newline_has_no_body(self) -> None:
        # "/task" then Enter (no body) — the realistic bare-task case via the
        # composer. DOTALL means \n is consumed by \s+, body strips to None.
        assert _parse_task_command("/task\n") == (True, None)

    def test_tab_separator_is_accepted(self) -> None:
        # \s+ matches a tab, not just a space.
        assert _parse_task_command("/task\tfix it") == (True, "fix it")

    def test_whitespace_only_multiline_body_has_no_body(self) -> None:
        assert _parse_task_command("/task   \n  ") == (True, None)

    def test_leading_whitespace_before_token_is_not_a_command(self) -> None:
        # Anchored at start: a leading space means it is not the command.
        assert _parse_task_command("  /task do it") == (False, None)


class TestThreadNameFromText:
    def test_short_text_passes_through(self) -> None:
        assert _thread_name_from_text("Fix the bug") == "Fix the bug"

    def test_collapses_whitespace(self) -> None:
        assert _thread_name_from_text("  fix   the\n\nbug ") == "fix the bug"

    def test_truncates_long_text_with_ellipsis(self) -> None:
        name = _thread_name_from_text("x" * 250)
        assert len(name) == 100
        assert name.endswith("…")

    def test_exactly_max_len_passes_through(self) -> None:
        # 100 chars is the cap; it must pass through unchanged, no ellipsis.
        text = "x" * 100
        name = _thread_name_from_text(text)
        assert name == text
        assert len(name) == 100
        assert not name.endswith("…")

    def test_one_over_max_len_truncates_to_cap(self) -> None:
        # 101 chars must be truncated to exactly the 100-char cap (Discord
        # hard-rejects longer names) — guards the off-by-one in the slice.
        name = _thread_name_from_text("x" * 101)
        assert len(name) == 100
        assert name.endswith("…")

    def test_empty_falls_back(self) -> None:
        assert _thread_name_from_text("   ") == "Task"


class TestNormalizeTaskWire:
    """The wire built for a ``/task`` must be ambient and thread-scoped: the
    parent channel hosts the webhook/topic, the new thread receives replies,
    and the anchor is the user's own message carrying the full text."""

    def test_builds_ambient_thread_wire(self) -> None:
        normalizer = MessageNormalizer(
            registry=_registry(), bot_user_id=_BOT_USER_ID, human_owner_id=None
        )
        message = _message(content="/task do the thing")
        wire = normalizer.normalize_task(message, thread_id=_THREAD_ID)

        # Always ambient — the router decides respondents.
        assert wire.kind == "message"
        assert wire.slash_target is None
        # Replies/steps post into the thread (source); webhook hosts on parent.
        assert wire.channel_id == _PARENT_CHANNEL_ID
        assert wire.source_channel_id == _THREAD_ID
        assert wire.thread_id == _THREAD_ID
        # Anchor is the user's own message; content is the full text (prefix kept).
        assert wire.message_id == _USER_MESSAGE_ID
        assert wire.content == "/task do the thing"
        assert wire.guild_id == _GUILD_ID
        # Genuinely user-authored — not a bot or webhook.
        assert wire.author.discord_user_id == _USER_ID
        assert wire.author.is_bot is False
        assert wire.author.is_webhook is False

    def test_preserves_message_created_at(self) -> None:
        # created_at is forwarded from the message (not stamped with now()), so
        # the wire orders correctly in history alongside other channel events.
        normalizer = MessageNormalizer(
            registry=_registry(), bot_user_id=_BOT_USER_ID, human_owner_id=None
        )
        message = _message(content="/task do the thing")
        wire = normalizer.normalize_task(message, thread_id=_THREAD_ID)
        assert wire.created_at == message.created_at

    def test_marks_owner_authored_task(self) -> None:
        # A task started by the configured human owner carries is_human_owner.
        owner_id = 9999
        normalizer = MessageNormalizer(
            registry=_registry(), bot_user_id=_BOT_USER_ID, human_owner_id=owner_id
        )
        message = _message(content="/task do the thing", author_id=owner_id)
        wire = normalizer.normalize_task(message, thread_id=_THREAD_ID)
        assert wire.author.is_human_owner is True

    def test_dm_raises(self) -> None:
        normalizer = MessageNormalizer(
            registry=_registry(), bot_user_id=_BOT_USER_ID, human_owner_id=None
        )
        message = _message(content="/task x")
        message.guild = None
        with pytest.raises(ValueError, match="DM"):
            normalizer.normalize_task(message, thread_id=_THREAD_ID)


class TestMaybeHandleTaskHappyPath:
    async def test_opens_thread_and_routes_ambiently(self) -> None:
        gateway, ingress = _gateway()
        message = _message(content="/task do the thing")

        owned = await gateway._maybe_handle_task(message)

        assert owned is True
        # Thread opened off the user's own message, titled from the body.
        message.create_thread.assert_awaited_once()
        assert message.create_thread.call_args.kwargs["name"] == "do the thing"
        # Routed ambiently with a thread-scoped wire built from the message.
        ingress.handle.assert_awaited_once()
        wire = ingress.handle.call_args.args[0]
        assert wire.kind == "message"
        assert wire.channel_id == _PARENT_CHANNEL_ID
        assert wire.source_channel_id == _THREAD_ID
        assert wire.message_id == _USER_MESSAGE_ID
        assert wire.content == "/task do the thing"
        # No error/usage reply on the happy path.
        message.reply.assert_not_awaited()


class TestMaybeHandleTaskPassThrough:
    """When ``_maybe_handle_task`` does not own the message it returns False and
    touches nothing, so the message flows through normal routing."""

    async def test_non_task_message_is_not_owned(self) -> None:
        gateway, ingress = _gateway()
        message = _message(content="just a normal message")

        owned = await gateway._maybe_handle_task(message)

        assert owned is False
        message.create_thread.assert_not_awaited()
        ingress.handle.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_webhook_author_task_is_not_owned(self) -> None:
        """A persona webhook posting "/task ..." must NOT open a thread — it
        falls through so peers still see it as an ordinary message."""
        gateway, ingress = _gateway()
        message = _message(content="/task do the thing", webhook_id=999, is_bot=True)

        owned = await gateway._maybe_handle_task(message)

        assert owned is False
        message.create_thread.assert_not_awaited()
        ingress.handle.assert_not_awaited()

    async def test_bot_author_task_is_not_owned(self) -> None:
        gateway, ingress = _gateway()
        message = _message(content="/task do the thing", is_bot=True)

        owned = await gateway._maybe_handle_task(message)

        assert owned is False
        message.create_thread.assert_not_awaited()
        ingress.handle.assert_not_awaited()


class TestMaybeHandleTaskGuardsAndFailures:
    async def test_bare_task_replies_with_usage(self) -> None:
        gateway, ingress = _gateway()
        message = _message(content="/task")

        owned = await gateway._maybe_handle_task(message)

        assert owned is True
        message.create_thread.assert_not_awaited()
        ingress.handle.assert_not_awaited()
        message.reply.assert_awaited_once()
        assert "usage" in message.reply.await_args.args[0].lower()

    async def test_rejects_when_not_a_text_channel(self) -> None:
        """Inside a thread (or a forum/voice channel) ``/task`` is rejected
        before opening anything."""
        gateway, ingress = _gateway()
        thread_channel = MagicMock(spec=discord.Thread)
        thread_channel.id = _THREAD_ID
        message = _message(content="/task do the thing", channel=thread_channel)

        owned = await gateway._maybe_handle_task(message)

        assert owned is True
        message.create_thread.assert_not_awaited()
        ingress.handle.assert_not_awaited()
        assert "text channel" in message.reply.await_args.args[0].lower()

    async def test_thread_create_failure_reports_and_stops(self) -> None:
        gateway, ingress = _gateway()
        message = _message(content="/task do the thing")
        message.create_thread = AsyncMock(side_effect=_httpexception())

        owned = await gateway._maybe_handle_task(message)

        assert owned is True
        ingress.handle.assert_not_awaited()
        assert "couldn't open" in message.reply.await_args.args[0].lower()

    async def test_empty_roster_reports_thread_but_no_agents(self) -> None:
        gateway, ingress = _gateway()
        ingress.handle.side_effect = AmbientRosterEmptyError(
            event_id="evt", channel_id=_PARENT_CHANNEL_ID
        )
        message = _message(content="/task do the thing")

        owned = await gateway._maybe_handle_task(message)

        assert owned is True
        # Thread was created (jump link present) but the task can't be worked.
        reply = message.reply.await_args.args[0]
        assert str(_THREAD_ID) in reply
        assert "no assistant" in reply.lower()

    async def test_generic_ingress_failure_reports(self) -> None:
        gateway, ingress = _gateway()
        ingress.handle.side_effect = RuntimeError("broker down")
        message = _message(content="/task do the thing")

        owned = await gateway._maybe_handle_task(message)

        assert owned is True
        reply = message.reply.await_args.args[0]
        assert str(_THREAD_ID) in reply
        # The thread already exists, so the reply must NOT advise a plain retry
        # (which would create a duplicate thread).
        assert "duplicate" in reply.lower()

    async def test_pre_ready_replies_and_owns(self) -> None:
        # If a /task somehow reaches the handler before the gateway is ready
        # (normalizer unset), it must not silently vanish: log + tell the user
        # to retry, and still claim ownership (no normal routing).
        gateway, ingress = _gateway()
        gateway._message_normalizer = None
        message = _message(content="/task do the thing")

        owned = await gateway._maybe_handle_task(message)

        assert owned is True
        message.create_thread.assert_not_awaited()
        ingress.handle.assert_not_awaited()
        assert "starting up" in message.reply.await_args.args[0].lower()

    async def test_reply_swallows_non_http_discord_error(self) -> None:
        # _reply_best_effort is the sole user-feedback sink for every /task
        # failure. A non-HTTP DiscordException (e.g. the socket dropping during
        # the same turbulence that triggered the failure) must be swallowed
        # here, not escape into discord.py's dispatcher to be silently dropped.
        gateway, _ingress = _gateway()
        message = _message(content="/task do the thing")
        message.reply = AsyncMock(side_effect=discord.ClientException("socket closed"))

        # Must not raise.
        await gateway._reply_best_effort(message, "anything")
        message.reply.assert_awaited_once()


class TestOnMessageDivertsTasks:
    """``_on_message`` must divert a ``/task`` to ``_maybe_handle_task`` and
    NOT also route it normally; a non-task message must still be normalized."""

    async def test_task_message_is_not_double_routed(self) -> None:
        gateway, _ingress = _gateway()
        # Spy on the real normalizer to prove normal routing is skipped.
        gateway._message_normalizer.normalize = MagicMock(  # type: ignore[method-assign]
            wraps=gateway._message_normalizer.normalize
        )
        message = _message(content="/task do the thing")

        await gateway._on_message(message)

        message.create_thread.assert_awaited_once()
        # Diverted: the normal normalize() path was never taken for this message.
        gateway._message_normalizer.normalize.assert_not_called()

    async def test_non_task_message_routes_normally(self) -> None:
        gateway, ingress = _gateway()
        message = _message(content="hello there")

        await gateway._on_message(message)

        message.create_thread.assert_not_awaited()
        # Normal ambient routing happened.
        ingress.handle.assert_awaited_once()
        wire = ingress.handle.call_args.args[0]
        assert wire.content == "hello there"
        assert wire.kind == "message"

    async def test_redelivered_task_does_not_spawn_second_thread(self) -> None:
        # discord.py can redeliver MESSAGE_CREATE on gateway reconnect. The
        # /task divert is checked AFTER the _already_seen dedupe precisely so a
        # redelivery can't open a second thread (and double-spend on the LLM).
        # This pins that ordering invariant against a future reorder.
        gateway, ingress = _gateway()
        message = _message(content="/task do the thing")

        await gateway._on_message(message)  # first delivery: opens the thread
        await gateway._on_message(message)  # reconnect redelivery: same id

        message.create_thread.assert_awaited_once()
        ingress.handle.assert_awaited_once()
