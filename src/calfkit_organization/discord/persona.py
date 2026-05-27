"""Persona-based Discord sender backed by per-channel webhooks.

A :class:`Persona` is a display identity (name + avatar). The
:class:`DiscordPersonaSender` projects a chosen persona onto each
message by routing through a per-channel webhook and overriding
``username`` and ``avatar_url`` on every send. This is the same
mechanism that powers PluralKit and Tupperbox: many identities, one
underlying bot.

The webhook itself has no intrinsic connection to personas — it is
just our project's outbound write channel into a given Discord text
channel. We name webhooks after the bot/project, not the use case, so
the bot can recognize and reuse its own webhooks across restarts.

The bot user must have the ``Manage Webhooks`` permission in any
channel where this sender is used.

**Inline replies caveat.** Discord webhooks cannot produce real
``type: 19`` reply messages — the ``message_reference`` field is
silently dropped on webhook execute. See
https://github.com/discord/discord-api-docs/issues/2251. When a caller
passes :class:`ReplyContext`, the sender approximates the inline-reply
UI by attaching a small embed (author + truncated snippet + jump link)
above the message, matching PluralKit's approach.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self

import discord

from calfkit_organization.discord.avatar import dicebear_avatar_url
from calfkit_organization.discord.messages import SentMessage
from calfkit_organization.discord.settings import DiscordSettings

__all__ = [
    "DiscordPersonaSender",
    "Persona",
    "ReplyContext",
    "ReplyStyle",
    "dicebear_avatar_url",
]

if TYPE_CHECKING:
    from calfkit_organization.bridge.wire import WireMessage

logger = logging.getLogger(__name__)

# Marker name on every webhook this sender creates so the bot can
# recognize its own webhooks across process restarts. Owner-focused
# (matches the bot/project), not use-case-focused.
_WEBHOOK_NAME = "calfkit"

# Reason recorded in Discord's audit log when we create a webhook.
_AUDIT_REASON = "calfkit-organization persona sender"

# Embed author-name and button-label visual limits. The hard Discord caps
# are higher (256 chars for embed author name, 80 for button labels) but
# we truncate well below to keep both styles on a single visual line.
_EMBED_SNIPPET_MAX_LEN = 60
_BUTTON_LABEL_MAX_LEN = 80

ReplyStyle = Literal["embed", "button"]


@dataclass(frozen=True, slots=True)
class Persona:
    """A display identity to project through a webhook on send.

    Attributes:
        name: Display name shown in Discord. 1-80 characters. Discord
            rejects the literal name "Clyde".
        avatar_url: Public URL to an image for the persona's avatar.
            When ``None``, the underlying webhook's default avatar is used.
    """

    name: str
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class ReplyContext:
    """Context for rendering a faked inline reply.

    Discord webhooks cannot produce real ``type: 19`` reply messages
    (see module docstring). Two approximation styles are supported via
    the ``style`` field:

    - ``"embed"`` (default, PluralKit-style): a small embed above-or-below
      the message with the original author's avatar, name, content
      snippet, and a clickable jump link.
    - ``"button"`` (Connections-Bot-style): a single Link button below
      the message labelled e.g. "↩ Replying to @user" that opens the
      original. Less visually noisy than an embed; requires a click to
      see the original.

    Attributes:
        message_id: Discord ID of the message being replied to.
        channel_id: Channel ID of that message (used to build the jump URL).
        guild_id: Guild ID (used to build the jump URL).
        author_display_name: Display name of the original author.
        content_snippet: The original message content; truncated by the
            sender before embedding.
        author_avatar_url: Avatar URL of the original author. Rendered
            as the embed icon when ``style="embed"``; ignored when
            ``style="button"`` (Discord link buttons cannot show user
            avatars).
        style: Which UI element to render the reply as. See class doc.
    """

    message_id: int
    channel_id: int
    guild_id: int
    author_display_name: str
    content_snippet: str
    author_avatar_url: str | None = None
    style: ReplyStyle = "embed"

    @classmethod
    def from_wire(cls, wire: WireMessage, style: ReplyStyle = "button") -> Self:
        """Build a :class:`ReplyContext` from a :class:`WireMessage`.

        Used wherever an agent replies inline to the inbound event that
        triggered it. The default ``style="button"`` matches the reply
        rendering used across the project's agents.
        """
        return cls(
            message_id=wire.message_id,
            channel_id=wire.channel_id,
            guild_id=wire.guild_id,
            author_display_name=wire.author.display_name,
            content_snippet=wire.content,
            author_avatar_url=wire.author.avatar_url,
            style=style,
        )


def _jump_url(reply_to: ReplyContext) -> str:
    """Discord deep-link to a specific message in a guild channel."""
    return (
        f"https://discord.com/channels/"
        f"{reply_to.guild_id}/{reply_to.channel_id}/{reply_to.message_id}"
    )


def _truncate(text: str, max_len: int) -> str:
    """Collapse whitespace and truncate to ``max_len`` with an ellipsis.

    Both embed author lines and button labels render newlines/extra
    whitespace literally, so we collapse first to keep visuals on one line.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1].rstrip() + "…"


def _build_reply_embed(reply_to: ReplyContext) -> discord.Embed:
    """Build a single-line PluralKit-style inline-reply embed.

    The whole author line acts as a jump link to the original message
    (Discord's "click the message link to jump" UX). The color stripe
    uses the Discord dark-theme channel color so it blends with the
    background and reads as metadata rather than as a prominent embed
    card — closer to a real inline-reply badge than a content embed.
    The original author's avatar is rendered as the icon next to the
    author line when present.
    """
    snippet = _truncate(reply_to.content_snippet, _EMBED_SNIPPET_MAX_LEN)
    embed = discord.Embed(color=discord.Color.dark_theme())
    # Compose the visible reply line. ``↪`` evokes Discord's native reply
    # marker. The whole line links to the original via the author URL.
    label = f"↪ {reply_to.author_display_name}"
    if snippet:
        label = f"{label}: {snippet}"
    if reply_to.author_avatar_url is not None:
        embed.set_author(name=label, url=_jump_url(reply_to), icon_url=reply_to.author_avatar_url)
    else:
        embed.set_author(name=label, url=_jump_url(reply_to))
    return embed


def _build_reply_button(reply_to: ReplyContext) -> discord.ui.View:
    """Build a single Link-button View labelled "↩ Replying to @user: <snippet>".

    Connections-Bot-style. Renders as one rounded button below the
    message; clicking opens the original via the same Discord
    deep-link the embed uses. Link buttons do not generate
    interactions, so no callback handler or persistent view is needed.

    The label includes the original message's content snippet (collapsed
    to one line, truncated to fit Discord's 80-char button-label limit)
    so the recipient sees what's being replied to without clicking.
    When the original message has no content (e.g. attachment-only),
    the snippet is omitted and only the author line remains.

    Requires the webhook to be application-owned (which any webhook
    created by our bot via ``channel.create_webhook`` is); generic
    incoming webhooks created via Discord's UI cannot carry components.
    """
    base = f"↩ Replying to @{reply_to.author_display_name}"
    # Collapse whitespace in the snippet before composition so the final
    # truncation operates on a clean single-line string. _truncate handles
    # the actual cap and ellipsis insertion.
    snippet = " ".join(reply_to.content_snippet.split())
    raw_label = f"{base}: {snippet}" if snippet else base
    label = _truncate(raw_label, _BUTTON_LABEL_MAX_LEN)
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.link,
            url=_jump_url(reply_to),
            label=label,
        )
    )
    return view


class DiscordPersonaSender:
    """Send messages under arbitrary :class:`Persona` identities.

    Discovers or creates one webhook per channel on first send and
    caches it for the sender's lifetime. Subsequent sends to the same
    channel reuse the cached webhook. Discovery is serialized with an
    asyncio lock so concurrent first-sends to the same channel cannot
    create duplicate webhooks.

    Use as an async context manager for automatic cleanup, or call
    :meth:`start` and :meth:`close` explicitly for long-lived instances.

    Example::

        aksel = Persona(name="Aksel", avatar_url="https://example.com/aksel.png")
        async with DiscordPersonaSender(settings) as personas:
            await personas.send(aksel, channel_id=123, content="Hello.")
    """

    def __init__(self, settings: DiscordSettings) -> None:
        self._settings = settings
        self._client: discord.Client | None = None
        self._webhooks: dict[int, discord.Webhook] = {}
        self._discovery_lock = asyncio.Lock()

    @property
    def client(self) -> discord.Client:
        """Return the authenticated REST client.

        Raises :class:`RuntimeError` if :meth:`start` has not been awaited.
        Exposed so deployments sharing this REST connection (e.g. the
        tools process's thread-history reader) don't reach into the
        underscore-prefixed attribute.
        """
        if self._client is None:
            raise RuntimeError(
                "DiscordPersonaSender not started; call start() or use as an async context manager."
            )
        return self._client

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Authenticate against Discord's REST API. Idempotent."""
        if self._client is not None:
            return
        # Intents.none() — we never connect to the gateway, only REST.
        client = discord.Client(intents=discord.Intents.none())
        await client.login(self._settings.bot_token.get_secret_value())
        self._client = client
        logger.info("DiscordPersonaSender authenticated")

    async def close(self) -> None:
        """Close the HTTP session and clear the webhook cache. Idempotent."""
        if self._client is None:
            return
        await self._client.close()
        self._client = None
        self._webhooks.clear()
        logger.info("DiscordPersonaSender closed")

    async def send(
        self,
        persona: Persona,
        channel_id: int,
        content: str,
        *,
        thread_id: int | None = None,
        reply_to: ReplyContext | None = None,
    ) -> SentMessage:
        """Send a message rendered under ``persona``'s identity.

        Args:
            persona: Display name and avatar to render the message under.
            channel_id: ID of the *parent* text channel that hosts the
                webhook. The bot must have View Channel and Manage
                Webhooks permissions here.
            content: Plain message text. Discord's 2000-character limit applies.
            thread_id: When set, posts into this thread inside ``channel_id``.
                The webhook still lives on the parent channel.
            reply_to: When set, prepends a PluralKit-style embed that
                visually approximates an inline reply. Discord webhooks
                cannot produce real ``type: 19`` reply messages, so this
                embed (author + snippet + jump link) is the closest
                possible UX. See :class:`ReplyContext`.

        Returns:
            :class:`SentMessage`. Its ``channel_id`` field is ``thread_id``
            when set, otherwise ``channel_id`` — i.e. where the message
            actually lives.

        Raises:
            RuntimeError: If :meth:`start` has not been called.
            TypeError: If ``channel_id`` does not refer to a text channel.
            discord.Forbidden: If the bot lacks ``Manage Webhooks`` and a
                webhook does not yet exist in the channel.
            discord.HTTPException: For other Discord-side failures.
        """
        if self._client is None:
            raise RuntimeError(
                "DiscordPersonaSender not started; call start() or use as an async context manager."
            )

        webhook = await self._get_or_create_webhook(channel_id)

        # discord.utils.MISSING is the library's "argument omitted" sentinel.
        # Passing None would explicitly clear the field; MISSING means
        # "use the webhook's default" (which is what we want when the
        # persona has no avatar override).
        thread = discord.Object(id=thread_id) if thread_id is not None else discord.utils.MISSING
        avatar = persona.avatar_url if persona.avatar_url is not None else discord.utils.MISSING

        embeds: Any = discord.utils.MISSING
        view: Any = discord.utils.MISSING
        if reply_to is not None:
            if reply_to.style == "embed":
                embeds = [_build_reply_embed(reply_to)]
            else:  # "button"
                view = _build_reply_button(reply_to)

        sent = await webhook.send(
            content=content,
            username=persona.name,
            avatar_url=avatar,
            thread=thread,
            embeds=embeds,
            view=view,
            wait=True,  # required so the response carries the message ID
        )

        message_channel = thread_id if thread_id is not None else channel_id
        logger.debug(
            "sent persona message id=%s persona=%s channel=%s reply=%s",
            sent.id,
            persona.name,
            message_channel,
            reply_to.message_id if reply_to is not None else None,
        )
        return SentMessage(id=sent.id, channel_id=message_channel)

    async def _get_or_create_webhook(self, channel_id: int) -> discord.Webhook:
        """Return our webhook for ``channel_id``, discovering or creating as needed."""
        cached = self._webhooks.get(channel_id)
        if cached is not None:
            return cached

        async with self._discovery_lock:
            # Re-check inside the lock: a peer task may have populated
            # the cache while we were waiting on it.
            cached = self._webhooks.get(channel_id)
            if cached is not None:
                return cached

            client = self._client
            assert client is not None, "internal: send() guarded that client is set"
            bot_user = client.user
            assert bot_user is not None, "internal: client.user is set after login()"

            channel = await self._fetch_text_channel(client, channel_id)

            for hook in await channel.webhooks():
                if (
                    hook.name == _WEBHOOK_NAME
                    and hook.user is not None
                    and hook.user.id == bot_user.id
                ):
                    logger.info(
                        "reusing existing webhook id=%s in channel=%s",
                        hook.id,
                        channel_id,
                    )
                    self._webhooks[channel_id] = hook
                    return hook

            new_hook = await channel.create_webhook(name=_WEBHOOK_NAME, reason=_AUDIT_REASON)
            logger.info("created webhook id=%s in channel=%s", new_hook.id, channel_id)
            self._webhooks[channel_id] = new_hook
            return new_hook

    @staticmethod
    async def _fetch_text_channel(client: discord.Client, channel_id: int) -> discord.TextChannel:
        """Fetch a TextChannel by ID, raising ``TypeError`` if it isn't one."""
        channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise TypeError(
                f"Channel {channel_id} is a {type(channel).__name__}, not a TextChannel; "
                "webhooks require a parent text channel. To post in a thread, pass the "
                "thread's ID via thread_id and the parent channel's ID via channel_id."
            )
        return channel
