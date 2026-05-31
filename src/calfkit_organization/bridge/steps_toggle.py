"""The on-demand step-transcript view button (Phase 3).

On the terminal hop the outbox consumer (the SOLE transcript writer)
attaches a single secondary button to the agent's final reply *when the
turn used tools* (see
``docs/design/step-transcripts-and-live-streaming-plan.md`` §5, §7.5,
§8). Clicking that button shows the turn's steps as an **ephemeral
message** — visible only to the clicker — containing the tool calls and
intermediate text the agent produced. The agent's reply is **never**
edited: there is no expand/collapse state, no content sentinel, and no
2000-char surgery on the reply itself. The whole flow stays inside the
bridge process: the callback reads the persisted ``delta_json`` from the
bridge-local
:class:`~calfkit_organization.bridge.transcripts.TranscriptStore`,
renders it, and sends it back to the clicker as a private followup.

**One-way action, not a toggle.** The button is a *persistent* view
(``timeout=None``) with a single static ``custom_id``
(:data:`_TOGGLE_CUSTOM_ID`), registered once via ``add_view`` on the
gateway client. Because it never mutates the reply, every click is
independent — the user can view the steps as many times as they like,
each producing a fresh ephemeral message only they can see.

**Why defer ``thinking=True, ephemeral=True``.** The callback
:meth:`StepsToggleView._show_steps`'s first action is
``interaction.response.defer(thinking=True, ephemeral=True)``. A
*component* interaction needs BOTH flags to land an ephemeral followup:
``defer(ephemeral=True)`` alone falls through to the invisible
``deferred_message_update`` and the ephemeral flag is IGNORED (verified
in discord.py 2.7.1). Deferring acknowledges the click immediately and
removes Discord's 3-second interaction deadline from the DB read + render
that follows; the result is then delivered via
:meth:`Interaction.followup.send` with ``ephemeral=True``.

**Long transcripts attach a file.** When the rendered steps exceed
Discord's 2000-char message cap, the FULL transcript is attached as a
``steps.md`` file instead of being inlined — nothing is truncated.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import discord

from calfkit_organization.bridge.steps import _render_delta
from calfkit_organization.bridge.transcripts import TranscriptStoreLike

logger = logging.getLogger(__name__)

# Static custom_id the persistent view matches on for dispatch. One
# registered StepsToggleView instance (added via ``client.add_view`` in
# the gateway's ``_on_ready``) handles every click that carries this id,
# regardless of which message it rides on.
_TOGGLE_CUSTOM_ID = "steps:toggle"

# Discord's hard per-message content cap. Above it the rendered steps are
# attached as a file rather than inlined into the ephemeral message.
_DISCORD_MESSAGE_LIMIT = 2000


def _pluralize_steps(count: int) -> str:
    """Render ``N step(s)`` with correct singular/plural for ``count``."""
    return f"{count} step" if count == 1 else f"{count} steps"


def _collapsed_label(step_count: int) -> str:
    """Label for the view-steps button: ``⤵ N step(s)``."""
    return f"⤵ {_pluralize_steps(step_count)}"


def build_toggle_button(step_count: int) -> discord.ui.Button[Any]:
    """Build the view-steps button the outbox attaches to a reply.

    Args:
        step_count: Number of rendered step parts in the turn's transcript.
            Drives the button label (``⤵ N step(s)``).

    Returns:
        A secondary :class:`discord.ui.Button` carrying the static
        :data:`_TOGGLE_CUSTOM_ID`. Dispatch happens via the persistent
        :class:`StepsToggleView` registered on the gateway client, matched
        by that ``custom_id`` — this throwaway button only emits the
        component JSON on the reply message.
    """
    return discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label=_collapsed_label(step_count),
        custom_id=_TOGGLE_CUSTOM_ID,
    )


def render_steps(delta_json: str) -> tuple[str, int]:
    """Render a persisted ``delta_json`` blob into a full steps block.

    Deserializes the blob with pydantic-ai's ``ModelMessagesTypeAdapter``,
    runs the shared :func:`~calfkit_organization.bridge.steps._render_delta`
    to get one rendered string per step part, and joins them with a blank
    line. The text is returned in full — NO truncation; the caller decides
    whether it fits an inline message or must be attached as a file.

    Args:
        delta_json: The serialized structured slice of a turn's
            ``message_history`` (``ModelMessagesTypeAdapter.dump_json`` of
            ``message_history[initial_len:-1]``).

    Returns:
        ``(text, count)`` where ``text`` is the full joined render and
        ``count`` is the number of rendered step parts.
    """
    # Imported lazily so the module-import cost (and the vendored
    # pydantic-ai type-adapter construction) is paid only when a toggle is
    # actually built or clicked, not at bridge import time.
    from calfkit._vendor.pydantic_ai.messages import ModelMessagesTypeAdapter

    messages = ModelMessagesTypeAdapter.validate_json(delta_json)
    rendered = _render_delta(messages)
    return "\n\n".join(rendered), len(rendered)


class StepsToggleView(discord.ui.View):
    """Persistent view backing the on-demand step-transcript button.

    One instance is registered on the gateway client via
    ``client.add_view`` in ``_on_ready``; that single registration handles
    every click carrying :data:`_TOGGLE_CUSTOM_ID`, on any reply message.
    The throwaway buttons the outbox attaches to individual messages only
    emit the component JSON.

    Holds a reference to the bridge-local transcript store so the callback
    can read the turn's persisted steps by the clicked message's id.
    """

    def __init__(self, store: TranscriptStoreLike) -> None:
        # timeout=None ⇒ persistent view: survives bridge restarts and is
        # matched purely by the static custom_id of its button.
        super().__init__(timeout=None)
        self._store = store

    @discord.ui.button(
        custom_id=_TOGGLE_CUSTOM_ID,
        style=discord.ButtonStyle.secondary,
        label="⤵ steps",
    )
    async def _show_steps(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
        """Show the clicked reply's steps as an ephemeral message.

        Algorithm:

        1. ``interaction.response.defer(thinking=True, ephemeral=True)``
           FIRST. A *component* interaction needs BOTH ``thinking=True``
           AND ``ephemeral=True`` — ``defer(ephemeral=True)`` alone falls
           through to the invisible ``deferred_message_update`` and the
           ephemeral flag is IGNORED (verified in discord.py 2.7.1). This
           acknowledges the click and lifts the 3-second deadline off the
           DB read below.
        2. Look up the transcript row by the clicked message's id. If it's
           gone (pruned / pre-restart / never written), send an ephemeral
           "no longer available" followup and stop.
        3. Render the FULL steps text. When it fits the Discord message
           cap, send it inline as an ephemeral followup; otherwise attach
           the full transcript as a ``steps.md`` file (nothing truncated).

        Every followup is sent via :meth:`_safe_followup`, which swallows
        :class:`discord.HTTPException` — a failed send must not raise out
        of the component callback (which would surface as an "interaction
        failed" to the user and a noisy traceback).
        """
        # 1) Ephemeral defer. CRITICAL: a COMPONENT interaction needs BOTH
        #    thinking=True AND ephemeral=True — defer(ephemeral=True) alone
        #    falls through to the invisible deferred_message_update and the
        #    ephemeral flag is IGNORED (verified in discord.py 2.7.1).
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except discord.HTTPException:
            logger.warning("steps view: defer failed message_id=%s", getattr(interaction.message, "id", None))
            return

        message = interaction.message
        if message is None:
            logger.debug("steps view: interaction carried no message; ignoring")
            return

        row = await self._store.get_by_final_message_id(str(message.id))
        if row is None:
            await self._safe_followup(interaction, content="Step details are no longer available.")
            return

        text, _count = render_steps(row.delta_json)
        if not text:
            await self._safe_followup(interaction, content="This response recorded no steps.")
            return

        if len(text) <= _DISCORD_MESSAGE_LIMIT:
            await self._safe_followup(interaction, content=text)
        else:
            # Too long to inline → attach the FULL transcript as a file
            # (nothing truncated).
            f = discord.File(io.BytesIO(text.encode("utf-8")), filename="steps.md")
            await self._safe_followup(interaction, file=f)

    async def _safe_followup(
        self,
        interaction: discord.Interaction,
        *,
        content: str = discord.utils.MISSING,
        file: discord.File = discord.utils.MISSING,
    ) -> None:
        """Send an ephemeral followup, swallowing any Discord error.

        The followup carries either ``content`` or ``file`` (both default
        to :data:`discord.utils.MISSING` so an unsupplied argument is
        omitted from the underlying ``followup.send`` call). A failed send
        is logged and swallowed — it must never raise out of the component
        callback.
        """
        try:
            await interaction.followup.send(content=content, file=file, ephemeral=True)
        except discord.HTTPException:
            logger.warning(
                "steps view: followup.send failed message_id=%s",
                getattr(interaction.message, "id", None),
            )
