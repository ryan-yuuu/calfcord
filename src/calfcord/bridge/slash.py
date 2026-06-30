"""Discord slash command registration and dispatch.

Owns the bot's ``app_commands.CommandTree``. Two operator commands, both
owner-gated, both registered once at boot (the tree is static, so there is no
roster-driven re-sync):

* ``/thinking-effort agent:<name> effort:<tier>`` — sets a per-agent thinking-effort
  override the bridge applies as a provider-blind ``model_settings`` union on that
  agent's next bridge invocation (C11/R-A1). The override is persisted in the
  SQLite ``agent_overrides`` table via :class:`~calfcord.bridge.overrides.EffortOverrides`
  (D-8) and survives a bridge restart. ``agent`` is free text (no live roster to
  build a choice list from); ``effort=none`` clears the override.
* ``/clear`` — posts a per-channel context-boundary marker the history fetcher
  truncates at, so agents stop seeing messages above the line. No agent involved.

Per-agent invocation slashes (``/echo`` …) are gone — agents are invoked by
``@<id>`` mention parsed in the gateway.
"""

from __future__ import annotations

import logging
from typing import cast, get_args

import discord
from discord import app_commands

from calfcord.agents.definition import ThinkingEffort
from calfcord.bridge.history import CLEAR_MARKER_TEXT
from calfcord.bridge.overrides import EffortOverrides

logger = logging.getLogger(__name__)

_THINKING_EFFORT_VALUES: tuple[ThinkingEffort, ...] = get_args(ThinkingEffort)
_THINKING_EFFORT_COMMAND_NAME = "thinking-effort"
_CLEAR_COMMAND_NAME = "clear"


class SlashCommandManager:
    """Builds, syncs, and dispatches the bridge's operator slash commands."""

    def __init__(
        self,
        client: discord.Client,
        *,
        overrides: EffortOverrides,
        owner_user_id: int | None = None,
        guild_id: int | None = None,
    ) -> None:
        self._client = client
        self._overrides = overrides
        self._owner_user_id = owner_user_id
        self._guild_id = guild_id
        self._tree = app_commands.CommandTree(client)

    def register_thinking_effort(self) -> None:
        """Register ``/thinking-effort`` on the command tree."""
        self._tree.add_command(self._build_thinking_effort_command())

    def register_clear(self) -> None:
        """Register the ``/clear`` operator slash on the command tree.

        Owner-gated. Posts a sentinel marker message
        (:data:`~calfcord.bridge.history.CLEAR_MARKER_TEXT`) into the invoking
        channel; the bridge's :class:`~calfcord.bridge.history.ChannelHistoryFetcher`
        truncates fetched history at the most recent marker, so every agent
        invoked in that channel stops seeing messages above the line on
        subsequent invocations. Non-destructive — no Discord messages are
        deleted, and the boundary lives in the channel itself, so it survives
        bridge restarts. Per-channel/thread scope.
        """

        async def callback(interaction: discord.Interaction) -> None:
            await self._on_clear(interaction)

        self._tree.add_command(
            app_commands.Command(
                name=_CLEAR_COMMAND_NAME,
                description="Clear agent context in this channel from this point onward",
                callback=callback,
            )
        )

    def _build_thinking_effort_command(self) -> app_commands.Command:
        effort_choices = [app_commands.Choice(name=value, value=value) for value in _THINKING_EFFORT_VALUES]

        @app_commands.describe(
            agent="Which agent to configure (its name, e.g. scribe)",
            effort="Thinking-effort tier applied on the agent's next bridge invocation; 'none' clears the override",
        )
        @app_commands.choices(effort=effort_choices)
        async def callback(
            interaction: discord.Interaction,
            agent: str,
            effort: app_commands.Choice[str],
        ) -> None:
            await self._on_thinking_effort(interaction, agent, effort.value)

        return app_commands.Command(
            name=_THINKING_EFFORT_COMMAND_NAME,
            description="Configure an agent's per-call thinking-effort tier",
            callback=callback,
        )

    async def _on_thinking_effort(
        self,
        interaction: discord.Interaction,
        agent_id: str,
        effort: str,
    ) -> None:
        logger.info(
            "thinking-effort slash invoked agent=%s effort=%s user_id=%s",
            agent_id,
            effort,
            interaction.user.id,
        )

        async def reply(text: str) -> None:
            # Best-effort ack: a Discord rejection (rate-limit / expired token) is
            # logged and swallowed — there is nothing actionable left to do.
            try:
                await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                logger.exception("failed to send slash reply agent=%s interaction_id=%s", agent_id, interaction.id)

        if self._owner_user_id is not None and interaction.user.id != self._owner_user_id:
            await reply("Only the configured owner can change agent effort.")
            return

        if effort not in _THINKING_EFFORT_VALUES:
            choices = ", ".join(f"`{v}`" for v in _THINKING_EFFORT_VALUES)
            await reply(f"Unknown effort `{effort}`. Choose one of: {choices}")
            return

        # Free text: agent ids are lower-case, so normalize so the override key
        # matches the mention the handler resolves. No roster validation — an
        # override for an offline (or mistyped) agent simply sits unused until
        # that agent is next invoked; validating against the online-only mesh
        # would wrongly reject a legitimately-offline agent.
        normalized = agent_id.strip().lower()
        if not normalized:
            await reply("Please specify an agent name.")
            return

        typed_effort = cast(ThinkingEffort, effort)
        try:
            await self._overrides.set(normalized, typed_effort)
        except Exception:
            logger.exception("failed to persist thinking-effort override agent=%s", normalized)
            await reply(f"Couldn't save the override for `{normalized}`. Check the bridge logs.")
            return

        if effort == "none":
            await reply(f"Cleared the thinking-effort override for `{normalized}`.")
        else:
            await reply(
                f"Set `effort={effort}` for `{normalized}`. Applies on its next bridge invocation "
                f"(native A2A consults and handoffs use the agent's own default)."
            )

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        """Handle a ``/clear`` invocation: post the per-channel context marker.

        Owner-gated. On success the bot posts
        :data:`~calfcord.bridge.history.CLEAR_MARKER_TEXT` into the channel as a
        plain (non-webhook) message; the history fetcher recognizes and truncates
        at it on the next invocation. Nothing is published to Kafka and no agent
        is involved — the boundary is purely the marker message in the channel.
        """
        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        logger.info("clear slash invoked channel_id=%s user_id=%s", channel_id, interaction.user.id)

        async def reply(text: str) -> None:
            # Broader ``DiscordException`` than the effort ack: also covers an
            # already-acknowledged interaction (``InteractionResponded``). Ack is
            # best-effort, so log and swallow.
            try:
                await interaction.response.send_message(text, ephemeral=True)
            except discord.DiscordException:
                logger.exception("failed to send clear reply interaction_id=%s", interaction.id)

        if self._owner_user_id is not None and interaction.user.id != self._owner_user_id:
            await reply("Only the configured owner can clear agent context.")
            return

        if channel is None:
            await reply("Couldn't find a channel to clear here. Run /clear from a text channel or thread.")
            return

        # Post the marker as a plain bot message (NOT an ephemeral/followup post)
        # so it lands in channel history with the bot's own user id and no
        # webhook_id — exactly what is_clear_marker authenticates.
        try:
            await channel.send(CLEAR_MARKER_TEXT)
        except discord.HTTPException:
            logger.exception("failed to post clear marker channel_id=%s interaction_id=%s", channel_id, interaction.id)
            await reply(
                "Couldn't post the clear marker (Discord rejected it), so context was NOT cleared. "
                "Check that I can send messages here and try again."
            )
            return
        except Exception:
            logger.exception(
                "unexpected error posting clear marker channel_id=%s interaction_id=%s", channel_id, interaction.id
            )
            await reply(
                "Couldn't post the clear marker (unexpected error), so context was NOT cleared. "
                "Check the bridge logs and try again."
            )
            return

        logger.info("clear marker posted channel_id=%s", channel_id)
        # The public 🧹 marker is the only confirmation needed; defer ephemerally
        # and delete the placeholder so the required ack leaves no lingering message.
        try:
            await interaction.response.defer(ephemeral=True)
            await interaction.delete_original_response()
        except discord.DiscordException:
            logger.exception("failed to ack clear interaction_id=%s", interaction.id)

    async def sync(self, guild_id: int | None) -> None:
        """Push the command tree to Discord. Idempotent; safe to call on every boot."""
        guild = discord.Object(id=guild_id) if guild_id is not None else None
        if guild is not None:
            self._tree.copy_global_to(guild=guild)
        synced = await self._tree.sync(guild=guild)
        logger.info("synced %d slash command(s) guild=%s", len(synced), guild_id)
