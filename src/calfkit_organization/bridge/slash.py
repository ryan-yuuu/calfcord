"""Discord slash command registration and dispatch.

Owns the ``app_commands.CommandTree`` for the bot. Two kinds of commands
can be registered:

* ``/thinking-effort agent:<name> effort:<tier>`` — the operator slash
  registered by :meth:`register_thinking_effort`. Rewrites the
  ``thinking_effort`` field of the agent's ``.md`` frontmatter via
  :meth:`AgentRegistry.set_thinking_effort`, and swaps the in-memory
  :class:`AgentDefinition` in the registry so the next invocation picks
  up the new value. The frontmatter is validated **before** the disk
  write so a malformed ``.md`` surfaces immediately rather than at next
  agent boot. Authorization is restricted to
  ``DiscordSettings.owner_user_id``.
* Per-agent invocation slashes (``/echo``, ``/scribe``, …) built by
  :meth:`register_all`. Currently disabled in the bridge in favour of
  ``@<agent_id>`` text-prefix invocation, but the builder is preserved
  here for future use. When enabled, dispatch defers the interaction,
  posts a followup as the reply anchor, normalizes to a
  :class:`WireMessage`, and hands off to :class:`BridgeIngress` for
  fire-and-forget publication. The agent's reply is posted later by
  the outbox consumer, not by this callback — so the 15-minute Discord
  followup window is no longer the LLM's deadline (the followup echo
  is posted before the LLM runs; the reply is posted via webhook).
"""

from __future__ import annotations

import logging
from typing import cast, get_args

import discord
from discord import app_commands

from calfkit_organization.agents.definition import AgentDefinition, ThinkingEffort
from calfkit_organization.bridge.ingress import BridgeIngress
from calfkit_organization.bridge.normalizer import SlashNormalizer
from calfkit_organization.bridge.registry import AgentRegistry

logger = logging.getLogger(__name__)

_THINKING_EFFORT_VALUES: tuple[ThinkingEffort, ...] = get_args(ThinkingEffort)
_THINKING_EFFORT_COMMAND_NAME = "thinking-effort"


class SlashCommandManager:
    """Builds, syncs, and dispatches per-agent slash commands."""

    def __init__(
        self,
        client: discord.Client,
        registry: AgentRegistry,
        ingress: BridgeIngress,
        slash_normalizer: SlashNormalizer,
        *,
        owner_user_id: int | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._ingress = ingress
        self._normalizer = slash_normalizer
        self._owner_user_id = owner_user_id
        self._tree = app_commands.CommandTree(client)

    def register_all(self) -> None:
        """Add one :class:`app_commands.Command` per agent. Call once at startup."""
        for spec in self._registry.all():
            self._tree.add_command(self._build_command(spec))

    def register_thinking_effort(self) -> None:
        """Register ``/thinking-effort`` on the command tree.

        Ambient-message limitation: the rewritten effort only takes effect
        on the *next* message for slash invocations and ``@<agent_id>``
        mentions — ambient channel messages use whatever was baked into
        the agent's model client at boot. See
        :mod:`calfkit_organization.agents.thinking` for the full story.
        """
        self._tree.add_command(self._build_thinking_effort_command())

    def _build_thinking_effort_command(self) -> app_commands.Command:
        agent_choices = [
            app_commands.Choice(name=spec.agent_id, value=spec.agent_id)
            for spec in self._registry.all()
        ]
        effort_choices = [
            app_commands.Choice(name=value, value=value) for value in _THINKING_EFFORT_VALUES
        ]

        @app_commands.describe(
            agent="Which agent to configure",
            effort="Thinking-effort tier; applies to the next message (mentions/slashes only — ambient messages use the agent's default)",
        )
        @app_commands.choices(agent=agent_choices, effort=effort_choices)
        async def callback(
            interaction: discord.Interaction,
            agent: app_commands.Choice[str],
            effort: app_commands.Choice[str],
        ) -> None:
            await self._on_thinking_effort(interaction, agent.value, effort.value)

        return app_commands.Command(
            name=_THINKING_EFFORT_COMMAND_NAME,
            description="Configure an agent's per-call thinking effort tier",
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
            # If the Discord API rejects our reply (rate-limit, expired
            # interaction token, etc.) log it but don't propagate — the
            # caller is in an error-recovery path and there's nothing
            # actionable left to do.
            try:
                await interaction.response.send_message(text, ephemeral=True)
            except discord.HTTPException:
                logger.exception(
                    "failed to send slash reply agent=%s interaction_id=%s",
                    agent_id,
                    interaction.id,
                )

        if self._owner_user_id is not None and interaction.user.id != self._owner_user_id:
            await reply("Only the configured owner can change agent effort.")
            return

        spec = self._registry.by_id(agent_id)
        if spec is None:
            known = ", ".join(f"`{s.agent_id}`" for s in self._registry.all()) or "<none>"
            await reply(f"No agent named `{agent_id}`. Known: {known}.")
            return

        if effort not in _THINKING_EFFORT_VALUES:
            choices = ", ".join(f"`{v}`" for v in _THINKING_EFFORT_VALUES)
            await reply(f"Unknown effort `{effort}`. Choose one of: {choices}")
            return

        try:
            await self._registry.set_thinking_effort(
                agent_id, cast(ThinkingEffort, effort)
            )
        except FileNotFoundError:
            logger.error(
                "agent %s source_path missing on disk; cannot rewrite frontmatter",
                agent_id,
            )
            await reply(
                f"Internal error: agent `{agent_id}` source file is missing. "
                "Check the bridge logs."
            )
            return
        except ValueError:
            # Includes pydantic ValidationError (subclass) and the
            # md_writer's "malformed YAML" re-raise. The .md is unusable
            # for this agent until the operator fixes it.
            logger.exception(
                "agent %s frontmatter validation failed interaction_id=%s",
                agent_id,
                interaction.id,
            )
            await reply(
                f"Couldn't rewrite `{agent_id}`'s .md — frontmatter is invalid "
                f"(interaction_id={interaction.id}). Check the bridge logs."
            )
            return
        except OSError:
            # Filesystem error during atomic write (permission, ENOSPC,
            # EROFS, fsync failure). The on-disk file is unchanged
            # because md_writer cleans up its tmp file.
            logger.exception(
                "filesystem error rewriting %s.md interaction_id=%s",
                agent_id,
                interaction.id,
            )
            await reply(
                f"Couldn't write `{agent_id}`'s .md — filesystem error "
                f"(interaction_id={interaction.id}). Check the bridge logs."
            )
            return

        if effort == "none":
            await reply(
                f"Saved `effort=none` for `{agent_id}`. Thinking is disabled; "
                "applies to the next slash or @-mention message. "
                "Restart the agent process to apply to ambient messages too."
            )
        else:
            await reply(
                f"Saved `effort={effort}` for `{agent_id}`. "
                "Applies to the next slash or @-mention message. "
                "Restart the agent process to apply to ambient messages too."
            )

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
            await self._ingress.handle(wire)
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
