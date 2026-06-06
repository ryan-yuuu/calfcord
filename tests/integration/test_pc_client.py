"""Gated REAL-binary smoke test for the Process Compose REST client.

The unit tests in ``tests/supervisor/test_client.py`` pin method + path against
a stubbed transport — they are the must-pass contract. This module is the
complement: it drives :class:`ProcessComposeClient` against an *actual*
``process-compose`` server to catch the failure the stub can't — a route that
parses fine but the real binary answers ``404`` (exactly the
``/process/{name}/state`` trap §13.2 calls out). It stands up a throwaway
one-process project on a high port, then exercises list / state / restart / stop
/ logs end to end.

Gated behind ``CALF_TEST_PC`` (mirrors ``test_broker_startup_provisioning.py``'s
``CALF_TEST_KAFKA`` lane): with no flag and/or no ``process-compose`` on PATH it
skips cleanly, so it never blocks the suite on a host without the binary::

    CALF_TEST_PC=1 uv run pytest tests/integration/test_pc_client.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from calfcord.supervisor.client import ProcessComposeClient

pytestmark = pytest.mark.skipif(
    not os.getenv("CALF_TEST_PC") or shutil.which("process-compose") is None,
    reason="set CALF_TEST_PC=1 with `process-compose` on PATH to run the real-binary smoke test",
)

# A long-lived no-op so the process stays Running long enough to query/restart.
_SLEEPER = f"{sys.executable} -c 'import time; time.sleep(3600)'"
_READY_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.2


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_project(path: Path) -> None:
    # Two processes: `sleeper` is the one we drive (restart/stop/logs), and
    # `keepalive` exists only so the supervisor (and its REST server) stays up
    # after `sleeper` is stopped — process-compose exits once *all* processes are
    # done, which would otherwise refuse the post-stop log/ghost queries.
    path.write_text(
        "version: '0.5'\n"
        "processes:\n"
        "  sleeper:\n"
        f"    command: \"{_SLEEPER}\"\n"
        "  keepalive:\n"
        f"    command: \"{_SLEEPER}\"\n"
    )


async def _wait_running(client: ProcessComposeClient, name: str) -> None:
    deadline = asyncio.get_event_loop().time() + _READY_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
        with contextlib.suppress(RuntimeError):
            state = await client.get_process(name)
            if state.get("is_running") or state.get("status") == "Running":
                return
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"{name} never reached Running within {_READY_TIMEOUT_S}s")


async def test_client_drives_a_real_process_compose(tmp_path: Path) -> None:
    port = _free_port()
    project = tmp_path / "process-compose.yaml"
    _write_project(project)
    logs = tmp_path / "pc.log"

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
            str(logs),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = ProcessComposeClient(port=port)
    try:
        await _wait_running(client, "sleeper")

        # list/state: the project's one process is present and Running.
        listed = await client.list_processes()
        assert listed is not None
        state = await client.get_process("sleeper")
        assert state.get("status") == "Running" or state.get("is_running")

        # config + project state routes answer (not 404).
        assert (await client.get_process_info("sleeper")) is not None
        assert (await client.project_state()) is not None

        # restart then stop drive the lifecycle through the real binary, proving
        # restart=POST and stop=PATCH hit the right handlers.
        await client.restart_process("sleeper")
        await _wait_running(client, "sleeper")
        await client.stop_process("sleeper")

        # logs route returns a bounded window without erroring.
        assert (await client.get_logs("sleeper", 0, 10)) is not None

        # a missing process surfaces as a loud RuntimeError, not a silent {}.
        with pytest.raises(RuntimeError):
            await client.get_process("ghost")
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["process-compose", "down", "-p", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=10)
