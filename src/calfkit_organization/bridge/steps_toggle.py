"""The inline step-transcript expand/collapse toggle (Phase 3).

On the terminal hop the outbox consumer (the SOLE transcript writer)
attaches a single secondary button to the agent's final reply *when the
turn used tools* (see
``docs/design/step-transcripts-and-live-streaming-plan.md`` §5, §7.5,
§8). Clicking that button expands the reply **inline** to reveal the
turn's steps (truncated to Discord's 2000-char message cap) and collapses
back. The whole flow stays inside the bridge process: the callback reads
the persisted ``delta_json`` from the bridge-local
:class:`~calfkit_organization.bridge.transcripts.TranscriptStore`,
renders it, and edits the reply message in place.

**No extra DB state for expand/collapse.** The toggle is a *persistent*
view (``timeout=None``) with a single static ``custom_id``
(:data:`_TOGGLE_CUSTOM_ID`). Because the ``custom_id`` is static, the
view cannot stash per-message expand state on the component. Instead the
state is derived from the message content itself: the expanded reply is
``reply_text + _STEPS_SEP + steps_text``, so the presence of the exotic
:data:`_STEPS_SEP` separator in the current content tells the callback
whether the message is currently expanded. Collapsing is just splitting
on the separator and keeping the prefix.

**Why defer first.** The callback :meth:`StepsToggleView._toggle`'s first
action is ``interaction.response.defer()`` (a *component* defer →
``deferred_message_update``, invisible, no "thinking" spinner). That
acknowledges the interaction immediately and removes Discord's 3-second
interaction deadline from the DB read + render that follows — the edit is
then applied via :meth:`Interaction.edit_original_response`.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from calfkit_organization.bridge.steps import _render_delta
from calfkit_organization.bridge.transcripts import TranscriptStore

logger = logging.getLogger(__name__)

# Static custom_id the persistent view matches on for dispatch. One
# registered StepsToggleView instance (added via ``client.add_view`` in
# the gateway's ``_on_ready``) handles every click that carries this id,
# regardless of which message it rides on.
_TOGGLE_CUSTOM_ID = "steps:toggle"

# Exotic separator delimiting the steps block appended to the reply
# content when expanded. The expand/collapse state carries NO extra DB
# column or component state — it is derived purely from whether the live
# message content contains this sentinel. It is deliberately unusual (a
# Discord subtext line ``-#`` plus em-dashes) so it is exceedingly
# unlikely to appear in a genuine agent reply and be mistaken for an
# already-expanded marker.
_STEPS_SEP = "\n\n-# ——— steps ———\n"

# Headroom subtracted from Discord's 2000-char message cap when computing
# how much room is left for the steps block in an expanded reply: the
# reply text, the separator, and a small safety margin for any rendering
# slack. Below :data:`_MIN_STEPS_BUDGET` chars of remaining room we don't
# bother rendering steps inline.
_DISCORD_MESSAGE_LIMIT = 2000
_EXPAND_MARGIN = 16
_MIN_STEPS_BUDGET = 50

# Shown in place of the steps block when the reply itself is so long that
# there is no meaningful room left under the Discord cap to render steps.
_STEPS_TOO_LONG_BLOCK = "(steps too long to display inline)"

# Appended to the rendered steps when the joined block had to be cut to
# fit the remaining budget, so the user knows more steps exist than are
# shown.
_RENDER_TRUNCATION_MARKER = "\n… (truncated)"


def _pluralize_steps(count: int) -> str:
    """Render ``N step(s)`` with correct singular/plural for ``count``."""
    return f"{count} step" if count == 1 else f"{count} steps"


def _collapsed_label(step_count: int) -> str:
    """Label for the COLLAPSED-state button: ``⤵ N step(s)``."""
    return f"⤵ {_pluralize_steps(step_count)}"


# Label for the EXPANDED-state button. Static (no count) — the count is
# already visible inline in the expanded steps block.
_EXPANDED_LABEL = "⤴ Hide steps"


def build_toggle_button(step_count: int) -> discord.ui.Button[Any]:
    """Build the COLLAPSED-state expand toggle the outbox attaches to a reply.

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


def render_steps(delta_json: str, *, limit: int) -> tuple[str, int]:
    """Render a persisted ``delta_json`` blob into an inline steps block.

    Deserializes the blob with pydantic-ai's ``ModelMessagesTypeAdapter``,
    runs the shared :func:`~calfkit_organization.bridge.steps._render_delta`
    to get one rendered string per step part, joins them with a blank line,
    and truncates the joined string to ``limit`` characters (appending a
    visible truncation marker when it had to cut).

    Args:
        delta_json: The serialized structured slice of a turn's
            ``message_history`` (``ModelMessagesTypeAdapter.dump_json`` of
            ``message_history[initial_len:-1]``).
        limit: Maximum length of the returned text. The joined steps are
            truncated to fit, with :data:`_RENDER_TRUNCATION_MARKER`
            appended when cut.

    Returns:
        ``(text, count)`` where ``text`` is the (possibly truncated)
        joined render and ``count`` is the number of rendered step parts
        (the *untruncated* count, so the button label reflects the true
        number of steps even when the inline view is cut).
    """
    # Imported lazily so the module-import cost (and the vendored
    # pydantic-ai type-adapter construction) is paid only when a toggle is
    # actually built or clicked, not at bridge import time.
    from calfkit._vendor.pydantic_ai.messages import ModelMessagesTypeAdapter

    messages = ModelMessagesTypeAdapter.validate_json(delta_json)
    rendered_parts = _render_delta(messages)
    count = len(rendered_parts)
    joined = "\n\n".join(rendered_parts)
    if len(joined) > limit:
        # Reserve room for the marker so the result stays within ``limit``.
        head = limit - len(_RENDER_TRUNCATION_MARKER)
        joined = joined[: max(head, 0)] + _RENDER_TRUNCATION_MARKER
    return joined, count


class StepsToggleView(discord.ui.View):
    """Persistent view backing the inline step-transcript expand toggle.

    One instance is registered on the gateway client via
    ``client.add_view`` in ``_on_ready``; that single registration handles
    every click carrying :data:`_TOGGLE_CUSTOM_ID`, on any reply message.
    The throwaway buttons the outbox/callback attach to individual
    messages only emit the component JSON.

    Holds a reference to the bridge-local :class:`TranscriptStore` so the
    callback can read the turn's persisted steps by the clicked message's
    id.
    """

    def __init__(self, store: TranscriptStore) -> None:
        # timeout=None ⇒ persistent view: survives bridge restarts and is
        # matched purely by the static custom_id of its button.
        super().__init__(timeout=None)
        self._store = store

    @discord.ui.button(
        custom_id=_TOGGLE_CUSTOM_ID,
        style=discord.ButtonStyle.secondary,
        label="⤵ steps",
    )
    async def _toggle(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
        """Expand or collapse the clicked reply's inline steps block.

        Algorithm:

        1. ``interaction.response.defer()`` FIRST — a component defer
           (``deferred_message_update``, invisible). This acknowledges the
           click and lifts the 3-second deadline off the DB read below.
        2. Look up the transcript row by the clicked message's id. If it's
           gone (pruned / pre-restart / never written), send an ephemeral
           "no longer available" followup and stop.
        3. Decide state from the live content: expanded iff it contains
           :data:`_STEPS_SEP`.
        4. **Collapse** (currently expanded): keep the prefix before the
           separator; relabel the button to the collapsed ``⤵ N step(s)``
           form (count recomputed from the row for the label).
           **Expand** (currently collapsed): compute the remaining budget
           under the Discord cap, render the steps to fit, append them
           after the separator; relabel to :data:`_EXPANDED_LABEL`.
        5. Build a fresh single-button view carrying the new label (same
           static ``custom_id``) and apply both via
           ``interaction.edit_original_response``.

        Every Discord call is wrapped so an :class:`discord.HTTPException`
        is logged and swallowed — a failed edit must not raise out of the
        component callback (which would surface as an "interaction failed"
        to the user and a noisy traceback).
        """
        try:
            # Component defer: acknowledges without a visible spinner and
            # without consuming the (single) initial response slot for a
            # message send — we edit the original message afterwards.
            await interaction.response.defer()
        except discord.HTTPException:
            logger.warning(
                "steps toggle: defer failed message_id=%s; aborting toggle",
                getattr(interaction.message, "id", None),
            )
            return

        message = interaction.message
        if message is None:
            # A component interaction always carries its message; defensive.
            logger.debug("steps toggle: interaction had no message; ignoring")
            return

        row = await self._store.get_by_final_message_id(str(message.id))
        if row is None:
            try:
                await interaction.followup.send(
                    "Step details are no longer available.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                logger.warning(
                    "steps toggle: followup.send (missing-row) failed message_id=%s",
                    message.id,
                )
            return

        current = message.content or ""
        expanded_now = _STEPS_SEP in current

        if expanded_now:
            # COLLAPSE: drop everything from the separator onward.
            new_content = current.split(_STEPS_SEP, 1)[0]
            # Recompute the count from the row so the label is accurate
            # even though we don't render the steps text here.
            _text, count = render_steps(row.delta_json, limit=_DISCORD_MESSAGE_LIMIT)
            new_button_label = _collapsed_label(count)
        else:
            # EXPAND: append the rendered steps after the separator,
            # budgeting the remaining room under the Discord cap.
            reply_text = current
            limit = _DISCORD_MESSAGE_LIMIT - len(reply_text) - len(_STEPS_SEP) - _EXPAND_MARGIN
            if limit < _MIN_STEPS_BUDGET:
                steps_text = _STEPS_TOO_LONG_BLOCK
            else:
                steps_text, _count = render_steps(row.delta_json, limit=limit)
            new_content = reply_text + _STEPS_SEP + steps_text
            new_button_label = _EXPANDED_LABEL

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=new_button_label,
                custom_id=_TOGGLE_CUSTOM_ID,
            )
        )

        try:
            await interaction.edit_original_response(content=new_content, view=view)
        except discord.HTTPException:
            logger.warning(
                "steps toggle: edit_original_response failed message_id=%s expanded=%s",
                message.id,
                not expanded_now,
            )
