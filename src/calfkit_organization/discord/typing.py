"""Best-effort Discord typing indicator for the bridge.

Discord's only live-activity affordance is the channel typing indicator
("*X is typing…*"), triggered by ``POST /channels/{channel_id}/typing``
(:meth:`discord.http.HTTPClient.send_typing`). It expires after ~10 seconds and
has no "stop" counterpart, so the bridge simply (re)fires it on each unit of
agent work for a correlation and lets it lapse on its own. A short tail after
the final reply is expected and accepted: replies post via webhooks (a
different identity than the bot user that types), so nothing the bridge posts
can actively clear the indicator.

:class:`TypingNotifier` wraps that one call with the project's
fire-and-forget + swallow-Discord-errors discipline:

* **Fire-and-forget.** :meth:`fire` is synchronous and returns immediately,
  scheduling the REST call on a detached :class:`asyncio.Task`. This is
  deliberate: the bridge's steps consumer subscribes to the single-partition
  ``agent.steps`` topic and processes hops *serially*, so an awaited
  ``send_typing`` that hit discord.py's rate limiter (which ``asyncio.sleep``s
  inside the request when ``max_ratelimit_timeout`` is unset — and the shared
  REST client leaves it unset) would stall the live-progress UI for **every**
  channel. Detaching the call keeps any rate-limit sleep off the consumer's
  critical path. Detached tasks are held in a set (with a done-callback that
  discards them) so they are not garbage-collected mid-flight.
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
        self._warned_forbidden = False
        logger.info("typing indicator enabled")

    def fire(self, channel_id: int) -> None:
        """Schedule a typing indicator for ``channel_id`` and return immediately.

        Synchronous and non-blocking: the REST call runs on a detached task, so
        a rate-limit sleep can never stall the caller (notably the serial
        ``agent.steps`` consumer). Safe to call from inside a ``try`` whose
        ``except`` would otherwise misread a typing error as a handler failure —
        any error lives on the detached task, not the caller.
        """
        task = asyncio.create_task(self._send(channel_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, channel_id: int) -> None:
        """Issue the single ``POST /channels/{id}/typing`` call, swallowing errors."""
        try:
            await self._client.http.send_typing(channel_id)
        except discord.Forbidden:
            if not self._warned_forbidden:
                self._warned_forbidden = True
                logger.warning(
                    "typing indicator forbidden in channel_id=%d; the bot user needs "
                    "Send Messages (and Send Messages in Threads for task threads) here, "
                    "separate from Manage Webhooks. Typing is skipped where denied.",
                    channel_id,
                )
            else:
                logger.debug("typing: forbidden channel_id=%d (suppressed)", channel_id)
        except Exception as e:
            # Cosmetic side effect — must never propagate (e.g. a session-closed
            # error during shutdown) or affect message handling.
            logger.debug("typing: send_typing failed channel_id=%d: %s", channel_id, e)

    async def aclose(self) -> None:
        """Cancel any in-flight typing tasks. Idempotent; called on bridge teardown."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
