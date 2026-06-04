"""Wire schema for Discord events flowing through Kafka.

These pydantic models are the contract between the bridge (producer) and any
calfkit agent (consumer). The bridge serializes a :class:`WireMessage` to JSON
and tucks it into ``envelope.context.deps["discord"]`` on each
publish. Agents read it back via the same path inside their gates and ``run()``.

Both models are frozen — once normalized, a wire message is immutable.

``schema_version`` policy:
    - Add-only fields are non-breaking. Do not bump the version.
    - Field renames or removals require bumping ``schema_version`` AND a
      CHANGELOG entry. Consumers must tolerate the bump.

A2A invocations:
    The ``calfkit-tools`` ``private_chat`` tool reuses this schema when it
    invokes another agent on ``agent.{agent_id}.in``. It forwards the
    caller's originating wire with three fields overridden:
    ``slash_target`` set to the target agent's id, ``kind`` set to
    ``"slash"`` (so the existing ``addressed_to_me`` gate accepts), and
    ``content`` set to the A2A payload. Channel id, author, and
    message_id are preserved from the caller's original Discord context.
    A companion deps key ``caller_agent_id: str`` names the originating
    *agent* (distinct from ``author``, which always reflects the original
    human or webhook that started the chain). Agents that want to
    distinguish "human ↔ me" from "peer ↔ me" should read
    ``deps.get("caller_agent_id")``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class WireAuthor(BaseModel):
    """Resolved author identity for a Discord message.

    ``agent_id`` is set when the author is a persona webhook whose display name
    matches a registered agent. ``is_human_owner`` is set when the author is
    the configured human owner user. ``avatar_url`` is the author's effective
    avatar URL (per-guild member avatar, falling back to user avatar, falling
    back to Discord's default) — used by downstream consumers that want to
    render the author visually (e.g. reply-embed icons).
    """

    model_config = ConfigDict(frozen=True)

    discord_user_id: int
    display_name: str
    is_bot: bool
    is_webhook: bool
    webhook_id: int | None = None
    agent_id: str | None = None
    is_human_owner: bool = False
    avatar_url: str | None = None


class WireMessage(BaseModel):
    """A Discord event (regular message or slash invocation) projected onto Kafka.

    For ``kind="message"``, ``message_id`` is the user's Discord message ID.
    For ``kind="slash"``, ``message_id`` is the bridge's followup message ID
    (the visible echo posted via ``interaction.followup.send``). In both cases
    this is the ID an agent should pass to ``DiscordPersonaSender.send``'s
    ``reply_to_message_id`` parameter to render its reply as an inline reply
    to the source.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    event_id: str
    kind: Literal["message", "slash"]
    slash_target: str | None = None
    message_id: int
    channel_id: int
    source_channel_id: int | None = None
    """The actual Discord channel id the message landed in (thread or
    top-level). ``channel_id`` is the parent-channel id used for Kafka
    topic routing (the normalizer flattens threads to parent so all
    messages in a thread group share one topic); ``source_channel_id``
    preserves the un-flattened id so the bridge's
    :class:`~calfcord.bridge.history.ChannelHistoryFetcher`
    fetches the right channel's history (the thread itself, not the
    parent). ``None`` means the wire predates this field; callers fall
    back to ``channel_id`` (correct for non-thread messages; a thread
    wire from before the deploy loses one cycle of accurate history)."""
    guild_id: int
    content: str
    author: WireAuthor
    created_at: datetime

    @model_validator(mode="after")
    def _check_slash_target(self) -> WireMessage:
        if self.kind == "slash" and self.slash_target is None:
            raise ValueError("slash_target is required when kind='slash'")
        if self.kind == "message" and self.slash_target is not None:
            raise ValueError("slash_target must be None when kind='message'")
        return self

    @property
    def thread_id(self) -> int | None:
        """The thread this event originated in, or ``None`` for a top-level message.

        ``channel_id`` is the flattened parent channel (the persona webhook's
        host and the Kafka topic key); ``source_channel_id`` is the
        un-flattened origin. They differ exactly when the event came from a
        Discord thread — which is precisely when an agent reply or live-step
        progress message must be posted *into* the thread rather than the
        parent. Callers pass this as the ``thread_id`` argument to
        :class:`~calfcord.discord.persona.DiscordPersonaSender`.
        """
        if self.source_channel_id is not None and self.source_channel_id != self.channel_id:
            return self.source_channel_id
        return None
