"""Live-progress renderer for the caller surface (spec §5.2).

The bridge's :class:`~calfcord.bridge.mention_handler.MentionHandler` drains a
run's ``stream()`` and, for every non-A2A :class:`~calfcord.bridge.step_events.StepEvent`,
calls :meth:`ProgressRenderer.on_step`; a ``finally`` always calls
:meth:`ProgressRenderer.finish`. This class turns that stream into a single
transient in-channel "progress" message per correlation — the live trace
itself (model text / ``tool_name(args)`` / ``⎿ result`` lines, no header),
posted under the emitting agent's persona, edited (debounced) as steps stream
in, and deleted once the run ends.

It replaces the old Kafka ``agent.steps`` consumer, but keeps that consumer's
hard-won UI lifecycle. What it deliberately DROPS (no longer reachable on the
caller surface):

* **No cursor / message-history walking.** Steps arrive pre-normalized and
  one-at-a-time; :func:`~calfcord.bridge.steps_render.render_step_line` renders
  each. There is no delta slice to de-dupe.
* **No completed-set / outbox-retry guard, no LRU.** The drain loop's
  ``finally`` → :meth:`finish` deterministically removes each correlation's
  entry exactly once, so a plain ``dict`` suffices and there is no retry to
  suppress.
* **No flash / terminal-first-hop special case.** The terminal answer never
  arrives as a step (it rides ``handle.result()``, posted by the reply poster),
  so any correlation that reaches :meth:`on_step` with renderable content did
  real intermediate work — there is nothing to immediately post-then-delete.

**Lifecycle.** Post lazily on the first renderable step (a turn whose steps all
render empty posts nothing); debounce subsequent edits into one
``edit_message`` per :data:`~calfcord.bridge.steps_render._PROGRESS_DEBOUNCE_SECONDS`
window; on :meth:`finish` cancel any pending edit and delete the message.

**Failure semantics.** Every Discord call is best-effort
(:func:`_best_effort_progress`): a transient/gone progress message must never
crash the run or affect the terminal reply. A failed POST leaves
``progress_message_id`` unset so the next renderable step retries it.

**Persona** is resolved per step from ``persona_for(step.emitter)`` — never
cached on the entry — so the node that actually did the work (the peer, after a
handoff) stamps the progress message. **Typing** is fired (best-effort,
fire-and-forget) for every step into the thread the conversation lives in, else
the parent channel.

**State loss on restart.** ``self._entries`` is process-local; a restart mid-run
strands the entry and its transient message lingers (cosmetic). The run's
terminal reply posts independently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from calfcord.bridge.persona_resolve import persona_for
from calfcord.bridge.steps_render import (
    _PROGRESS_DEBOUNCE_SECONDS,
    _progress_content,
    render_step_line,
)

if TYPE_CHECKING:
    from calfcord.bridge.mention_handler import MentionRequest
    from calfcord.bridge.step_events import StepEvent
    from calfcord.discord.persona import DiscordPersonaSender, Persona
    from calfcord.discord.typing import TypingNotifier

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProgressEntry:
    """Per-correlation state for one in-flight run's progress message.

    ``thread_id`` routes the message INTO a thread the conversation originated
    in (the persona webhook still hosts on the parent ``channel_id``); ``None``
    for a top-level channel. ``progress_message_id`` stays ``None`` until the
    first renderable step posts — a run whose steps all render empty never posts.
    ``rendered_lines`` accumulates the compact render of every renderable step;
    the message body is a tail-windowed join of these. ``debounce_task`` is the
    single in-flight trailing-edit handle (or ``None``); subsequent steps reuse
    it (it re-renders ``rendered_lines`` at fire time).

    ``slots=True`` catches typo-creates-new-attribute bugs.
    """

    channel_id: int
    thread_id: int | None
    progress_message_id: int | None = None
    rendered_lines: list[str] = field(default_factory=list)
    debounce_task: asyncio.Task[None] | None = None


async def _best_effort_progress[T](coro: Awaitable[T], *, action: str, key_label: str, key_value: int) -> T | None:
    """Await a best-effort progress-message Discord call, swallowing the usual
    failures so a transient/gone message can never crash the run. Returns the
    call's result, or ``None`` if it failed.

    ``NotFound`` (already gone) is DEBUG; ``Forbidden`` and the broader
    ``DiscordException`` (which also funnels the sibling ``RateLimited``, NOT a
    subclass of ``HTTPException``) are WARNING. ``CancelledError`` is a
    ``BaseException`` and is intentionally not caught, so shutdown stays clean.
    """
    try:
        return await coro
    except discord.NotFound:
        logger.debug("progress: %s hit NotFound %s=%d (already gone)", action, key_label, key_value)
    except discord.Forbidden:
        logger.warning("progress: %s Forbidden %s=%d", action, key_label, key_value)
    except discord.DiscordException as e:
        logger.warning(
            "progress: %s failed %s=%d status=%s: %s",
            action,
            key_label,
            key_value,
            getattr(e, "status", None),
            e,
        )
    return None


class ProgressRenderer:
    """Stateful Discord lifecycle for the live-progress message.

    Satisfies the ``ProgressRenderer`` protocol the
    :class:`~calfcord.bridge.mention_handler.MentionHandler` injects. Construct
    once per bridge process from the REST-only persona sender and (optionally)
    a typing notifier.
    """

    def __init__(self, persona_sender: DiscordPersonaSender, typing_notifier: TypingNotifier | None = None) -> None:
        self._persona_sender = persona_sender
        self._typing = typing_notifier
        # Plain dict — finish() removes each correlation deterministically, so
        # there is no eviction pressure and no retry to suppress.
        self._entries: dict[str, ProgressEntry] = {}

    async def on_step(self, step: StepEvent, req: MentionRequest) -> None:
        """Reflect one renderable step into the correlation's progress message.

        Seeds the entry lazily, fires typing (work is happening, regardless of
        whether this step renders anything visible), then renders the step. A
        step that renders nothing (a whitespace-only preamble) appends no line
        and posts nothing. Otherwise the line is appended and the message is
        posted (first renderable step) or a debounced edit is scheduled.
        """
        entry = self._entries.setdefault(
            step.correlation_id,
            ProgressEntry(
                channel_id=req.channel_id,
                thread_id=(req.source_channel_id if req.source_channel_id != req.channel_id else None),
            ),
        )
        # Typing reflects that the agent is working, independent of whether this
        # step renders to anything — fire-and-forget so it never blocks the drain.
        # Targets the thread the wire originated in, else the parent channel (the
        # surface the user is reading; typing addresses that id directly, unlike
        # the webhook post which addresses the parent and routes via thread_id).
        if self._typing is not None:
            self._typing.fire(entry.thread_id or entry.channel_id)

        line = render_step_line(step)
        if not line:
            return
        # Accumulate BEFORE the post/edit so it renders the up-to-date trace.
        entry.rendered_lines.append(line)
        if entry.progress_message_id is None:
            # First renderable step: post under the step's emitter persona.
            await self._post(entry, persona_for(step.emitter))
        else:
            self._schedule_debounced_edit(entry)

    async def finish(self, correlation_id: str) -> None:
        """Tear down the correlation's progress message — runs on success AND
        fault (the handler calls it in a ``finally``).

        Pops the entry, cancels and awaits any pending debounce edit (so no late
        edit races the delete), and deletes the progress message if one was
        posted. A no-op for a correlation that never produced a renderable step
        (no entry, or an entry that never posted).
        """
        entry = self._entries.pop(correlation_id, None)
        if entry is None:
            return
        await self._cancel_debounce(entry)
        await self._delete(entry)

    async def _post(self, entry: ProgressEntry, persona: Persona) -> None:
        """Post the transient progress message for the first renderable step.

        Routes into the thread via ``thread_id`` when the wire originated in one
        (``None`` ⇒ posts to ``channel_id``). Best-effort — on a Discord failure
        the id stays ``None`` so the next renderable step retries the post.
        """
        sent = await _best_effort_progress(
            self._persona_sender.send(
                persona=persona,
                channel_id=entry.channel_id,
                content=_progress_content(entry.rendered_lines),
                thread_id=entry.thread_id,
            ),
            action="post",
            key_label="channel_id",
            key_value=entry.channel_id,
        )
        if sent is not None:
            entry.progress_message_id = sent.id

    async def _edit(self, entry: ProgressEntry) -> None:
        """Edit the progress message to the entry's CURRENT trace.

        Re-renders from ``entry.rendered_lines`` at call time, so a debounced
        fire reflects every line appended while it slept. Best-effort; a deleted
        message (``NotFound``) is ignored at DEBUG.
        """
        message_id = entry.progress_message_id
        if message_id is None:
            return
        await _best_effort_progress(
            self._persona_sender.edit_message(
                entry.channel_id,
                message_id,
                content=_progress_content(entry.rendered_lines),
                thread_id=entry.thread_id,
            ),
            action="edit",
            key_label="message_id",
            key_value=message_id,
        )

    def _schedule_debounced_edit(self, entry: ProgressEntry) -> None:
        """Ensure exactly one trailing-debounce edit task is pending.

        If the entry already has a live (not-done) debounce task, return — that
        task re-renders ``entry.rendered_lines`` when it fires, so the line this
        step just appended is picked up for free. Otherwise spawn one that
        sleeps :data:`~calfcord.bridge.steps_render._PROGRESS_DEBOUNCE_SECONDS`
        then edits.
        """
        existing = entry.debounce_task
        if existing is not None and not existing.done():
            return

        async def _run() -> None:
            await asyncio.sleep(_PROGRESS_DEBOUNCE_SECONDS)
            await self._edit(entry)

        entry.debounce_task = asyncio.create_task(_run())

    async def _cancel_debounce(self, entry: ProgressEntry) -> None:
        """Cancel and await the entry's pending debounce task, if any.

        Suppresses ``CancelledError`` so a mid-sleep cancel is silent. Awaiting
        guarantees the task is fully torn down before :meth:`finish` deletes the
        progress message — no late edit can race the delete.
        """
        task = entry.debounce_task
        if task is None:
            return
        entry.debounce_task = None
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _delete(self, entry: ProgressEntry) -> None:
        """Delete the transient progress message. Only acts when one was posted;
        best-effort, an already-deleted message (``NotFound``) is ignored at DEBUG."""
        message_id = entry.progress_message_id
        if message_id is None:
            return
        await _best_effort_progress(
            self._persona_sender.delete_message(entry.channel_id, message_id, thread_id=entry.thread_id),
            action="delete",
            key_label="message_id",
            key_value=message_id,
        )
