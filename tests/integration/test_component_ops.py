"""Gated REAL-binary integration test for the singleton-component ops (§2 / §12.4).

The unit tests in ``tests/supervisor/test_component.py`` drive
:mod:`calfcord.supervisor.component` with an injected fake client — they are the
must-pass contract for the *control flow* (the workspace check, the start/stop
REST call site, the absence of an agent-only duplicate guard). This module is the
complement: it runs ``component_start`` / ``component_stop`` against a real
``process-compose`` v1.110.0 binary to prove the property only a real binary can
show — that clocking a pre-declared ``disabled`` singleton slot (``tools`` and
``mcp``, the two components wired in this change) in and out goes through the real
REST handlers AND leaves the substrate PIDs untouched (the §13.1 / upstream-#494
PID-stable path the agent roster ops already prove for an agent slot).

This is the component analogue of ``tests/integration/test_roster_ops.py``: same
substrate-plus-disabled-slot stub project, same PID-stability assertion, but
exercising the *generic component* entry points (with their slot names) rather
than the agent roster ops. Tools and mcp are SINGLETONs, so there is no probe and
no duplicate guard to inject — the ops fall straight through to the real Process
Compose calls. A :class:`ProcessComposeClient` bound to the launched port is
injected so the ops' workspace check + start/stop hit the throwaway supervisor
(never a broker or ``$CALFCORD_HOME``'s derived port).

The substrate is a stub: a ``broker`` sleeper (the PID anchor) plus a
``keepalive`` sleeper that exists only so the supervisor (and its REST server)
stays up after the component slots are stopped — process-compose exits once *all*
processes are done, which would otherwise refuse the post-stop queries.

Gated behind ``CALF_TEST_PC`` with ``process-compose`` on PATH (mirrors
``tests/integration/test_roster_ops.py``); skips cleanly otherwise::

    CALF_TEST_PC=1 PATH="$HOME/.calfcord/bin:$PATH" \
        uv run pytest tests/integration/test_component_ops.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from calfcord.supervisor import component
from calfcord.supervisor.client import ProcessComposeClient

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_PC") or shutil.which("process-compose") is None,
    reason="set CALF_TEST_PC=1 with `process-compose` on PATH to run the real-binary component test",
)

# The singleton component slots the ops clock in/out — the two wired in this
# change — and the substrate process whose PID must stay stable across that (the
# §13.1 PID-stability assertion).
_COMPONENT_SLOTS = ("tools", "mcp")
_SUBSTRATE_PID_ANCHOR = "broker"

# Bounded polling for state transitions / teardown, so a wedged binary fails the
# test loudly instead of hanging it.
_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.2


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub_project(path: Path, logs: Path) -> None:
    """A substrate-plus-disabled-component-slots stub project (no broker, Discord).

    ``broker`` is the PID anchor whose stability we assert; ``keepalive`` keeps
    the supervisor's REST server up after the component slots are stopped (PC exits
    once *all* processes finish). ``tools`` / ``mcp`` are the pre-declared
    ``disabled`` singleton slots — each a sleeper with an ``exec`` readiness probe
    of ``true`` (mirroring the real renderer's component shape) that the ops clock
    in via ``POST /process/start``, the §13.1 GO path that must leave the substrate
    PIDs intact.
    """
    probe = (
        "    readiness_probe:\n"
        "      exec:\n"
        '        command: "true"\n'
        "      initial_delay_seconds: 1\n"
        "      period_seconds: 1\n"
        "      timeout_seconds: 2\n"
        "      success_threshold: 1\n"
        "      failure_threshold: 3\n"
    )
    component_blocks = "".join(
        # Each pre-declared component slot, disabled until it clocks in. It depends
        # on nothing and gates nothing, so starting it must leave the substrate
        # PIDs untouched (§13.1 GO path).
        f"  {slot}:\n"
        '    command: "sleep 3600"\n'
        "    disabled: true\n"
        f"    log_location: {logs}/{slot}.log\n"
        f"{probe}"
        for slot in _COMPONENT_SLOTS
    )
    path.write_text(
        "version: '0.5'\n"
        "processes:\n"
        "  broker:\n"
        '    command: "sleep 3600"\n'
        f"    log_location: {logs}/broker.log\n"
        "  keepalive:\n"
        '    command: "sleep 3600"\n'
        f"    log_location: {logs}/keepalive.log\n"
        f"{component_blocks}"
    )


async def _poll_until(predicate, *, timeout_s: float = _POLL_TIMEOUT_S) -> bool:
    """Await ``predicate()`` becoming truthy within ``timeout_s`` (no fixed sleep)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(_POLL_INTERVAL_S)
    return False


async def _slot_running(client: ProcessComposeClient, slot: str) -> bool:
    with contextlib.suppress(RuntimeError, KeyError, TypeError):
        return (await client.get_process(slot)).get("is_running") is True
    return False


async def _slot_stopped(client: ProcessComposeClient, slot: str) -> bool:
    with contextlib.suppress(RuntimeError, KeyError, TypeError):
        return (await client.get_process(slot)).get("is_running") is not True
    return False


async def test_component_ops_against_real_process_compose(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    logs = Path(home) / "state" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    project = Path(home) / "state" / "process-compose.yaml"
    _stub_project(project, logs)

    port = _free_port()
    proc = subprocess.Popen(
        [
            "process-compose",
            "up",
            "-f",
            str(project),
            "-D",
            "-t=false",
            "-p",
            str(port),
            "-L",
            str(logs / "process-compose.log"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Inject the port-bound client so the ops' workspace check + PC calls hit THIS
    # throwaway supervisor, not $CALFCORD_HOME's derived port. Singletons take no
    # probe, so there is nothing else to inject.
    client = ProcessComposeClient(port=port)
    try:
        # The supervisor's REST server binds a beat after the detached `up` forks;
        # wait until the ops' workspace check would pass before driving them.
        async def _supervisor_up() -> bool:
            with contextlib.suppress(RuntimeError):
                await client.project_state()
                return True
            return False

        assert await _poll_until(_supervisor_up), (
            "the detached process-compose REST server never came up"
        )

        broker_pid = (await client.get_process(_SUBSTRATE_PID_ANCHOR))["pid"]
        assert broker_pid, "the substrate anchor must expose a real OS pid"

        for slot in _COMPONENT_SLOTS:
            assert not await _slot_running(client, slot), (
                f"the `{slot}` slot must start out disabled (not running)"
            )

            # (a) component_start clocks the disabled slot in: it returns 0, the
            # slot reaches Running, and — the decisive §13.1 / #494 property — the
            # substrate anchor's PID is UNCHANGED (no substrate bounce).
            rc = await component.component_start(home, name=slot, client=client)
            assert rc == 0, f"component_start should return 0 once `{slot}` is started"
            assert await _poll_until(lambda s=slot: _slot_running(client, s)), (
                f"component_start must bring the disabled `{slot}` slot to Running"
            )
            assert (await client.get_process(_SUBSTRATE_PID_ANCHOR))["pid"] == broker_pid, (
                f"clocking the `{slot}` component in must not bounce the substrate (§13.1 / #494)"
            )

            # (b) component_stop clocks it out (the PATCH /process/stop wire): it
            # returns 0 and the slot stops, still without touching the substrate.
            rc_stop = await component.component_stop(home, name=slot, client=client)
            assert rc_stop == 0, f"component_stop should return 0 for `{slot}`"
            assert await _poll_until(lambda s=slot: _slot_stopped(client, s)), (
                f"component_stop must take the `{slot}` slot out of Running"
            )
            assert (await client.get_process(_SUBSTRATE_PID_ANCHOR))["pid"] == broker_pid, (
                f"stopping the `{slot}` component must not bounce the substrate"
            )
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["process-compose", "down", "-p", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=10)
