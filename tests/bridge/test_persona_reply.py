"""Unit tests for the PluralKit-style reply embed builder.

Discord webhooks cannot produce real ``type: 19`` reply messages (the
``message_reference`` field is silently dropped on webhook execute; see
https://github.com/discord/discord-api-docs/issues/2251). The persona
sender approximates the inline-reply UI with a small embed at the top
of the message; these tests pin the embed shape so the visual contract
doesn't silently regress.
"""

from __future__ import annotations

import pytest

import discord

from calfkit_organization.discord.persona import (
    ReplyContext,
    _build_reply_button,
    _build_reply_embed,
    _truncate,
)


class TestTruncate:
    def test_short_text_passes_through(self) -> None:
        assert _truncate("hello", 60) == "hello"

    def test_collapses_whitespace(self) -> None:
        assert _truncate("hello   world\n\nfoo", 60) == "hello world foo"

    def test_truncates_with_ellipsis(self) -> None:
        long = "a" * 100
        result = _truncate(long, 10)
        assert len(result) == 10
        assert result.endswith("…")

    def test_empty_string(self) -> None:
        assert _truncate("", 60) == ""

    def test_whitespace_only_collapses_to_empty(self) -> None:
        assert _truncate("   \n  \t  ", 60) == ""


class TestBuildReplyEmbed:
    @pytest.fixture
    def context(self) -> ReplyContext:
        return ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="hello world",
        )

    def test_embed_has_author_with_jump_url(self, context: ReplyContext) -> None:
        embed = _build_reply_embed(context)
        assert embed.author.url == "https://discord.com/channels/777/888/999"

    def test_embed_author_includes_arrow_name_and_snippet(self, context: ReplyContext) -> None:
        embed = _build_reply_embed(context)
        assert embed.author.name == "↪ alice: hello world"

    def test_embed_color_blends_with_dark_theme(self, context: ReplyContext) -> None:
        """Dark-theme color stripe blends with the channel background so the
        embed reads as a thin reply badge, not a prominent content card."""
        embed = _build_reply_embed(context)
        assert embed.color == discord.Color.dark_theme()

    def test_avatar_url_used_as_author_icon(self, context: ReplyContext) -> None:
        with_avatar = ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="hello",
            author_avatar_url="https://cdn.discordapp.com/avatars/123/abc.png",
        )
        embed = _build_reply_embed(with_avatar)
        assert embed.author.icon_url == "https://cdn.discordapp.com/avatars/123/abc.png"

    def test_no_avatar_url_omits_icon(self, context: ReplyContext) -> None:
        """When the wire didn't carry an avatar URL, the icon is left unset."""
        embed = _build_reply_embed(context)
        assert embed.author.icon_url is None

    def test_long_content_truncated(self, context: ReplyContext) -> None:
        long_context = ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="x" * 200,
        )
        embed = _build_reply_embed(long_context)
        # Author name is "↪ alice: " + snippet; snippet alone is capped at 60.
        snippet_part = embed.author.name.removeprefix("↪ alice: ")
        assert len(snippet_part) <= 60
        assert snippet_part.endswith("…")

    def test_empty_snippet_omits_colon(self, context: ReplyContext) -> None:
        empty_context = ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="",
        )
        embed = _build_reply_embed(empty_context)
        # No trailing ": " when there's nothing to quote — just the arrow + author.
        assert embed.author.name == "↪ alice"

    def test_newlines_in_snippet_collapsed(self, context: ReplyContext) -> None:
        multiline = ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="line one\nline two\n\nline four",
        )
        embed = _build_reply_embed(multiline)
        # All whitespace collapsed so the embed stays on one line in Discord.
        assert "\n" not in embed.author.name
        assert "line one line two line four" in embed.author.name


class TestBuildReplyButton:
    @pytest.fixture
    def context(self) -> ReplyContext:
        return ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="hello world",
            style="button",
        )

    def test_view_has_single_button(self, context: ReplyContext) -> None:
        view = _build_reply_button(context)
        assert len(view.children) == 1
        assert isinstance(view.children[0], discord.ui.Button)

    def test_button_is_link_style(self, context: ReplyContext) -> None:
        """Link buttons don't fire interactions — no callback or persistent View needed."""
        view = _build_reply_button(context)
        button = view.children[0]
        assert isinstance(button, discord.ui.Button)
        assert button.style == discord.ButtonStyle.link

    def test_button_url_is_jump_link(self, context: ReplyContext) -> None:
        view = _build_reply_button(context)
        button = view.children[0]
        assert isinstance(button, discord.ui.Button)
        assert button.url == "https://discord.com/channels/777/888/999"

    def test_button_label_includes_author_and_snippet(self, context: ReplyContext) -> None:
        view = _build_reply_button(context)
        button = view.children[0]
        assert isinstance(button, discord.ui.Button)
        assert button.label == "↩ Replying to @alice: hello world"

    def test_button_label_omits_colon_when_snippet_empty(self) -> None:
        """Attachment-only / empty-content originals show just the author line."""
        context = ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="",
            style="button",
        )
        view = _build_reply_button(context)
        button = view.children[0]
        assert isinstance(button, discord.ui.Button)
        assert button.label == "↩ Replying to @alice"

    def test_button_label_collapses_whitespace_in_snippet(self) -> None:
        context = ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="line one\nline two",
            style="button",
        )
        view = _build_reply_button(context)
        button = view.children[0]
        assert isinstance(button, discord.ui.Button)
        assert button.label is not None
        assert "\n" not in button.label
        assert "line one line two" in button.label

    def test_button_label_truncated_to_80_chars(self) -> None:
        context = ReplyContext(
            message_id=999,
            channel_id=888,
            guild_id=777,
            author_display_name="alice",
            content_snippet="x" * 200,
            style="button",
        )
        view = _build_reply_button(context)
        button = view.children[0]
        assert isinstance(button, discord.ui.Button)
        assert button.label is not None
        # Discord button label hard cap is 80; we enforce ≤ 80.
        assert len(button.label) <= 80
        assert button.label.endswith("…")
