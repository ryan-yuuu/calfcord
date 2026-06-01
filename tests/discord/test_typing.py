"""Unit tests for the best-effort Discord typing notifier.

``TypingNotifier.fire`` is synchronous and schedules the actual ``send_typing``
REST call on a detached task (so it can never block the serial steps consumer).
Tests call ``fire`` then drain ``notifier._tasks`` to let the detached send run,
and assert on the mocked ``client.http.send_typing``.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from calfkit_organization.discord.typing import TypingNotifier

_CHANNEL_ID = 6789


def _http_exc(exc_cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic"})


def _client(*, send_typing: AsyncMock | None = None) -> MagicMock:
    """A discord.Client whose ``http.send_typing`` is an AsyncMock to assert on."""
    client = MagicMock()
    client.http = MagicMock()
    client.http.send_typing = send_typing or AsyncMock(return_value=None)
    return client


async def _drain(notifier: TypingNotifier) -> None:
    """Run every detached send task to completion, then flush done-callbacks."""
    if notifier._tasks:
        await asyncio.gather(*list(notifier._tasks))
    await asyncio.sleep(0)  # let add_done_callback discards run


class TestFire:
    async def test_fire_sends_typing_for_channel(self) -> None:
        client = _client()
        notifier = TypingNotifier(client)
        notifier.fire(_CHANNEL_ID)
        await _drain(notifier)
        client.http.send_typing.assert_awaited_once_with(_CHANNEL_ID)

    async def test_fire_is_non_blocking(self) -> None:
        """``fire`` returns immediately; the REST call runs only once the loop
        yields (proving it can't stall a synchronous caller)."""
        client = _client()
        notifier = TypingNotifier(client)
        assert notifier.fire(_CHANNEL_ID) is None
        assert client.http.send_typing.await_count == 0  # not yet run
        await _drain(notifier)
        assert client.http.send_typing.await_count == 1

    async def test_completed_task_is_discarded(self) -> None:
        client = _client()
        notifier = TypingNotifier(client)
        notifier.fire(_CHANNEL_ID)
        await _drain(notifier)
        assert notifier._tasks == set()


class TestSwallow:
    async def test_forbidden_swallowed_and_warns_once_per_channel(self, caplog: pytest.LogCaptureFixture) -> None:
        client = _client(send_typing=AsyncMock(side_effect=_http_exc(discord.Forbidden, 403)))
        notifier = TypingNotifier(client)
        with caplog.at_level(logging.WARNING, logger="calfkit_organization.discord.typing"):
            notifier.fire(_CHANNEL_ID)
            notifier.fire(_CHANNEL_ID)  # same channel → suppressed
            notifier.fire(_CHANNEL_ID + 1)  # different channel → warns again
            await _drain(notifier)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2  # once per channel, not once per call
        assert "Send Messages" in warnings[0].message

    async def test_transient_discord_error_swallowed_quietly(self, caplog: pytest.LogCaptureFixture) -> None:
        """A transient Discord error (5xx, rate-limit exhausted) is swallowed at
        DEBUG — typing fires per hop, so it must not spam WARNING/ERROR."""
        client = _client(send_typing=AsyncMock(side_effect=_http_exc(discord.HTTPException, 503)))
        notifier = TypingNotifier(client)
        with caplog.at_level(logging.DEBUG, logger="calfkit_organization.discord.typing"):
            notifier.fire(_CHANNEL_ID)
            await _drain(notifier)  # must not raise
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    async def test_unexpected_error_swallowed_but_logged_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        """A non-Discord error (mis-wired client, session closed at shutdown, a
        real bug) must not escape the detached task, but must be visible — a
        cosmetic feature should never silently bury a programming error."""
        client = _client(send_typing=AsyncMock(side_effect=RuntimeError("session closed")))
        notifier = TypingNotifier(client)
        with caplog.at_level(logging.ERROR, logger="calfkit_organization.discord.typing"):
            notifier.fire(_CHANNEL_ID)
            await _drain(notifier)  # must not raise
        assert any(r.levelno == logging.ERROR for r in caplog.records)


class TestAclose:
    async def test_aclose_cancels_pending_tasks(self) -> None:
        gate = asyncio.Event()

        async def _blocked(_channel_id: int) -> None:
            await gate.wait()

        client = _client(send_typing=AsyncMock(side_effect=_blocked))
        notifier = TypingNotifier(client)
        notifier.fire(_CHANNEL_ID)
        await asyncio.sleep(0)  # let the task start and block on the gate
        (task,) = tuple(notifier._tasks)

        await notifier.aclose()

        assert task.cancelled()

    async def test_aclose_with_no_tasks_is_noop(self) -> None:
        notifier = TypingNotifier(_client())
        await notifier.aclose()  # must not raise

    async def test_aclose_is_idempotent(self) -> None:
        notifier = TypingNotifier(_client())
        notifier.fire(_CHANNEL_ID)
        await notifier.aclose()
        await notifier.aclose()  # second call must not raise
