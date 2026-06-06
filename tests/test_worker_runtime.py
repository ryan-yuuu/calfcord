"""Unit tests for the shared worker-shutdown helper in
:mod:`calfcord._worker_runtime`.

Two layers are covered:

* The pure :func:`~calfcord._worker_runtime._select_exit_exception` decision
  function — driven with pre-completed futures so every ``(worker, signal)``
  combination is asserted deterministically (no timing, no flakiness). The
  race that matters there: a worker crash on the *same* event-loop wakeup as a
  SIGINT/SIGTERM must still surface (non-zero exit), not be masked as a clean
  drain.

* :func:`~calfcord._worker_runtime.run_worker_until_signal` itself, driven
  end-to-end against a fake worker with a **real** ``os.kill(SIGTERM)``. This
  guards the embedded-lifecycle contract: the helper must own SIGINT/SIGTERM
  via its own handlers and *not* hand the foreground to ``Worker.run()`` (whose
  FastStream ``set_exit`` re-registers — and so clobbers — the same signals, so
  a commanded SIGTERM would drain FastStream-side without ever setting the
  helper's stop event, mis-reported as the "returned unexpectedly" crash).
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from calfcord._worker_runtime import _select_exit_exception, run_worker_until_signal


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


class _FastStreamLikeWorker:
    """A fake :class:`~calfkit.worker.Worker` that reproduces FastStream's
    signal ownership on the ``run()`` surface but not on ``start()``/``stop()``.

    ``Worker.run()`` delegates to ``FastStream.run()``, which calls ``set_exit``
    → ``loop.add_signal_handler(SIGINT/SIGTERM, ...)``. Because
    ``add_signal_handler`` *replaces* the handler, this clobbers any handler the
    caller installed beforehand and routes a real commanded signal to
    FastStream's own clean drain — never to the caller's stop event. This fake
    mimics exactly that: a commanded signal makes ``run()`` return cleanly while
    leaving the caller's stop event untouched.

    The managed ``start()``/``stop()`` surface installs *no* signal handlers
    (matching the real Worker), so a caller that drives those keeps ownership of
    SIGINT/SIGTERM and drains on its own stop event.
    """

    def __init__(self) -> None:
        self.stop_called = False

    async def run(self) -> None:
        """Block until a signal, FastStream-style: own the signals, then drain.

        Installs its own SIGINT/SIGTERM handlers (clobbering the caller's) and
        returns cleanly when one fires — the precise behaviour that makes the
        old ``run()``-based helper synthesize the bogus "returned unexpectedly"
        RuntimeError on a real SIGTERM.
        """
        loop = asyncio.get_running_loop()
        drained = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, drained.set)
        try:
            await drained.wait()
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    async def start(self) -> None:
        """Boot without touching signals (managed/embedded surface)."""

    async def stop(self) -> None:
        """Record that the helper drained us via the managed surface."""
        self.stop_called = True


def _raise_sigterm_soon(loop: asyncio.AbstractEventLoop) -> None:
    """Schedule a real ``os.kill(SIGTERM)`` to this process on the next tick.

    A real signal (not ``stop.set()`` directly) is the whole point: it exercises
    whichever handler is actually installed on the loop at delivery time, so the
    test can tell the embedded surface (caller owns the signal) apart from the
    ``run()`` surface (FastStream clobbers it).
    """
    loop.call_soon(lambda: os.kill(os.getpid(), signal.SIGTERM))


async def test_commanded_sigterm_drains_cleanly_without_raising() -> None:
    """A real SIGTERM must drain the worker and return WITHOUT raising.

    Regression guard for the embedded rewrite: on the old ``Worker.run()``-based
    helper, FastStream's ``set_exit`` re-registers SIGTERM, so the commanded
    signal drives FastStream's clean drain, the helper's stop event never fires,
    and ``_select_exit_exception`` mis-classifies the clean ``run()`` return as
    the bogus "returned unexpectedly without a shutdown signal" crash → the
    helper raises and the process exits non-zero (a false crash that can
    spuriously restart under ``on_failure``). The embedded ``start()``/``stop()``
    helper keeps signal ownership, so the same SIGTERM drains cleanly.
    """
    worker = _FastStreamLikeWorker()
    loop = asyncio.get_running_loop()
    _raise_sigterm_soon(loop)

    # Must NOT raise. Bounded so a regression (helper hangs or never returns)
    # surfaces as a timeout rather than a hung suite.
    await asyncio.wait_for(
        run_worker_until_signal(worker, drain_label="test worker"),  # type: ignore[arg-type]
        timeout=5.0,
    )
    assert worker.stop_called, "embedded helper must drain the worker via stop()"


class _CrashOnStartWorker:
    """Fake worker whose managed boot fails — exercises the crash path of the
    embedded helper (``start()`` raising must propagate, not be swallowed)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.stop_called = False

    async def start(self) -> None:
        raise self._exc

    async def stop(self) -> None:
        self.stop_called = True


async def test_worker_boot_crash_propagates() -> None:
    """A crash during the managed boot must escape the helper so the process
    exits non-zero (supervisor restarts), and the worker is still drained."""
    worker = _CrashOnStartWorker(_BoomError("broker unreachable"))
    with pytest.raises(_BoomError, match="broker unreachable"):
        await asyncio.wait_for(
            run_worker_until_signal(worker, drain_label="test worker"),  # type: ignore[arg-type]
            timeout=5.0,
        )
