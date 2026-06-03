"""Shared worker-lifetime helper: run a :class:`Worker` until a signal.

Several calfcord deployments (the tools runner, the MCP bridge runner)
share one shutdown contract: spawn ``worker.run()`` as a foreground task,
wait for either it to finish or a SIGINT/SIGTERM, and treat *any* exit of
``worker.run()`` that is **not** preceded by a shutdown signal as a crash
worth surfacing to the process supervisor. This module is the single
source of truth for that contract so the runners cannot drift apart.

It is intentionally transport-agnostic and dependency-light (it imports
only :class:`~calfkit.worker.Worker` for typing), so both the
credential-free tools runner and the bridge runner can share it without
either pulling the other's dependencies.

The supervisor invariant
-------------------------

A clean return from ``worker.run()`` *without* a shutdown signal is
unexpected — under normal operation the only way out is a signal. If we
let that case exit 0, a supervisor configured for ``Restart=on-failure``
would leave the process down. So a signal-less clean return is converted
into a :class:`RuntimeError` and raised, exactly as a runtime crash would
be, to force a non-zero exit and a restart.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from calfkit.worker import Worker

logger = logging.getLogger(__name__)


def _select_exit_exception(
    worker_task: asyncio.Future,
    stop_task: asyncio.Future,
    drain_label: str,
) -> BaseException | None:
    """Decide the process-exit outcome after the run/stop race resolves.

    Returns the exception to re-raise (forcing a non-zero exit so a
    supervisor restarts us), or ``None`` for a clean drain. Factored out as
    a pure decision function so the otherwise timing-dependent race between a
    worker crash and a shutdown signal is deterministically testable —
    callers (tests) can pass pre-completed futures.

    A done ``worker_task`` always means the worker exited on its own: the
    signal handlers only ``stop.set()`` (completing ``stop_task``), never
    ``worker_task``. So the precedence is:

    1. ``worker_task`` done **with an exception** → surface it (a crash wins,
       even if ``stop_task`` is also done from a signal on the same wakeup);
    2. done **cleanly, with a signal** → benign shutdown, drain;
    3. done **cleanly, no signal** → unexpected; synthesize a
       :class:`RuntimeError` (the supervisor invariant in the module docstring);
    4. ``worker_task`` **not** done → only the signal fired, drain.
    """
    if not worker_task.done():
        logger.info("shutdown signal received, draining %s", drain_label)
        return None
    worker_exc = worker_task.exception()
    if worker_exc is not None:
        logger.error("worker crashed during runtime; exiting non-zero", exc_info=worker_exc)
        return worker_exc
    if stop_task.done():
        logger.info("shutdown signal received, draining %s", drain_label)
        return None
    runtime_error = RuntimeError("worker.run() returned unexpectedly without a shutdown signal")
    logger.error("%s; exiting non-zero", runtime_error)
    return runtime_error


async def run_worker_until_signal(worker: Worker, *, drain_label: str = "worker") -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Spawns ``worker.run()`` as a foreground task alongside a stop-event
    task armed by the SIGINT/SIGTERM handlers, and waits for whichever
    completes first:

    * **Signal first** — log "shutdown signal received" and drain: cancel
      the still-running worker task and await both tasks to teardown.
    * **Worker finished first** — inspect why. A propagated exception is
      re-raised (runtime crash), and that takes precedence even if a shutdown
      signal arrived on the same event-loop wakeup. A clean return with no
      pending signal is *unexpected* and synthesized into a
      :class:`RuntimeError` (see module docstring's supervisor invariant),
      which is then re-raised; a clean return that coincides with a signal is
      a benign shutdown and drains normally. See :func:`_select_exit_exception`.

    Args:
        worker: The calfkit :class:`~calfkit.worker.Worker` to run.
        drain_label: Human-readable label for the worker, used in the
            "shutdown signal received, draining {drain_label}" log line so
            a multi-process deployment's logs identify which worker is
            draining (e.g. ``"tools worker"``, ``"mcp bridge worker"``).

    Raises:
        BaseException: Whatever ``worker.run()`` raised, if it crashed; or
            a :class:`RuntimeError` if it returned cleanly without a
            shutdown signal.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    worker_task = asyncio.create_task(worker.run())
    stop_task = asyncio.create_task(stop.wait())
    worker_exc: BaseException | None = None
    try:
        await asyncio.wait(
            {worker_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # A done ``worker_task`` always means the worker exited on its own —
        # a signal only sets the stop event, it never completes that task.
        # ``_select_exit_exception`` therefore surfaces a worker crash even
        # when a SIGINT/SIGTERM lands on the same event-loop wakeup; a clean
        # exit 0 would otherwise mask the crash from the process supervisor.
        worker_exc = _select_exit_exception(worker_task, stop_task, drain_label)
    finally:
        for t in (worker_task, stop_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(worker_task, stop_task, return_exceptions=True)

    if worker_exc is not None:
        raise worker_exc
