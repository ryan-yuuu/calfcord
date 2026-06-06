"""Shared worker-lifetime helper: run a :class:`Worker` until a signal.

Every roster runner (agents / tools / router / MCP bridge) shares one
shutdown contract: bring the worker up, wait for either it to fail or a
SIGINT/SIGTERM, and treat *any* worker exit that is **not** preceded by a
shutdown signal as a crash worth surfacing to the process supervisor. This
module is the single source of truth for that contract so the runners cannot
drift apart.

It is intentionally transport-agnostic and dependency-light (it imports
only :class:`~calfkit.worker.Worker` for typing), so both the
credential-free tools runner and the bridge runner can share it without
either pulling the other's dependencies.

Why the embedded ``start()``/``stop()`` surface, not ``run()``
--------------------------------------------------------------

This helper drives the worker via :meth:`Worker.start` / :meth:`Worker.stop`
and owns SIGINT/SIGTERM with its *own* handlers — the same embedded pattern
the bridge uses. It deliberately does **not** use :meth:`Worker.run`.

``Worker.run()`` delegates to FastStream's ``run()``, whose first act is
``set_exit(...)`` → ``loop.add_signal_handler(SIGINT/SIGTERM, ...)``. Because
``add_signal_handler`` *replaces* the existing handler, calling ``run()``
after we installed our own handlers silently clobbers ours: a commanded
``SIGTERM`` would then fire only FastStream's handler, draining the worker and
returning ``run()`` cleanly — but our stop event would never be set, so the
clean return reads as the "returned unexpectedly" crash below and the process
exits non-zero (a false crash that can spuriously restart under
``Restart=on-failure``). The embedded surface installs no signal handlers, so
ours survive and a commanded signal drains cleanly (exit 0).

The supervisor invariant
-------------------------

A worker that finishes *without* a shutdown signal is unexpected — under
normal operation the only way out is a signal. If we let that case exit 0, a
supervisor configured for ``Restart=on-failure`` would leave the process
down. So a signal-less worker exit (a boot crash, or a serving loop that ends
on its own) is converted into a re-raised exception — the worker's own crash,
or a synthesized :class:`RuntimeError` for a clean-but-signal-less exit — to
force a non-zero exit and a restart.
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

    Here ``worker_task`` wraps :meth:`Worker.start` followed by an indefinite
    serve (see :func:`run_worker_until_signal`): a boot failure completes it
    with the raised exception (case 1); a successful boot leaves it pending so
    only the signal can complete the race (case 4). The clean-no-signal case
    (3) cannot arise from the normal embedded path — the serve never returns on
    its own — but the contract is preserved verbatim so any future surface that
    *can* return cleanly still trips the supervisor invariant rather than
    exiting 0.
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
    runtime_error = RuntimeError("worker serve returned unexpectedly without a shutdown signal")
    logger.error("%s; exiting non-zero", runtime_error)
    return runtime_error


async def _serve(worker: Worker) -> None:
    """Boot the worker (managed surface) and then serve until cancelled.

    :meth:`Worker.start` returns as soon as the broker is up and the nodes are
    consuming in the background; the serve is event-driven from there with no
    awaitable to block on. We model the worker's *serving lifetime* as a task
    that boots and then parks indefinitely, so the caller can race it against
    the stop event:

    * a boot failure completes this task with the raised exception — surfaced
      as a crash by :func:`_select_exit_exception`;
    * a successful boot parks here forever, so only a shutdown signal can
      complete the race; the caller then drains via :meth:`Worker.stop`.

    The park (``asyncio.Event().wait()`` on a never-set event) holds the
    serving open until the caller cancels this task in its ``finally``.
    """
    await worker.start()
    await asyncio.Event().wait()  # park until cancelled by the caller's drain


async def run_worker_until_signal(worker: Worker, *, drain_label: str = "worker") -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Drives the worker via the embedded :meth:`Worker.start` / :meth:`Worker.stop`
    surface (signals stay OFF, so FastStream never re-registers — and so never
    clobbers — our handlers; see the module docstring) and owns SIGINT/SIGTERM
    with its own handlers. A serve task (:func:`_serve`) boots the worker and
    then parks; it is raced against a stop-event task armed by the signal
    handlers, and whichever completes first decides the outcome:

    * **Signal first** — log "shutdown signal received" and drain: cancel the
      still-parked serve task and :meth:`Worker.stop` the worker in ``finally``.
    * **Serve finished first** — inspect why. A propagated exception is re-raised
      (a boot crash), and that takes precedence even if a shutdown signal
      arrived on the same event-loop wakeup. A clean finish with no pending
      signal is *unexpected* and synthesized into a :class:`RuntimeError` (see
      the module docstring's supervisor invariant), which is then re-raised; a
      clean finish that coincides with a signal is a benign shutdown and drains
      normally. See :func:`_select_exit_exception`.

    The worker is always drained via :meth:`Worker.stop` in ``finally`` — a
    no-op if it never started, idempotent otherwise — so neither a failed boot
    nor a clean run ever leaks the broker connection.

    Args:
        worker: The calfkit :class:`~calfkit.worker.Worker` to run.
        drain_label: Human-readable label for the worker, used in the
            "shutdown signal received, draining {drain_label}" log line so
            a multi-process deployment's logs identify which worker is
            draining (e.g. ``"tools worker"``, ``"mcp bridge worker"``).

    Raises:
        BaseException: Whatever :meth:`Worker.start` raised, if boot crashed; or
            a :class:`RuntimeError` if the serve finished cleanly without a
            shutdown signal.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Install OUR handlers before booting. ``Worker.start()`` never touches
    # signals, so these survive the whole run — a SIGTERM during a slow boot
    # still sets the stop event and the boot is then cancelled+drained cleanly.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    worker_task = asyncio.create_task(_serve(worker))
    stop_task = asyncio.create_task(stop.wait())
    worker_exc: BaseException | None = None
    try:
        await asyncio.wait(
            {worker_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # A done ``worker_task`` always means the worker exited on its own —
        # a signal only sets the stop event, it never completes that task.
        # ``_select_exit_exception`` therefore surfaces a boot crash even
        # when a SIGINT/SIGTERM lands on the same event-loop wakeup; a clean
        # exit 0 would otherwise mask the crash from the process supervisor.
        worker_exc = _select_exit_exception(worker_task, stop_task, drain_label)
    finally:
        for t in (worker_task, stop_task):
            if not t.done():
                t.cancel()
        # Gather the cancelled serve/stop tasks (retrieve their results so no
        # "Task was destroyed but it is pending" warning leaks), then drain the
        # worker. ``stop()`` is a no-op if the worker never started (boot raised
        # before ``start()`` completed) and idempotent otherwise, so it is safe
        # on every path — clean signal, boot crash, or unexpected serve return.
        await asyncio.gather(worker_task, stop_task, return_exceptions=True)
        await worker.stop()

    if worker_exc is not None:
        raise worker_exc
