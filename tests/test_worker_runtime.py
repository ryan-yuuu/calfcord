"""Unit tests for the shared worker-shutdown decision in
:mod:`calfcord._worker_runtime`.

The race that matters: ``worker.run()`` raising on the *same* event-loop
wakeup as a SIGINT/SIGTERM must still surface the crash (non-zero exit), not
be masked as a clean drain (the bug the helper previously had). The shutdown
logic lives in the pure :func:`~calfcord._worker_runtime._select_exit_exception`
decision function, so these tests drive it with pre-completed futures and
assert the outcome for every ``(worker, signal)`` combination deterministically
— no timing, no flakiness.
"""

from __future__ import annotations

import asyncio

from calfcord._worker_runtime import _select_exit_exception


class _BoomError(RuntimeError):
    """Distinct exception type so a synthesized RuntimeError can't be
    mistaken for the worker's own crash in assertions."""


def _done(*, result: object = None, exc: BaseException | None = None) -> asyncio.Future:
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(result)
    return fut


def _pending() -> asyncio.Future:
    return asyncio.get_running_loop().create_future()


async def test_crash_surfaces_even_with_simultaneous_signal() -> None:
    """Regression guard: the worker crashed AND a signal fired on the same
    wakeup — the crash must win, not be drained as a clean shutdown."""
    worker = _done(exc=_BoomError("broker died"))
    stop = _done(result=True)  # a signal landed at the same time
    assert isinstance(_select_exit_exception(worker, stop, "test"), _BoomError)


async def test_crash_without_signal_surfaces() -> None:
    worker = _done(exc=_BoomError("crashed"))
    stop = _pending()
    try:
        assert isinstance(_select_exit_exception(worker, stop, "test"), _BoomError)
    finally:
        stop.cancel()


async def test_clean_return_without_signal_is_runtime_error() -> None:
    worker = _done(result=None)
    stop = _pending()
    try:
        exc = _select_exit_exception(worker, stop, "test")
        assert isinstance(exc, RuntimeError) and not isinstance(exc, _BoomError)
    finally:
        stop.cancel()


async def test_clean_return_with_signal_drains() -> None:
    """Worker happened to finish cleanly as a signal arrived — benign drain."""
    worker = _done(result=None)
    stop = _done(result=True)
    assert _select_exit_exception(worker, stop, "test") is None


async def test_signal_only_drains() -> None:
    """Only the signal fired; the worker is still running — normal drain."""
    worker = _pending()
    stop = _done(result=True)
    try:
        assert _select_exit_exception(worker, stop, "test") is None
    finally:
        worker.cancel()
