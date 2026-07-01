"""Best-effort Discord typing indicator for the bridge.

Discord's only live-activity affordance is the channel typing indicator
("*X is typing…*"), triggered by ``POST /channels/{channel_id}/typing``
(:meth:`discord.http.HTTPClient.send_typing`). It expires after ~10 seconds and
has no "stop" counterpart, so the bridge simply (re)fires it on each unit of
agent work for a correlation and lets it lapse on its own. Because replies post
via webhooks (a different identity than the bot user that types), nothing the
bridge posts can actively clear the indicator, so it persists up to ~10s from
the last fire — a short, accepted tail once the reply lands.

:class:`TypingNotifier` wraps that one call with the project's
fire-and-forget + swallow-Discord-errors discipline:

* **Fire-and-forget.** :meth:`fire` is synchronous and returns immediately,
  scheduling the REST call on a detached :class:`asyncio.Task`. This is
  deliberate: under the 0.12 caller surface the bridge's progress renderer
  drains the run's ``stream()`` and processes hops *serially* on the bridge's
  event loop (there are no consumers and no ``agent.steps`` topic), so an
  awaited ``send_typing`` that hit discord.py's rate limiter (which
  ``asyncio.sleep``s inside the request when ``max_ratelimit_timeout`` is
  unset — and the shared REST client leaves it unset) would stall the
  live-progress UI for **every** channel. Detaching the call keeps any
  rate-limit sleep off the renderer's critical path. Detached tasks are held
  in a set (with a done-callback that discards them) so they are not
  garbage-collected mid-flight.
* **Best-effort.** Typing is purely cosmetic; a failure must never propagate or
  crash a hop. Every error is logged and swallowed. ``CancelledError`` is a
  ``BaseException`` (not caught), so shutdown cancellation stays clean.

**Identity & permissions.** The indicator is triggered as the **bot user**, not
as the agent persona — Discord webhooks (which the personas use) cannot type,
so the indicator always shows the bot's own name. Triggering typing also
requires the bot user to hold ``Send Messages`` in the channel (and
``Send Messages in Threads`` for ``/task`` threads), which is **separate** from
the ``Manage Webhooks`` permission the persona sender relies on. A bot granted
only ``Manage Webhooks`` gets ``403 Forbidden`` on every typing call; that is
swallowed (the indicator simply never shows), with the first occurrence logged
at WARNING so the misconfiguration is discoverable.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import discord

logger = logging.getLogger(__name__)


class TypingNotifier:
    """Fire-and-forget Discord typing indicators, best-effort.

    Construct once per bridge process from a started, REST-capable
    :class:`discord.Client` (e.g. :attr:`DiscordPersonaSender.client`) and call
    :meth:`fire` with the id of the channel (or thread) the indicator should
    appear in. See the module docstring for the fire-and-forget rationale and
    the bot-user permission requirement.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client
        self._tasks: set[asyncio.Task[None]] = set()
        # Channels we've already warned about a typing 403 in — warn once per
        # channel (bounded by the guild's channel count) so a per-channel
        # permission override stays discoverable, without per-hop log spam.
        self._forbidden_channels: set[int] = set()
        logger.info("typing indicator enabled")

    def fire(self, channel_id: int) -> None:
        """Schedule a typing indicator for ``channel_id`` and return immediately.

        Synchronous and non-blocking: the REST call runs on a detached task, so
        a rate-limit sleep can never stall the caller (notably the serial
        progress renderer). Safe to call from inside a ``try`` whose
        ``except`` would otherwise misread a typing error as a handler failure —
        any error lives on the detached task, not the caller.
        """
        try:
            task = asyncio.create_task(self._send(channel_id))
        except RuntimeError:
            # No running event loop to schedule onto. Keeps the "fire never
            # raises into its caller" contract unconditional (every real call
            # site runs inside the bridge's loop, so this is belt-and-braces).
            logger.debug("typing: no running event loop; skipping channel_id=%d", channel_id)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, channel_id: int) -> None:
        """Issue the single ``POST /channels/{id}/typing`` call, swallowing errors.

        Tiered so a *cosmetic* failure never crashes a hop, yet a real bug is
        not buried: permission denials warn once per channel, transient Discord
        errors stay at DEBUG, and anything unexpected (a mis-wired client, a
        programming error) is logged with a stack trace.
        """
        try:
            # discord.py exposes no public one-shot "trigger typing" for a bare
            # channel id (``Messageable.typing()`` is a repeating context
            # manager); ``http.send_typing`` is the intended primitive, and is
            # stable across discord.py 2.x.
            await self._client.http.send_typing(channel_id)
        except discord.Forbidden:
            # Missing Send Messages / Send Messages in Threads (separate from the
            # Manage Webhooks the personas use). Operator-actionable, so warn —
            # but once per channel, since typing fires on every hop.
            if channel_id not in self._forbidden_channels:
                self._forbidden_channels.add(channel_id)
                logger.warning(
                    "typing indicator forbidden in channel_id=%d; the bot user needs "
                    "Send Messages (and Send Messages in Threads for task threads) here, "
                    "separate from Manage Webhooks. Typing is skipped where denied.",
                    channel_id,
                )
            else:
                logger.debug("typing: forbidden channel_id=%d (suppressed)", channel_id)
        except discord.DiscordException as e:
            # Transient Discord-side failure (5xx, rate-limit exhausted, channel
            # gone). Cosmetic and self-resolving, and typing fires per hop, so
            # DEBUG keeps the noise down.
            logger.debug("typing: send_typing failed channel_id=%d: %s", channel_id, e)
        except Exception:
            # Not a Discord API error — a mis-wired client, a session closed at
            # teardown, or a genuine bug. Swallowed so a cosmetic call can't
            # crash a hop, but logged loudly so it is never invisible.
            logger.exception("typing: unexpected error firing typing for channel_id=%d", channel_id)

    async def aclose(self) -> None:
        """Cancel any in-flight typing tasks. Idempotent; called on bridge teardown."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
