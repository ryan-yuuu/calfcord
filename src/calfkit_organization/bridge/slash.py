"""Discord slash command registration and dispatch.

Owns the ``app_commands.CommandTree`` for the bot. At construction time
registers one ``/agent_id`` command per :class:`AgentDefinition` in the
registry, each bound to the same dispatch path. At runtime, when a user
invokes a slash command:

    1. Defer the interaction publicly (Discord shows "Bot is thinking…").
    2. Post a public followup that echoes the invocation. This message
       becomes the inline-reply anchor that the agent will reply to.
    3. Normalize the slash into a :class:`WireMessage` carrying the
       followup's message ID as ``message_id``.
    4. Publish via :class:`KafkaPublisher`.

The 15-minute followup window covers the LLM round-trip; the dispatcher
future created by the publisher auto-cleans at the same horizon.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.normalizer import SlashNormalizer
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.roundtrip import BridgeRoundTrip

logger = logging.getLogger(__name__)


class SlashCommandManager:
    """Builds, syncs, and dispatches per-agent slash commands."""

    def __init__(
        self,
        client: discord.Client,
        registry: AgentRegistry,
        roundtrip: BridgeRoundTrip,
        slash_normalizer: SlashNormalizer,
    ) -> None:
        self._client = client
        self._registry = registry
        self._roundtrip = roundtrip
        self._normalizer = slash_normalizer
        self._tree = app_commands.CommandTree(client)

    def register_all(self) -> None:
        """Add one :class:`app_commands.Command` per agent. Call once at startup."""
        for spec in self._registry.all():
            self._tree.add_command(self._build_command(spec))

    async def sync(self, guild_id: int | None) -> None:
        """Push the command tree to Discord. Idempotent; safe to call on every boot."""
        guild = discord.Object(id=guild_id) if guild_id is not None else None
        if guild is not None:
            self._tree.copy_global_to(guild=guild)
        synced = await self._tree.sync(guild=guild)
        logger.info("synced %d slash command(s) guild=%s", len(synced), guild_id)

    def _build_command(self, spec: AgentDefinition) -> app_commands.Command:
        # A factory function gives each callback its own scope so ``spec``
        # closes over its own loop iteration, not the last one.
        def _make_callback(spec: AgentDefinition):
            @app_commands.describe(message="What you want this agent to do")
            async def callback(interaction: discord.Interaction, message: str) -> None:
                await self._on_invocation(interaction, spec, message)

            return callback

        return app_commands.Command(
            name=spec.slash.lstrip("/"),
            description=spec.description[:100],
            callback=_make_callback(spec),
        )

    async def _on_invocation(
        self,
        interaction: discord.Interaction,
        spec: AgentDefinition,
        message: str,
    ) -> None:
        logger.info(
            "slash invocation agent=%s interaction_id=%s user_id=%s",
            spec.agent_id,
            interaction.id,
            interaction.user.id,
        )
        try:
            await interaction.response.defer(ephemeral=False)
            followup = await interaction.followup.send(
                f"**/{spec.slash[1:]}** {message}",
                wait=True,
            )
            assert followup is not None, "followup.send with wait=True must return a Message"

            wire = self._normalizer.normalize(
                interaction=interaction,
                slash_target=spec,
                message_arg=message,
                followup_message_id=followup.id,
            )
            await self._roundtrip.handle(wire)
            logger.info(
                "slash dispatched agent=%s interaction_id=%s followup_id=%s event_id=%s",
                spec.agent_id,
                interaction.id,
                followup.id,
                wire.event_id,
            )
        except Exception:
            logger.exception(
                "slash invocation failed agent=%s interaction_id=%s",
                spec.agent_id,
                interaction.id,
            )
            try:
                await interaction.followup.send(
                    "Sorry — something went wrong handling that slash. Please try again.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
