"""Unit tests for ``calfkit-tools`` runner helpers.

Covers the tool-registry guard (``_resolve_tool_nodes``), the
supervisor-restart shutdown contract (``_run_worker``), and the runner's
one A2A-specific responsibility: exposing the process-wide calfkit
``Client`` as a worker-scoped resource so the ``private_chat`` tool body
can reach it via ``ctx.resources``. The Discord connection itself is no
longer the runner's concern — it is built by ``private_chat``'s own
node-scoped ``@resource`` bracket (see
``tests/tools/test_private_chat.py::TestA2AResource``), so the per-call response
timeout and audit-channel env resolvers now live with that tool and are
tested there.

The full ``_amain`` requires Discord auth, a Kafka broker, and an agents
directory — too heavy for a unit test; the client-exposure test patches
those boundaries.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.client import Client
from calfkit.worker import Worker

from calfcord.tools import private_chat, runner


class TestResolveToolNodes:
    def test_returns_nodes_from_populated_registry(self) -> None:
        node = MagicMock()
        result = runner._resolve_tool_nodes({"private_chat": node})
        assert result == [node]

    def test_empty_registry_fails_fast(self) -> None:
        """The empty-registry guard exists specifically to prevent the
        worker from starting in an inert state — subscribed to nothing,
        responding to nothing, but otherwise looking healthy in logs."""
        with pytest.raises(SystemExit, match="empty"):
            runner._resolve_tool_nodes({})

    def test_empty_registry_message_names_include_filter_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty-registry is most often caused by a typo in
        ``CALFCORD_TOOLS_INCLUDE`` (per-tool images). The SystemExit
        message must NAME the env var and surface its value so the
        operator can short-circuit a ``why is my registry empty`` hunt.
        A regression that strips the env-var attribution would pass
        the broader ``match='empty'`` guard above but silently revert
        the cleanup's UX improvement — this test pins it."""
        monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "definitely_not_a_real_tool")
        with pytest.raises(SystemExit) as exc_info:
            runner._resolve_tool_nodes({})
        message = str(exc_info.value)
        assert "CALFCORD_TOOLS_INCLUDE=" in message
        assert "definitely_not_a_real_tool" in message

    def test_empty_registry_message_handles_unset_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``CALFCORD_TOOLS_INCLUDE`` is unset, the message must
        still surface the env var with an explicit ``<unset>`` marker
        rather than an ambiguous empty string — a future log-aggregation
        regex that anchors on ``CALFCORD_TOOLS_INCLUDE=\\S+`` would
        otherwise miss the unset case silently."""
        monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            runner._resolve_tool_nodes({})
        message = str(exc_info.value)
        assert "CALFCORD_TOOLS_INCLUDE=<unset>" in message


class TestAmainExposesClientResource:
    """The runner's sole A2A wiring responsibility after the 0.6.0 migration:
    expose the process-wide calfkit ``Client`` as the worker-scoped
    ``a2a_client`` resource. ``private_chat``'s body reads it from
    ``ctx.resources`` (merged under its own node-scoped Discord bundle), so a
    regression that dropped this line would break A2A at runtime — pin it.
    Discord is built by the tool's own ``@resource`` bracket, so the runner
    constructs none here.
    """

    def test_resource_key_matches_private_chat(self) -> None:
        """Producer (runner) and consumer (private_chat) must agree on the
        worker-resource key. The runner imports the constant from private_chat,
        so they're the same object — pin that single-source-of-truth so a future
        edit that re-hardcodes the literal here is caught (mirrors the
        cross-module symbol-parity guards elsewhere)."""
        assert runner._A2A_CLIENT_RESOURCE is private_chat._RES_CLIENT

    async def test_connected_client_is_registered_as_worker_resource(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock(spec=Client)
        captured: dict[str, object] = {}

        @asynccontextmanager
        async def _fake_connect(*args, **kwargs):
            captured["connect_kwargs"] = kwargs
            yield client

        fake_client_cls = MagicMock()
        fake_client_cls.connect = _fake_connect
        monkeypatch.setattr(runner, "Client", fake_client_cls)

        def _make_worker(c, nodes):
            worker = MagicMock(spec=Worker)
            worker.resources = {}
            captured["worker"] = worker
            return worker

        monkeypatch.setattr(runner, "Worker", _make_worker)
        monkeypatch.setattr(runner, "_resolve_tool_nodes", lambda registry: [MagicMock()])
        monkeypatch.setattr(runner, "_run_worker", AsyncMock())

        await runner._amain()

        assert captured["worker"].resources["a2a_client"] is client
        # The reply topic must be the tools-private one (NOT the bridge's
        # discord.outbox) or target-agent ReturnCalls get double-projected.
        assert captured["connect_kwargs"]["reply_topic"] == runner._REPLY_TOPIC


class TestConfigureToolWorkspace:
    """The runner points the vendored hermes terminal backend at the shared
    calfcord workspace by setting ``TERMINAL_CWD``. The hermes local backend
    starts each agent session's shell in ``TERMINAL_CWD`` (falling back to the
    process cwd), so this gives every agent a consistent, writable base dir
    while keeping per-agent session isolation."""

    def test_sets_terminal_cwd_from_workspace_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        ws = tmp_path / "ws"
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(ws))
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        resolved = runner._configure_tool_workspace()
        assert resolved == ws.resolve()
        assert os.environ["TERMINAL_CWD"] == str(ws.resolve())
        assert ws.is_dir()  # created on demand

    def test_expands_user_home_in_workspace_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # ``~`` in CALFCORD_WORKSPACE_DIR must expand, not land a literal
        # "~/..." directory next to the cwd.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", "~/myws")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        resolved = runner._configure_tool_workspace()
        assert resolved == (tmp_path / "myws").resolve()

    def test_respects_operator_set_terminal_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TERMINAL_CWD", str(explicit))
        runner._configure_tool_workspace()
        # An explicit operator value wins — not overwritten by the workspace.
        assert os.environ["TERMINAL_CWD"] == str(explicit)

    def test_defaults_workspace_when_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.delenv("CALFCORD_WORKSPACE_DIR", raising=False)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        resolved = runner._configure_tool_workspace()
        expected = (tmp_path / "state" / "workspace").resolve()
        assert resolved == expected
        assert os.environ["TERMINAL_CWD"] == str(expected)
        assert expected.is_dir()


class TestMainEntryPoint:
    """``main`` is the console-script entry: configure logging, load the env,
    pin the tool workspace, then run the worker. The Kafka/Discord boundary
    (``_amain``) is mocked so these stay unit tests."""

    def test_parse_args_returns_namespace(self) -> None:
        assert runner._parse_args([]) is not None

    def test_main_configures_workspace_before_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(runner, "load_dotenv", lambda: calls.append("dotenv"))
        # _parse_args() with no argv would parse pytest's sys.argv — stub it.
        monkeypatch.setattr(runner, "_parse_args", lambda: None)
        monkeypatch.setattr(
            runner, "_configure_tool_workspace", lambda: calls.append("workspace")
        )

        def _fake_run(coro: object) -> None:
            coro.close()  # avoid "coroutine never awaited"
            calls.append("run")

        monkeypatch.setattr(runner.asyncio, "run", _fake_run)
        runner.main()
        # Workspace must be pinned before the worker runs (TERMINAL_CWD is
        # read per call, but pinning first keeps the ordering contract clear).
        assert calls.index("workspace") < calls.index("run")

    def test_main_swallows_keyboard_interrupt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runner, "load_dotenv", lambda: None)
        monkeypatch.setattr(runner, "_parse_args", lambda: None)
        monkeypatch.setattr(runner, "_configure_tool_workspace", lambda: None)

        def _raise(coro: object) -> None:
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(runner.asyncio, "run", _raise)
        runner.main()  # Ctrl-C is a clean shutdown, not a crash.


class TestRunWorkerShutdownContract:
    """The tools runner delegates its shutdown contract to the shared
    :func:`run_worker_until_signal`, which drives the worker via the embedded
    ``Worker.start()``/``stop()`` surface (not ``run()``) so it keeps SIGINT/
    SIGTERM ownership. The full contract — a real commanded SIGTERM draining
    cleanly, and a signal-less exit being surfaced so a supervisor restarts — is
    exercised in ``tests/test_worker_runtime.py``. Here we only pin, per runner,
    that a managed-boot crash propagates out of ``_run_worker`` (so this passes
    only if the runner really wires into the shared helper)."""

    async def test_worker_boot_crash_propagates(self) -> None:
        """A crash during the managed boot (``Worker.start()``) must escape
        ``_run_worker`` so the surrounding ``asyncio.run`` exits non-zero."""
        crash = ValueError("simulated kafka drop")
        worker = MagicMock(spec=Worker)
        worker.start = AsyncMock(side_effect=crash)
        with pytest.raises(ValueError, match="simulated kafka drop"):
            await runner._run_worker(worker)
