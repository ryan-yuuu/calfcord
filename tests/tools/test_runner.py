"""Unit tests for the ``calfkit-tools`` runner.

After the calfkit 0.12 migration the tools runner is a plain calfkit
``Worker`` hosting the vendored tool nodes: it connects the process-wide
``Client`` with calfcord's shared provisioning policy and hands the tool
nodes to a managed ``Worker``. It owns no reply inbox and no A2A client
resource — agent-to-agent messaging moved onto calfkit's native handoff
dispatch, so ``private_chat`` (and its worker-scoped ``a2a_client``
resource) no longer exist.

These tests cover the tool-registry guard (``_resolve_tool_nodes``), the
workspace pinning (``_configure_tool_workspace``), the ``main`` entry point,
the ``_amain`` wiring (provisioned connect + a ``Worker`` over the tool
nodes, with NO reply topic and NO A2A resource), and the supervisor-restart
shutdown contract (``_run_worker``). ``_amain``'s Kafka boundary is patched
so it stays a unit test.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.client import Client
from calfkit.worker import Worker

from calfcord._provisioning import PROVISIONING
from calfcord.tools import runner


class TestResolveToolNodes:
    def test_returns_nodes_from_populated_registry(self) -> None:
        node = MagicMock()
        result = runner._resolve_tool_nodes({"terminal": node})
        assert result == [node]

    def test_empty_registry_fails_fast(self) -> None:
        """The empty-registry guard exists specifically to prevent the
        worker from starting in an inert state — subscribed to nothing,
        responding to nothing, but otherwise looking healthy in logs."""
        with pytest.raises(SystemExit, match="empty"):
            runner._resolve_tool_nodes({})

    def test_empty_registry_message_names_include_filter_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty-registry is most often caused by a typo in
        ``CALFCORD_TOOLS_INCLUDE`` (per-host tool narrowing). The SystemExit
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

    def test_empty_registry_message_handles_unset_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


class TestAmainBuildsPlainWorker:
    """After the 0.12 migration the tools runner is a plain calfkit ``Worker``
    over the vendored tool nodes: connect with calfcord's provisioning policy,
    hand the resolved nodes to a ``Worker``, and run it. It claims NO named
    reply inbox and exposes NO ``a2a_client`` worker resource — A2A is native
    handoff dispatch now, so a regression re-adding either would be wrong.
    """

    def test_runner_exposes_no_a2a_or_reply_wiring(self) -> None:
        """The deleted A2A wiring must stay deleted: the runner no longer
        defines a reply-topic literal or an a2a-client resource key. Pinning
        their absence catches a copy-paste that reintroduces the old inbox."""
        assert not hasattr(runner, "_REPLY_TOPIC")
        assert not hasattr(runner, "_A2A_CLIENT_RESOURCE")

    async def test_connects_provisioned_and_builds_worker_over_tool_nodes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALF_HOST_URL", raising=False)
        client = MagicMock(spec=Client)
        nodes = [MagicMock(), MagicMock()]
        captured: dict[str, object] = {}

        @asynccontextmanager
        async def _fake_connect(*args, **kwargs):
            captured["connect_args"] = args
            captured["connect_kwargs"] = kwargs
            yield client

        fake_client_cls = MagicMock()
        fake_client_cls.connect = _fake_connect
        monkeypatch.setattr(runner, "Client", fake_client_cls)

        def _make_worker(c, node_list):
            worker = MagicMock(spec=Worker)
            captured["worker"] = worker
            captured["worker_client"] = c
            captured["worker_nodes"] = node_list
            return worker

        monkeypatch.setattr(runner, "Worker", _make_worker)
        monkeypatch.setattr(runner, "_resolve_tool_nodes", lambda registry: nodes)
        run_mock = AsyncMock()
        monkeypatch.setattr(runner, "_run_worker", run_mock)

        await runner._amain()

        # Connect targets the default broker (CALF_HOST_URL unset) with
        # calfcord's shared provisioning policy — and claims NO named reply inbox
        # (the tools process invokes tools natively; it owns no reply dispatcher).
        assert captured["connect_args"][0] == "localhost"
        assert captured["connect_kwargs"]["provisioning"] is PROVISIONING
        assert "reply_topic" not in captured["connect_kwargs"]

        # A plain Worker over the resolved tool nodes, then run via the shared
        # shutdown helper — no per-runner A2A/resource wiring in between.
        assert captured["worker_client"] is client
        assert captured["worker_nodes"] is nodes
        run_mock.assert_awaited_once_with(captured["worker"])


class TestConfigureToolWorkspace:
    """The runner points the vendored hermes terminal backend at the shared
    calfcord workspace by setting ``TERMINAL_CWD``. The hermes local backend
    starts each agent session's shell in ``TERMINAL_CWD`` (falling back to the
    process cwd), so this gives every agent a consistent, writable base dir
    while keeping per-agent session isolation."""

    def test_sets_terminal_cwd_from_workspace_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        ws = tmp_path / "ws"
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(ws))
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        resolved = runner._configure_tool_workspace()
        assert resolved == ws.resolve()
        assert os.environ["TERMINAL_CWD"] == str(ws.resolve())
        assert ws.is_dir()  # created on demand

    def test_expands_user_home_in_workspace_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        # ``~`` in CALFCORD_WORKSPACE_DIR must expand, not land a literal
        # "~/..." directory next to the cwd.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", "~/myws")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        resolved = runner._configure_tool_workspace()
        assert resolved == (tmp_path / "myws").resolve()

    def test_respects_operator_set_terminal_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        monkeypatch.setenv("CALFCORD_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TERMINAL_CWD", str(explicit))
        runner._configure_tool_workspace()
        # An explicit operator value wins — not overwritten by the workspace.
        assert os.environ["TERMINAL_CWD"] == str(explicit)

    def test_defaults_workspace_when_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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

    def test_main_configures_workspace_before_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(runner, "load_dotenv", lambda: calls.append("dotenv"))
        # _parse_args() with no argv would parse pytest's sys.argv — stub it.
        monkeypatch.setattr(runner, "_parse_args", lambda: None)
        monkeypatch.setattr(runner, "_configure_tool_workspace", lambda: calls.append("workspace"))

        def _fake_run(coro: object) -> None:
            coro.close()  # avoid "coroutine never awaited"
            calls.append("run")

        monkeypatch.setattr(runner.asyncio, "run", _fake_run)
        runner.main()
        # Workspace must be pinned before the worker runs (TERMINAL_CWD is
        # read per call, but pinning first keeps the ordering contract clear).
        assert calls.index("workspace") < calls.index("run")

    def test_main_swallows_keyboard_interrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
