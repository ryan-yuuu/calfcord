"""Discord slash command registration and dispatch.

Owns the ``app_commands.CommandTree`` for the bot. Two kinds of commands
can be registered:

* ``/thinking-effort agent:<name> effort:<tier>`` — the operator slash
  registered by :meth:`register_thinking_effort`. Fire-and-forget: it
  publishes a :class:`SetThinkingEffortOp` to the agent's control topic
  and optimistically updates the bridge's in-memory registry copy. The
  agent applies the command asynchronously and rewrites its own ``.md``
  via :mod:`calfkit_organization.agents.md_writer`; the bridge's
  projection reconciles when the agent's post-apply state event arrives.
  Authorization is restricted to ``DiscordSettings.owner_user_id``.
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

State-event-driven roster changes (first-seen agent, agent departure)
debounce a :meth:`schedule_resync` that rebuilds and re-syncs the
``/thinking-effort`` choice list so the Discord UI reflects the live
roster.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import cast, get_args

import discord
from calfkit.client import Client
from discord import app_commands

from calfkit_organization.agents.definition import AgentDefinition, ThinkingEffort
from calfkit_organization.bridge.history import CLEAR_MARKER_TEXT
from calfkit_organization.bridge.ingress import AmbientRosterEmptyError, BridgeIngress
from calfkit_organization.bridge.normalizer import SlashNormalizer
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.control_plane.publish import publish_control_command
from calfkit_organization.control_plane.schema import SetThinkingEffortOp

logger = logging.getLogger(__name__)

_THINKING_EFFORT_VALUES: tuple[ThinkingEffort, ...] = get_args(ThinkingEffort)
_THINKING_EFFORT_COMMAND_NAME = "thinking-effort"
_CLEAR_COMMAND_NAME = "clear"
_TASK_COMMAND_NAME = "task"

# Discord caps thread names at 100 characters.
_THREAD_NAME_MAX_LEN = 100


def _thread_name_from_text(text: str, *, fallback: str = "Task", max_len: int = _THREAD_NAME_MAX_LEN) -> str:
    """Derive a thread title from the ``/task`` message.

    Collapses runs of whitespace and truncates to Discord's ``max_len``-char
    thread-name cap (appending an ellipsis when truncated). Falls back to
    ``fallback`` when the message is empty after collapsing.
    """
    collapsed = " ".join(text.split())
    if not collapsed:
        return fallback
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1].rstrip() + "…"


class SlashCommandManager:
    """Builds, syncs, and dispatches per-agent slash commands."""

    def __init__(
        self,
        client: discord.Client,
        registry: AgentRegistry,
        ingress: BridgeIngress,
        slash_normalizer: SlashNormalizer,
        *,
        calfkit_client: Client,
        owner_user_id: int | None = None,
        guild_id: int | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._ingress = ingress
        self._normalizer = slash_normalizer
        self._client_calfkit = calfkit_client
        self._owner_user_id = owner_user_id
        self._guild_id = guild_id
        self._tree = app_commands.CommandTree(client)
        # Debounced re-sync state. The state consumer fires
        # ``schedule_resync`` on every first-seen / departure event; we
        # coalesce bursts into a single rebuild+sync to avoid hammering
        # Discord's slash-sync endpoint when many agents come up at once.
        #
        # Trailing-edge: an event that arrives DURING an in-flight sync
        # sets ``_resync_pending`` so the in-flight task schedules a
        # follow-up cycle after it completes. Without this, agents that
        # joined mid-sync would never make it into the slash choice list
        # until the next unrelated event.
        self._resync_task: asyncio.Task[None] | None = None
        self._resync_pending: bool = False
        self._resync_debounce_s: float = 1.0

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

    def register_clear(self) -> None:
        """Register the ``/clear`` operator slash on the command tree.

        Owner-gated. Posts a sentinel marker message
        (:data:`~calfkit_organization.bridge.history.CLEAR_MARKER_TEXT`)
        into the invoking channel; the bridge's
        :class:`~calfkit_organization.bridge.history.ChannelHistoryFetcher`
        truncates fetched history at the most recent marker, so every
        agent subscribed to that channel stops seeing messages above the
        line on subsequent invocations. Non-destructive — no Discord
        messages are deleted, and the boundary lives in the channel
        itself, so it survives bridge restarts.

        Per-channel/thread scope: the marker exists only in the channel
        it was posted in and the fetcher keys history on the source
        channel, so ``/clear`` in a thread clears that thread and
        ``/clear`` in a parent channel does not clear its threads.

        The command takes no parameters and has no agent-roster choice
        list, so (unlike ``/thinking-effort``) it needs no debounced
        re-sync — it is registered once and built inline here.
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

    def register_task(self) -> None:
        """Register the ``/task`` command on the command tree.

        Open to anyone in the guild (no owner gate). The callback posts the
        supplied message into the invoking channel, opens a public thread
        anchored on it, and routes the message ambiently so the router
        summons whichever agents the task needs — whose replies and
        live-step progress post back into the new thread (see
        :meth:`_on_task`). This realizes the "threads are tasks" design as a
        first-class slash command.

        Like ``/clear`` it takes a single text parameter and has no
        agent-roster choice list, so it needs no debounced re-sync — it is
        registered once and built inline here.
        """

        @app_commands.describe(message="What the task is; posted as the thread's opening message")
        async def callback(interaction: discord.Interaction, message: str) -> None:
            await self._on_task(interaction, message)

        self._tree.add_command(
            app_commands.Command(
                name=_TASK_COMMAND_NAME,
                description="Post a message and open a task thread for the agents to work in",
                callback=callback,
            )
        )

    def _build_thinking_effort_command(self) -> app_commands.Command:
        # The built-in router is excluded from the choice list — its
        # config is env-driven (CALFKIT_ROUTER_*) and it is not a
        # user-invocable agent, so exposing it in the Discord UI would
        # only confuse operators.
        agent_choices = [
            app_commands.Choice(name=spec.agent_id, value=spec.agent_id)
            for spec in self._registry.all()
            if spec.role != "router"
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
            await reply(
                f"No agent named `{agent_id}` in the bridge's roster. "
                f"Known: {known}."
            )
            return

        if effort not in _THINKING_EFFORT_VALUES:
            choices = ", ".join(f"`{v}`" for v in _THINKING_EFFORT_VALUES)
            await reply(f"Unknown effort `{effort}`. Choose one of: {choices}")
            return

        typed_effort = cast(ThinkingEffort, effort)

        # Optimistic in-memory update. The bridge's projection will be
        # reconciled when the agent applies the command and emits a
        # fresh state event.
        self._registry.apply_local_thinking_effort_override(agent_id, typed_effort)

        request_id = str(uuid.uuid4())
        command = SetThinkingEffortOp(
            agent_id=agent_id,
            value=typed_effort,
            request_id=request_id,
            issued_by=str(interaction.user.id),
        )
        try:
            await publish_control_command(self._client_calfkit, agent_id, command)
        except Exception:
            logger.exception(
                "failed to publish control command agent=%s request_id=%s",
                agent_id,
                request_id,
            )
            await reply(
                f"Couldn't publish control command for `{agent_id}` "
                f"(request_id={request_id}). Check bridge logs."
            )
            return

        await reply(
            f"Sent `effort={effort}` to `{agent_id}` (fire-and-forget, "
            f"request_id={request_id}). Bridge applies override on next "
            f"slash/mention; agent rewrites its `.md` asynchronously."
        )

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        """Handle a ``/clear`` invocation: post the per-channel context marker.

        Owner-gated. On success the bot posts
        :data:`~calfkit_organization.bridge.history.CLEAR_MARKER_TEXT`
        into the channel as a plain (non-webhook) message; the history
        fetcher recognizes and truncates at it on the next invocation.
        Nothing is published to Kafka and no agent is involved — the
        boundary is purely the marker message in the channel.
        """
        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        logger.info(
            "clear slash invoked channel_id=%s user_id=%s",
            channel_id,
            interaction.user.id,
        )

        async def reply(text: str) -> None:
            # Like :meth:`_on_thinking_effort`'s reply helper, but catch the
            # broader ``DiscordException`` so a failed ack can never escape
            # into the command dispatcher: this covers Discord's own
            # rejection (``HTTPException`` — expired token, rate limit) AND
            # an already-acknowledged interaction (``InteractionResponded``,
            # a ``ClientException``). The ack is best-effort with nothing
            # actionable left to do, so log and swallow.
            try:
                await interaction.response.send_message(text, ephemeral=True)
            except discord.DiscordException:
                logger.exception(
                    "failed to send clear reply interaction_id=%s",
                    interaction.id,
                )

        if self._owner_user_id is not None and interaction.user.id != self._owner_user_id:
            await reply("Only the configured owner can clear agent context.")
            return

        if channel is None:
            # A guild slash always carries a messageable channel; this
            # guards the rare uncached-channel case rather than letting an
            # AttributeError escape into the command dispatcher.
            await reply(
                "Couldn't find a channel to clear here. Run /clear from a "
                "text channel or thread."
            )
            return

        # Post the marker as a plain bot message (NOT an ephemeral/followup
        # post) so it lands in channel history with the bot's own user id
        # and no webhook_id — exactly what is_clear_marker authenticates.
        try:
            await channel.send(CLEAR_MARKER_TEXT)
        except discord.HTTPException:
            # Discord rejected the post — most often ``Forbidden`` (a
            # subclass), i.e. the bot lacks Send Messages here.
            logger.exception(
                "failed to post clear marker channel_id=%s interaction_id=%s",
                channel_id,
                interaction.id,
            )
            await reply(
                "Couldn't post the clear marker (Discord rejected it), so "
                "context was NOT cleared. Check that I can send messages "
                "here and try again."
            )
            return
        except Exception:
            # Anything non-Discord (connector death, an unexpected channel
            # type) must still surface a truthful result rather than escape
            # into the command dispatcher as a generic "did not respond".
            logger.exception(
                "unexpected error posting clear marker channel_id=%s interaction_id=%s",
                channel_id,
                interaction.id,
            )
            await reply(
                "Couldn't post the clear marker (unexpected error), so "
                "context was NOT cleared. Check the bridge logs and try again."
            )
            return

        logger.info("clear marker posted channel_id=%s", channel_id)
        await reply(
            "Context cleared. Agents won't see messages above the marker on "
            "their next turn in this channel."
        )

    async def _on_task(self, interaction: discord.Interaction, message: str) -> None:
        """Handle a ``/task`` invocation: post the message, open a thread, route it.

        Open to anyone (no owner gate). Flow:

        1. Defer ephemerally — the user gets a private ack and we buy time
           past Discord's 3-second initial-response budget for the posts
           below.
        2. Require a top-level text channel: reject inside an existing thread
           (Discord can't nest threads) or a non-text channel (forum/voice —
           the persona webhook needs a parent text channel anyway).
        3. Post the message as a plain bot message — the thread's anchor. A
           bot, non-webhook message is filtered by the gateway's
           ``_on_message`` handler, so it is NOT double-routed; we route a
           hand-built wire below instead.
        4. Open a public thread anchored on that message.
        5. Build an ambient wire whose ``source_channel_id`` is the new
           thread and hand it to the ingress (fire-and-forget). The router
           fans out; replies and step progress post into the thread.
        6. Confirm ephemerally with a jump link.

        Every Discord call is best-effort: a failure logs and sends an
        ephemeral followup explaining what went wrong rather than escaping
        into the command dispatcher (which would surface to the user as a
        generic "the application did not respond").
        """
        channel = interaction.channel
        logger.info(
            "task slash invoked channel_id=%s user_id=%s",
            getattr(channel, "id", None),
            interaction.user.id,
        )

        # Defer ephemerally up front; all later replies are ephemeral
        # followups. A failed defer leaves nothing useful to do.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.DiscordException:
            logger.exception("task: defer failed interaction_id=%s", interaction.id)
            return

        async def followup(text: str) -> None:
            try:
                await interaction.followup.send(text, ephemeral=True)
            except discord.DiscordException:
                logger.exception("task: followup send failed interaction_id=%s", interaction.id)

        # Step 2: require a top-level text channel.
        if not isinstance(channel, discord.TextChannel):
            await followup(
                "`/task` must be run in a top-level text channel — not inside a "
                "thread (threads can't be nested) or a forum/voice channel."
            )
            return

        # Step 3: post the starter message (the thread's anchor).
        try:
            starter = await channel.send(message)
        except discord.DiscordException:
            logger.exception(
                "task: failed to post starter channel_id=%s interaction_id=%s",
                channel.id,
                interaction.id,
            )
            await followup(
                "Couldn't post the task message (Discord rejected it). Check that "
                "I can send messages here and try again."
            )
            return

        # Step 4: open the public thread anchored on the starter.
        try:
            thread = await channel.create_thread(
                name=_thread_name_from_text(message),
                message=discord.Object(id=starter.id),
            )
        except discord.DiscordException:
            logger.exception(
                "task: failed to create thread channel_id=%s anchor_id=%s interaction_id=%s",
                channel.id,
                starter.id,
                interaction.id,
            )
            await followup(
                "Posted the message but couldn't open a thread (Discord rejected "
                "it). Check that I have the Create Public Threads permission here."
            )
            return

        # Step 5: route the task ambiently; replies/steps land in the thread.
        wire = self._normalizer.normalize_task(
            interaction,
            message,
            anchor_message_id=starter.id,
            thread_id=thread.id,
        )
        try:
            await self._ingress.handle(wire)
        except AmbientRosterEmptyError:
            logger.info(
                "task: created thread but roster empty channel_id=%s thread_id=%s event_id=%s",
                channel.id,
                thread.id,
                wire.event_id,
            )
            await followup(
                f"Opened the task thread ({thread.jump_url}), but no assistant "
                "agents are configured to work it. Contact an operator to add one."
            )
            return
        except Exception:
            logger.exception(
                "task: ingress publish failed channel_id=%s thread_id=%s event_id=%s",
                channel.id,
                thread.id,
                wire.event_id,
            )
            await followup(
                f"Opened the task thread ({thread.jump_url}), but dispatching it "
                "to the agents failed. The thread already exists, so don't re-run "
                "`/task` (that would create a duplicate) — an operator should "
                f"check the bridge logs for event `{wire.event_id}`."
            )
            return

        logger.info(
            "task dispatched channel_id=%s thread_id=%s anchor_id=%s event_id=%s",
            channel.id,
            thread.id,
            starter.id,
            wire.event_id,
        )
        await followup(f"✅ Created task thread: {thread.jump_url}")

    def schedule_resync(self, agent_id: str) -> None:
        """Schedule a debounced re-sync of ``/thinking-effort``.

        Reflects the current agent roster to Discord. Trailing-edge
        debounce: while a sync is in flight, additional calls flip a
        ``_resync_pending`` flag and the running task chains a follow-up
        cycle so events arriving mid-sync are not dropped. Called by the
        state consumer's ``on_first_seen`` and ``on_departed`` callbacks.
        The ``agent_id`` argument is logged but not otherwise used --
        the rebuild reads the current registry.
        """
        if self._resync_task is not None and not self._resync_task.done():
            # A debounced resync is already pending or in-flight. Mark
            # that another cycle is needed: if the task hasn't finished
            # its sleep yet it picks up the latest registry state when
            # it builds the command; if the task is already past its
            # sleep (mid-sync), the ``_resync_pending`` flag will be
            # observed in its finally block and a follow-up cycle is
            # chained. Either way, the new agent gets reflected.
            self._resync_pending = True
            return
        self._resync_pending = False
        self._resync_task = asyncio.create_task(self._debounced_resync())

    async def _debounced_resync(self) -> None:
        try:
            try:
                await asyncio.sleep(self._resync_debounce_s)
            except asyncio.CancelledError:
                raise
            try:
                self._tree.remove_command(_THINKING_EFFORT_COMMAND_NAME)
            except Exception:
                # Not registered yet or some other transient -- proceed
                # to add.
                logger.debug("remove_command(/thinking-effort) raised; proceeding")
            self._tree.add_command(self._build_thinking_effort_command())
            await self.sync(self._guild_id)
            non_router_count = sum(
                1 for s in self._registry.all() if s.role != "router"
            )
            logger.info(
                "re-synced /thinking-effort with %d agent choice(s)",
                non_router_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("debounced slash resync failed")
        finally:
            # Trailing-edge: if any schedule_resync calls came in during
            # the rebuild+sync above, chain another cycle so we don't
            # drop their roster updates. The next cycle observes the
            # current registry state, so multiple coalesced events all
            # get reflected by the one follow-up sync.
            if self._resync_pending:
                self._resync_pending = False
                self._resync_task = asyncio.create_task(
                    self._debounced_resync()
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
            name=spec.agent_id,
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
                f"**/{spec.agent_id}** {message}",
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
