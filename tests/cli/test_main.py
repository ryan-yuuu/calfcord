"""Tests for the ``calfcord-cli`` argparse entry point and path resolution.

These confirm the entry point is importable and wired (``--help`` exits 0,
the ``init`` subcommand is registered) and that :func:`init.resolve_paths`
honours the native (``$CALFCORD_HOME``) vs dev layouts and the
``CALFKIT_AGENTS_DIR`` override the shim/runners already respect.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from calfcord.cli import (
    agent_create,
    agent_edit,
    agent_inspect,
    agent_lifecycle,
    deploy,
    doctor,
    explain,
    init,
    logs,
    router_config,
    tool_aliases,
)
from calfcord.cli import main as main_mod
from calfcord.cli.main import main
from calfcord.supervisor import component, lifecycle, mcp_roster, roster


def test_main_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_main_init_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["init", "--help"])
    assert exc.value.code == 0


def test_main_requires_subcommand() -> None:
    # No subcommand → argparse errors out (exit 2), never a silent success.
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_main_router_setup_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["router", "setup", "--help"])
    assert exc.value.code == 0


def test_main_router_requires_subcommand() -> None:
    # ``router`` is a verb group: a bare ``calfcord router`` must error (exit 2),
    # never silently no-op — the required sub-subparser enforces this.
    with pytest.raises(SystemExit) as exc:
        main(["router"])
    assert exc.value.code != 0


def test_main_router_setup_dispatches_with_resolved_env_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The shim exports CALFCORD_HOME; main must resolve the install's config/.env
    # via init.resolve_paths and hand exactly that path to the ONE wizard. After
    # the DRY reconciliation `router setup` is a deprecated alias of `router edit`,
    # so it dispatches to router_config.edit — there is no second wizard module.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    def _sentinel(prompter: object, *, env_path: Path) -> int:
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(router_config, "edit", _sentinel)

    assert main(["router", "setup"]) == 0
    assert captured["env_path"] == home / "config" / ".env"


def test_resolve_paths_native_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    home = tmp_path / "home"
    env_path, agents_dir = init.resolve_paths(home)
    assert env_path == home / "config" / ".env"
    assert agents_dir == home / "agents"


def test_resolve_paths_dev_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    env_path, agents_dir = init.resolve_paths(None)
    assert env_path == Path(".env")
    assert agents_dir == Path("agents")


def test_resolve_paths_agents_dir_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(tmp_path / "custom-agents"))
    # Override beats both the native-home and dev defaults.
    _, native_agents = init.resolve_paths(tmp_path / "home")
    _, dev_agents = init.resolve_paths(None)
    assert native_agents == Path(os.environ["CALFKIT_AGENTS_DIR"])
    assert dev_agents == Path(os.environ["CALFKIT_AGENTS_DIR"])


# --- _require_home: the shared native-install guard ------------------------


def test_require_home_returns_resolved_home_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # On a native install the guard returns the resolved home and prints nothing,
    # so the caller proceeds to drive the supervisor.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    assert main_mod._require_home("deploy") == home
    assert capsys.readouterr().out == ""


def test_require_home_dev_run_prints_message_and_returns_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A dev run (no CALFCORD_HOME) returns None after printing the actionable
    # native-install steer, so the caller can `return 1` without crashing
    # downstream in os.fspath(None). The default detail is the supervisor home.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    assert main_mod._require_home("agent stop") is None
    out = capsys.readouterr().out
    assert out == (
        "error: `calfcord agent stop` needs a native install — set CALFCORD_HOME "
        "(or run the installer) so the supervisor has a stable home.\n"
    )


def test_require_home_detail_customizes_trailing_clause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `deploy`/`logs`/`start` pass a verb-specific rationale (manifest / logs dir
    # / shim) that the guard substitutes after "so ".
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    assert (
        main_mod._require_home("deploy", detail="the manifest can reference a stable home and shim.")
        is None
    )
    out = capsys.readouterr().out
    assert out == (
        "error: `calfcord deploy` needs a native install — set CALFCORD_HOME "
        "(or run the installer) so the manifest can reference a stable home and shim.\n"
    )


# --- agent verb group: help + dispatch -------------------------------------


@pytest.mark.parametrize(
    "verb",
    ["create", "list", "show", "edit", "set", "rename", "delete", "tools"],
)
def test_main_agent_subcommand_help_exits_zero(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb, "--help"])
    assert exc.value.code == 0


def test_main_agent_requires_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["agent"])
    assert exc.value.code != 0


def _use_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the resolver at temp agents/state dirs via the env overrides."""
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("CALFKIT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CALFCORD_HOME", raising=False)


def test_main_agent_list_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_list(agents_dir: Path, *, as_json: bool) -> int:
        captured.update(agents_dir=agents_dir, as_json=as_json)
        return 0

    monkeypatch.setattr(agent_inspect, "run_list", _run_list)
    assert main(["agent", "list", "--json"]) == 0
    assert captured == {"agents_dir": tmp_path / "agents", "as_json": True}


def test_main_agent_set_collects_flags_and_provider_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_set(agents_dir: Path, name: str, updates: dict[str, str]) -> int:
        captured.update(name=name, updates=updates)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_set", _run_set)
    rc = main([
        "agent", "set", "scribe",
        "--description", "Has: colon",
        "--thinking-effort", "high",
        "--tools", "read_file,shell",
        "--provider", "openai",
        "--model", "gpt-5-nano",
    ])
    assert rc == 0
    assert captured["name"] == "scribe"
    assert captured["updates"] == {
        "description": "Has: colon",
        "thinking_effort": "high",
        "tools": "read_file,shell",
        "provider": "openai",
        "model": "gpt-5-nano",
    }


def test_main_agent_set_expands_prompt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("You are Scribe.\nBe terse.\n")
    captured: dict[str, object] = {}

    def _run_set(agents_dir: Path, name: str, updates: dict[str, str]) -> int:
        captured.update(updates=updates)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_set", _run_set)
    assert main(["agent", "set", "scribe", f"--system-prompt=@{prompt_file}"]) == 0
    assert captured["updates"] == {"system_prompt": "You are Scribe.\nBe terse.\n"}


def test_main_agent_set_missing_prompt_file_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_dirs(monkeypatch, tmp_path)
    assert main(["agent", "set", "scribe", "--system-prompt=@/no/such/file.md"]) == 1
    assert "error:" in capsys.readouterr().out


def test_main_agent_rename_passes_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_rename(agents_dir: Path, state_dir: Path, old: str, new: str) -> int:
        captured.update(agents_dir=agents_dir, state_dir=state_dir, old=old, new=new)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_rename", _run_rename)
    assert main(["agent", "rename", "scribe", "penny"]) == 0
    assert captured == {
        "agents_dir": tmp_path / "agents",
        "state_dir": tmp_path / "state",
        "old": "scribe",
        "new": "penny",
    }


def test_main_agent_delete_passes_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _run_delete(
        prompter: object, agents_dir: Path, state_dir: Path, name: str, *, yes: bool, keep_state: bool
    ) -> int:
        captured.update(name=name, yes=yes, keep_state=keep_state)
        return 0

    monkeypatch.setattr(agent_lifecycle, "run_delete", _run_delete)
    assert main(["agent", "delete", "scribe", "--yes", "--keep-state"]) == 0
    assert captured == {"name": "scribe", "yes": True, "keep_state": True}


# --- main(): interrupt + raw-mode trapping ---------------------------------


def test_main_traps_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ^C during the interactive dispatch exits 130 with ``aborted.``, not a traceback."""

    def _interrupt(parser: object, args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(main_mod, "_dispatch", _interrupt)
    assert main(["init"]) == 130
    assert "aborted." in capsys.readouterr().out


def test_main_maps_oserror_to_clean_exit_when_stdin_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """InquirerPy's raw-mode ``OSError`` (EINVAL) on a non-TTY stdin → exit 1 + a hint."""

    def _raise(parser: object, args: object) -> int:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(main_mod, "_dispatch", _raise)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)

    assert main(["init"]) == 1
    assert "interactive terminal" in capsys.readouterr().out


def test_main_reraises_oserror_on_a_real_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` with stdin a real TTY is a genuine bug — it must propagate,
    not be masked behind the friendly non-TTY message."""

    def _raise(parser: object, args: object) -> int:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(main_mod, "_dispatch", _raise)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: True)

    with pytest.raises(OSError):
        main(["init"])


# --- doctor: help + dispatch -----------------------------------------------


def test_main_doctor_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--help"])
    assert exc.value.code == 0


def test_main_doctor_dispatches_with_resolved_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The shim exports CALFCORD_HOME; doctor must run against the install's config/.env + agents/,
    # and (phase 4) the install home must be threaded through so the runtime section activates.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(*, env_path: Path, agents_dir: Path, offline: bool = False, home: object = None, **kwargs: object) -> int:
        captured.update(env_path=env_path, agents_dir=agents_dir, offline=offline, home=home)
        return 0

    monkeypatch.setattr(doctor, "run", _run)
    assert main(["doctor", "--offline"]) == 0
    assert captured["env_path"] == home / "config" / ".env"
    assert captured["agents_dir"] == home / "agents"
    assert captured["offline"] is True
    # The resolved install home is passed so doctor's runtime daemon-health section runs.
    assert captured["home"] == home


def test_main_doctor_passes_none_home_in_dev_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A dev run (no CALFCORD_HOME) has no install heartbeats; doctor must receive
    # home=None so the runtime section is correctly skipped (not a half-built probe).
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    monkeypatch.setenv("CALFKIT_AGENTS_DIR", str(tmp_path / "agents"))
    captured: dict[str, object] = {}

    def _run(*, env_path: Path, agents_dir: Path, offline: bool = False, home: object = None, **kwargs: object) -> int:
        captured.update(home=home)
        return 0

    monkeypatch.setattr(doctor, "run", _run)
    assert main(["doctor"]) == 0
    assert captured["home"] is None


def test_main_doctor_fix_flag_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # `--fix` is DEFERRED (no auto-repair plumbing in doctor.run), so the flag was
    # removed rather than advertised in --help while doing nothing. An unrecognized
    # flag must make argparse error (exit 2), never silently accept-and-no-op.
    def _boom(**kwargs: object) -> int:
        raise AssertionError("doctor.run must not run for an unrecognized flag")

    monkeypatch.setattr(doctor, "run", _boom)
    with pytest.raises(SystemExit) as exc:
        main(["doctor", "--fix"])
    assert exc.value.code == 2


# --- _healthcheck: hidden readiness-probe subcommand -----------------------


def test_main_healthcheck_broker_exits_with_probe_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The hidden _healthcheck broker probe returns 0 when the injected production
    # probe reports the broker reachable. main resolves home + builds the probe
    # from CALF_HOST_URL; we patch the probe builder so no live Kafka is needed.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CALF_HOST_URL", "localhost:9092")
    captured: dict[str, object] = {}

    async def _reachable() -> bool:
        return True

    def _builder(server_urls: str) -> object:
        captured["server_urls"] = server_urls
        return _reachable

    monkeypatch.setattr(main_mod, "default_broker_probe", _builder)
    assert main(["_healthcheck", "broker"]) == 0
    # The probe is built from CALF_HOST_URL, exactly as the runners read it.
    assert captured["server_urls"] == "localhost:9092"


def test_main_healthcheck_broker_unreachable_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CALF_HOST_URL", "localhost:9092")

    async def _unreachable() -> bool:
        return False

    monkeypatch.setattr(main_mod, "default_broker_probe", lambda server_urls: _unreachable)
    assert main(["_healthcheck", "broker"]) == 1


def test_main_healthcheck_defaults_host_url_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # CALF_HOST_URL unset → "localhost" (same default the runners use), so the
    # probe is still buildable on a bare dev box.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    captured: dict[str, object] = {}

    async def _reachable() -> bool:
        return True

    def _builder(server_urls: str) -> object:
        captured["server_urls"] = server_urls
        return _reachable

    monkeypatch.setattr(main_mod, "default_broker_probe", _builder)
    assert main(["_healthcheck", "broker"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_healthcheck_bridge_reads_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A non-broker component is judged by heartbeat freshness under the resolved
    # home's state/health/ — no broker probe is consulted at all. A fresh beat → 0.
    from datetime import UTC, datetime

    from calfcord.health.heartbeat import write_beat

    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    write_beat(home, "bridge", status="healthy", now=datetime.now(UTC))

    # Building a broker probe for a non-broker check would be a bug; make the
    # builder explode so the test fails loudly if the broker path is taken.
    def _explode(server_urls: str) -> object:
        raise AssertionError("broker probe must not be built for a heartbeat check")

    monkeypatch.setattr(main_mod, "default_broker_probe", _explode)
    assert main(["_healthcheck", "bridge"]) == 0


def test_main_healthcheck_bridge_missing_beat_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    assert main(["_healthcheck", "bridge"]) == 1


def test_main_agent_create_and_edit_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_dirs(monkeypatch, tmp_path)
    seen: list[tuple[str, str | None]] = []

    def _create(prompter: object, *, agents_dir: Path, env_path: Path, name: str | None) -> int:
        seen.append(("create", name))
        return 0

    def _edit(prompter: object, *, agents_dir: Path, env_path: Path, name: str | None) -> int:
        seen.append(("edit", name))
        return 0

    monkeypatch.setattr(agent_create, "run", _create)
    monkeypatch.setattr(agent_edit, "run", _edit)
    assert main(["agent", "create", "scribe"]) == 0
    assert main(["agent", "edit"]) == 0
    assert seen == [("create", "scribe"), ("edit", None)]


# --- substrate lifecycle: start / stop / status ----------------------------


@pytest.mark.parametrize("verb", ["start", "stop", "status"])
def test_main_lifecycle_help_exits_zero(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([verb, "--help"])
    assert exc.value.code == 0


def test_main_start_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``start`` must resolve home from CALFCORD_HOME, build the launcher as the
    # install's shim, read server_urls from CALF_HOST_URL, and enumerate the
    # agents dir for the roster — then asyncio.run lifecycle.start and propagate
    # its exit code.
    home = tmp_path / "home"
    agents = home / "agents"
    agents.mkdir(parents=True)
    (agents / "assistant.md").write_text(
        "---\nname: assistant\nmodel: gpt-5-nano\n---\nYou are assistant.\n"
    )
    (agents / "scribe.md").write_text(
        "---\nname: scribe\nmodel: gpt-5-nano\n---\nYou are scribe.\n"
    )
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, server_urls, launcher, agent_ids, **kwargs):
        captured.update(
            home=home_arg,
            server_urls=server_urls,
            launcher=launcher,
            agent_ids=list(agent_ids),
        )
        return 0

    monkeypatch.setattr(lifecycle, "start", _start)
    assert main(["start"]) == 0
    assert captured["home"] == home
    assert captured["server_urls"] == "broker.example:9092"
    assert captured["launcher"] == str(home / "shims" / "calfcord")
    # Roster is the sorted .md stems (the same seam `agent list` uses).
    assert captured["agent_ids"] == ["assistant", "scribe"]


def test_main_start_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(lifecycle, "start", _start)
    assert main(["start"]) == 1


def test_main_start_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Dev runs (no CALFCORD_HOME) have no shim to launch under, so ``start`` must
    # fail with a clear native-install message rather than asyncio.run a half-built
    # invocation.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("lifecycle.start must not run without a home")

    monkeypatch.setattr(lifecycle, "start", _boom)
    assert main(["start"]) == 1
    out = capsys.readouterr().out
    assert "native install" in out


def test_main_start_defaults_host_url_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, server_urls, **kwargs):
        captured["server_urls"] = server_urls
        return 0

    monkeypatch.setattr(lifecycle, "start", _start)
    assert main(["start"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_stop_dispatches_with_resolved_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _stop(home_arg, **kwargs):
        captured["home"] = home_arg
        return 0

    monkeypatch.setattr(lifecycle, "stop", _stop)
    assert main(["stop"]) == 0
    assert captured["home"] == home


def test_main_stop_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(lifecycle, "stop", _stop)
    assert main(["stop"]) == 3


def test_main_status_dispatches_with_resolved_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _status(home_arg, **kwargs):
        captured["home"] = home_arg
        return 0

    monkeypatch.setattr(lifecycle, "status", _status)
    assert main(["status"]) == 0
    assert captured["home"] == home


def test_main_stop_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("lifecycle.stop must not run without a home")

    monkeypatch.setattr(lifecycle, "stop", _boom)
    assert main(["stop"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_status_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("lifecycle.status must not run without a home")

    monkeypatch.setattr(lifecycle, "status", _boom)
    assert main(["status"]) == 1
    assert "native install" in capsys.readouterr().out


# --- roster lifecycle: agent start / stop / restart / ps -------------------


@pytest.mark.parametrize("verb", ["start", "stop", "restart", "ps"])
def test_main_agent_roster_help_exits_zero(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb, "--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_roster_requires_name_or_all(verb: str) -> None:
    # Exactly one of <name> | --all is required: a bare `agent start` (neither)
    # must error (exit 2), never silently act on nothing. The name is now
    # optional (nargs="?") so the mutual-exclusion is enforced in the dispatcher
    # via parser.error (which exits 2), not by argparse's required-positional.
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb])
    assert exc.value.code == 2


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_roster_name_and_all_are_mutually_exclusive(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Passing BOTH a name and --all is contradictory (one targets a single agent,
    # the other every agent on this host) — parser.error (exit 2), and neither the
    # singular nor the bulk roster fn runs.
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))

    def _boom(*args, **kwargs):
        raise AssertionError("no roster fn should run when name and --all collide")

    monkeypatch.setattr(roster, f"agent_{verb}", _boom)
    monkeypatch.setattr(roster, f"agent_{verb}_all", _boom)
    with pytest.raises(SystemExit) as exc:
        main(["agent", verb, "assistant", "--all"])
    assert exc.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_agent_start_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent start <name>` resolves home from CALFCORD_HOME, reads server_urls
    # from CALF_HOST_URL, asyncio.runs roster.agent_start with the resolved
    # home + name + server_urls, and propagates its exit code.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")

    captured: dict[str, object] = {}

    async def _start(home_arg, *, name, server_urls, **kwargs):
        captured.update(home=home_arg, name=name, server_urls=server_urls)
        return 0

    monkeypatch.setattr(roster, "agent_start", _start)
    assert main(["agent", "start", "assistant"]) == 0
    assert captured == {
        "home": home,
        "name": "assistant",
        "server_urls": "broker.example:9092",
    }


def test_main_agent_start_propagates_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(roster, "agent_start", _start)
    assert main(["agent", "start", "assistant"]) == 1


def test_main_agent_start_defaults_host_url_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # CALF_HOST_URL unset → "localhost" (the same default the runners + lifecycle
    # use), so the broker-wide duplicate probe is still buildable on a dev box.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, name, server_urls, **kwargs):
        captured["server_urls"] = server_urls
        return 0

    monkeypatch.setattr(roster, "agent_start", _start)
    assert main(["agent", "start", "assistant"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_agent_start_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Roster ops drive the install-scoped supervisor (derived port under
    # $CALFCORD_HOME), so a dev run with no home refuses with a clear message
    # rather than asyncio.run a half-built invocation against the dev tree.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_start must not run without a home")

    monkeypatch.setattr(roster, "agent_start", _boom)
    assert main(["agent", "start", "assistant"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_stop_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent stop <name>` needs no broker probe — just the resolved home + name.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _stop(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(roster, "agent_stop", _stop)
    assert main(["agent", "stop", "assistant"]) == 0
    assert captured == {"home": home, "name": "assistant"}


def test_main_agent_stop_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(roster, "agent_stop", _stop)
    assert main(["agent", "stop", "assistant"]) == 3


def test_main_agent_stop_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_stop must not run without a home")

    monkeypatch.setattr(roster, "agent_stop", _boom)
    assert main(["agent", "stop", "assistant"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_restart_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _restart(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(roster, "agent_restart", _restart)
    assert main(["agent", "restart", "assistant"]) == 0
    assert captured == {"home": home, "name": "assistant"}


def test_main_agent_restart_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _restart(*args, **kwargs):
        return 2

    monkeypatch.setattr(roster, "agent_restart", _restart)
    assert main(["agent", "restart", "assistant"]) == 2


def test_main_agent_restart_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_restart must not run without a home")

    monkeypatch.setattr(roster, "agent_restart", _boom)
    assert main(["agent", "restart", "assistant"]) == 1
    assert "native install" in capsys.readouterr().out


# --- roster lifecycle: agent start/stop/restart --all (behavior #1) ---------
#
# `--all` is the uniform-surface bulk verb (decision B), LOCAL-only (this host's
# supervisor). `start --all` targets every DEFINED agent (so main must pass the
# detected .md ids); `stop --all` / `restart --all` target every RUNNING local
# agent (the bulk fn reads the supervisor itself, so main passes no ids).


def test_main_agent_start_all_dispatches_with_defined_agent_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent start --all` resolves home + server_urls and passes the DEFINED agent
    # ids (the same detect_agents seam `start`/`agent list` use) to agent_start_all.
    home = tmp_path / "home"
    agents = home / "agents"
    agents.mkdir(parents=True)
    (agents / "assistant.md").write_text("---\nname: assistant\nmodel: gpt-5-nano\n---\nYou are assistant.\n")
    (agents / "scribe.md").write_text("---\nname: scribe\nmodel: gpt-5-nano\n---\nYou are scribe.\n")
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    async def _start_all(home_arg, *, agent_ids, server_urls, **kwargs):
        captured.update(home=home_arg, agent_ids=list(agent_ids), server_urls=server_urls)
        return 0

    def _single_boom(*args, **kwargs):
        raise AssertionError("--all must dispatch to agent_start_all, not the singular")

    monkeypatch.setattr(roster, "agent_start_all", _start_all)
    monkeypatch.setattr(roster, "agent_start", _single_boom)
    assert main(["agent", "start", "--all"]) == 0
    assert captured["home"] == home
    assert captured["agent_ids"] == ["assistant", "scribe"]
    assert captured["server_urls"] == "broker.example:9092"


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_main_agent_stop_restart_all_dispatch_with_home_only(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent stop --all` / `restart --all` target every RUNNING local agent — the
    # bulk fn reads the supervisor itself, so main passes only the resolved home
    # (no ids, no server_urls / broker probe).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    captured: dict[str, object] = {}

    async def _all(home_arg, **kwargs):
        captured.update(home=home_arg, kwargs=kwargs)
        return 0

    def _single_boom(*args, **kwargs):
        raise AssertionError(f"--all must dispatch to agent_{verb}_all, not the singular")

    monkeypatch.setattr(roster, f"agent_{verb}_all", _all)
    monkeypatch.setattr(roster, f"agent_{verb}", _single_boom)
    assert main(["agent", verb, "--all"]) == 0
    assert captured["home"] == home
    # No name, no server_urls leak into the bulk-stop/restart call.
    assert "name" not in captured["kwargs"]
    assert "server_urls" not in captured["kwargs"]


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_all_propagates_exit_code(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _all(*args, **kwargs):
        return 1

    monkeypatch.setattr(roster, f"agent_{verb}_all", _all)
    assert main(["agent", verb, "--all"]) == 1


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_main_agent_all_without_home_errors_native_install(
    verb: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--all` drives the install-scoped supervisor too, so a dev run with no home
    # refuses with the same native-install steer rather than running a bulk sweep.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError(f"agent_{verb}_all must not run without a home")

    monkeypatch.setattr(roster, f"agent_{verb}_all", _boom)
    assert main(["agent", verb, "--all"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_ps_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent ps` takes no name; it resolves home + server_urls and asyncio.runs
    # roster.agent_ps (the running view), distinct from `agent list` (defined).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")

    captured: dict[str, object] = {}

    async def _ps(home_arg, *, server_urls, **kwargs):
        captured.update(home=home_arg, server_urls=server_urls)
        return 0

    monkeypatch.setattr(roster, "agent_ps", _ps)
    assert main(["agent", "ps"]) == 0
    assert captured == {"home": home, "server_urls": "broker.example:9092"}


def test_main_agent_ps_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))

    async def _ps(*args, **kwargs):
        return 4

    monkeypatch.setattr(roster, "agent_ps", _ps)
    assert main(["agent", "ps"]) == 4


def test_main_agent_ps_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("roster.agent_ps must not run without a home")

    monkeypatch.setattr(roster, "agent_ps", _boom)
    assert main(["agent", "ps"]) == 1
    assert "native install" in capsys.readouterr().out


def test_main_agent_list_and_ps_are_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `agent list` (defined agents) must NOT route to the roster `agent ps`
    # (running agents). They share the `agent` verb group but are different ops.
    _use_dirs(monkeypatch, tmp_path)

    def _run_list(agents_dir: Path, *, as_json: bool) -> int:
        return 0

    def _ps_boom(*args, **kwargs):
        raise AssertionError("`agent list` must not dispatch to roster.agent_ps")

    monkeypatch.setattr(agent_inspect, "run_list", _run_list)
    monkeypatch.setattr(roster, "agent_ps", _ps_boom)
    assert main(["agent", "list"]) == 0


# --- router config + lifecycle: show / set / edit / start / stop ------------


@pytest.mark.parametrize("verb", ["show", "set", "edit", "start", "stop"])
def test_main_router_subcommand_help_exits_zero(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["router", verb, "--help"])
    assert exc.value.code == 0


def test_main_router_show_dispatches_with_resolved_env_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `router show` resolves the install's config/.env via init.resolve_paths and
    # hands exactly that path to router_config.show, propagating its exit code.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _show(*, env_path: Path) -> int:
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(router_config, "show", _show)
    assert main(["router", "show"]) == 0
    assert captured["env_path"] == home / "config" / ".env"


def test_main_router_set_dispatches_provider_and_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `router set --provider P --model M` resolves env_path and forwards both flags
    # verbatim (validation lives in router_config.set_config, not the CLI wiring).
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _set(*, env_path: Path, provider: str | None, model: str | None) -> int:
        captured.update(env_path=env_path, provider=provider, model=model)
        return 0

    monkeypatch.setattr(router_config, "set_config", _set)
    assert main(["router", "set", "--provider", "openai", "--model", "gpt-5-nano"]) == 0
    assert captured == {
        "env_path": home / "config" / ".env",
        "provider": "openai",
        "model": "gpt-5-nano",
    }


def test_main_router_set_defaults_unset_flags_to_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Omitted --provider / --model arrive as None so a partial set is honoured
    # downstream (set_config writes only the flag(s) given).
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _set(*, env_path: Path, provider: str | None, model: str | None) -> int:
        captured.update(provider=provider, model=model)
        return 0

    monkeypatch.setattr(router_config, "set_config", _set)
    assert main(["router", "set", "--model", "gpt-5-nano"]) == 0
    assert captured == {"provider": None, "model": "gpt-5-nano"}


def test_main_router_set_propagates_validation_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    def _set(*, env_path: Path, provider: str | None, model: str | None) -> int:
        return 1

    monkeypatch.setattr(router_config, "set_config", _set)
    assert main(["router", "set", "--provider", "nope"]) == 1


def test_main_router_edit_dispatches_with_prompter_and_env_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `router edit` is the interactive wizard: it gets a prompter + resolved env_path.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _edit(prompter: object, *, env_path: Path) -> int:
        captured.update(env_path=env_path, has_prompter=prompter is not None)
        return 0

    monkeypatch.setattr(router_config, "edit", _edit)
    assert main(["router", "edit"]) == 0
    assert captured["env_path"] == home / "config" / ".env"
    assert captured["has_prompter"] is True


def test_main_router_start_dispatches_with_home_and_env_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `router start` is async and needs BOTH the install home (for pc_port_for, the
    # supervisor REST port — same home agent start/stop pass) AND env_path (for the
    # fail-fast unconfigured check). It must NOT consult CALF_HOST_URL (component
    # lifecycle does not probe the broker). Exit code is propagated unchanged.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    async def _start(home_arg, *, env_path, **kwargs):
        captured.update(home=home_arg, env_path=env_path)
        return 0

    monkeypatch.setattr(router_config, "router_start", _start)
    assert main(["router", "start"]) == 0
    # The home is the $CALFCORD_HOME dir itself (what pc_port_for keys on), exactly
    # as the agent roster / substrate lifecycle pass it — NOT env_path's parent.
    assert captured["home"] == home
    assert captured["env_path"] == home / "config" / ".env"


def test_main_router_start_propagates_unconfigured_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(router_config, "router_start", _start)
    assert main(["router", "start"]) == 1


def test_main_router_stop_dispatches_with_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `router stop` is async and needs only the install home (for the supervisor
    # REST port); no env_path config check, no broker probe.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    async def _stop(home_arg, **kwargs):
        captured["home"] = home_arg
        return 0

    monkeypatch.setattr(router_config, "router_stop", _stop)
    assert main(["router", "stop"]) == 0
    assert captured["home"] == home


def test_main_router_stop_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(router_config, "router_stop", _stop)
    assert main(["router", "stop"]) == 3


def test_main_router_start_and_stop_pass_the_same_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # pc_port_for keys on the home dir, so start and stop MUST pass the identical
    # home value (the $CALFCORD_HOME root) or they'd talk to different REST ports.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    seen: dict[str, object] = {}

    async def _start(home_arg, *, env_path, **kwargs):
        seen["start_home"] = home_arg
        return 0

    async def _stop(home_arg, **kwargs):
        seen["stop_home"] = home_arg
        return 0

    monkeypatch.setattr(router_config, "router_start", _start)
    monkeypatch.setattr(router_config, "router_stop", _stop)
    assert main(["router", "start"]) == 0
    assert main(["router", "stop"]) == 0
    assert seen["start_home"] == seen["stop_home"] == home


@pytest.mark.parametrize("verb", ["start", "stop"])
def test_main_router_lifecycle_without_home_errors_native_install(
    verb: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Router lifecycle drives the install-scoped supervisor (port derived from
    # $CALFCORD_HOME), so a dev run with no home must refuse with a clear message
    # rather than crash inside os.fspath(None) — mirroring agent/substrate lifecycle.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("router lifecycle must not run without a home")

    monkeypatch.setattr(router_config, "router_start", _boom)
    monkeypatch.setattr(router_config, "router_stop", _boom)
    assert main(["router", verb]) == 1
    assert "native install" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["show", "edit"])
def test_main_router_config_works_without_home_in_dev_mode(
    verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Config verbs use env_path (dev: ./.env), not the supervisor home, so they
    # must still work in dev mode — the native-install guard gates lifecycle only.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _show(*, env_path: Path) -> int:
        captured["env_path"] = env_path
        return 0

    def _edit(prompter: object, *, env_path: Path) -> int:
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(router_config, "show", _show)
    monkeypatch.setattr(router_config, "edit", _edit)
    assert main(["router", verb]) == 0
    assert captured["env_path"] == Path(".env")


def test_main_router_setup_still_dispatches_back_compat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `router setup` is kept as a DEPRECATED alias of the editable wizard; after the
    # DRY reconciliation there is ONE wizard (router_config.edit), so `setup` must
    # dispatch to it (not to a removed router_setup.run) and print a deprecation
    # note steering the operator at `router edit`.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _sentinel(prompter: object, *, env_path: Path) -> int:
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(router_config, "edit", _sentinel)
    assert main(["router", "setup"]) == 0
    assert captured["env_path"] == home / "config" / ".env"
    out = capsys.readouterr().out.lower()
    assert "deprecated" in out
    assert "router edit" in out


# --- tools lifecycle: start / stop (singleton-component veneers) -------------
#
# `tools` is a SINGLETON roster component: its start/stop are thin veneers over
# the generic component_start/component_stop, dispatched with the component's
# Process Compose slot name. Unlike the router, it has NO config surface, so the
# CLI wiring is the entire veneer — these tests pin that the resolved install
# home and the slot name reach component_start/stop and that their exit codes
# propagate, mirroring the router lifecycle tests above.


@pytest.mark.parametrize("group", ["tools"])
@pytest.mark.parametrize("verb", ["start", "stop"])
def test_main_component_lifecycle_help_exits_zero(group: str, verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([group, verb, "--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_requires_subcommand(group: str) -> None:
    # `tools` is a verb group: a bare `calfcord tools` must error (exit 2),
    # never a silent no-op (so the group can grow further commands later).
    with pytest.raises(SystemExit) as exc:
        main([group])
    assert exc.value.code == 2


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_start_dispatches_with_home_and_slot_name(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `<group> start` is async and drives the install-scoped supervisor: it passes
    # the $CALFCORD_HOME dir itself (what pc_port_for keys on, identical to agent
    # start/stop and the substrate lifecycle) and the component's slot name. It
    # must NOT consult CALF_HOST_URL (component lifecycle does not probe the
    # broker). Exit code is propagated unchanged.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    captured: dict[str, object] = {}

    async def _start(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, "component_start", _start)
    assert main([group, "start"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_stop_dispatches_with_home_and_slot_name(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `<group> stop` is async and needs only the install home + the slot name; no
    # config check, no broker probe.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    async def _stop(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, "component_stop", _stop)
    assert main([group, "stop"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_start_propagates_exit_code(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _start(*args, **kwargs):
        return 1

    monkeypatch.setattr(component, "component_start", _start)
    assert main([group, "start"]) == 1


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_stop_propagates_exit_code(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _stop(*args, **kwargs):
        return 3

    monkeypatch.setattr(component, "component_stop", _stop)
    assert main([group, "stop"]) == 3


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_start_and_stop_pass_the_same_home(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # pc_port_for keys on the home dir, so start and stop MUST pass the identical
    # home value (the $CALFCORD_HOME root) or they'd talk to different REST ports.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    seen: dict[str, object] = {}

    async def _start(home_arg, *, name, **kwargs):
        seen["start_home"] = home_arg
        return 0

    async def _stop(home_arg, *, name, **kwargs):
        seen["stop_home"] = home_arg
        return 0

    monkeypatch.setattr(component, "component_start", _start)
    monkeypatch.setattr(component, "component_stop", _stop)
    assert main([group, "start"]) == 0
    assert main([group, "stop"]) == 0
    assert seen["start_home"] == seen["stop_home"] == home


@pytest.mark.parametrize("group", ["tools"])
@pytest.mark.parametrize("verb", ["start", "stop"])
def test_main_component_lifecycle_without_home_errors_native_install(
    group: str, verb: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Component lifecycle drives the install-scoped supervisor (port derived from
    # $CALFCORD_HOME), so a dev run with no home must refuse with a clear message
    # rather than crash inside os.fspath(None) — mirroring router/agent/substrate.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError(f"{group} lifecycle must not run without a home")

    monkeypatch.setattr(component, "component_start", _boom)
    monkeypatch.setattr(component, "component_stop", _boom)
    assert main([group, verb]) == 1
    assert "native install" in capsys.readouterr().out


# --- tools / router restart + --all synonym (behavior #1, uniform) ----------
#
# The four roster verbs are uniform across agent (multi-instance) and the
# singletons. For a singleton the new `restart` subcommand dispatches through the
# generic component_restart, and `--all` is a documented SYNONYM that just calls
# the singular component fn (there is one instance on this host to act on).


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_restart_help_exits_zero(group: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([group, "restart", "--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_restart_dispatches_with_home_and_slot_name(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `<group> restart` drives the generic component_restart with the slot name and
    # the $CALFCORD_HOME dir (what pc_port_for keys on), no broker probe.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    captured: dict[str, object] = {}

    async def _restart(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, "component_restart", _restart)
    assert main([group, "restart"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


@pytest.mark.parametrize("group", ["tools"])
def test_main_component_restart_propagates_exit_code(
    group: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    async def _restart(*args, **kwargs):
        return 2

    monkeypatch.setattr(component, "component_restart", _restart)
    assert main([group, "restart"]) == 2


@pytest.mark.parametrize("group", ["tools"])
@pytest.mark.parametrize(
    "verb,fn",
    [("start", "component_start"), ("stop", "component_stop"), ("restart", "component_restart")],
)
def test_main_component_all_is_synonym_for_singular(
    group: str, verb: str, fn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # For a one-process-per-host singleton, `--all` is an honest SYNONYM: it just
    # calls the SAME singular component fn with the slot name (it targets the one
    # instance), so `<group> <verb> --all` is indistinguishable from `<group> <verb>`.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    async def _fn(home_arg, *, name, **kwargs):
        captured.update(home=home_arg, name=name)
        return 0

    monkeypatch.setattr(component, fn, _fn)
    assert main([group, verb, "--all"]) == 0
    assert captured["home"] == home
    assert captured["name"] == group


@pytest.mark.parametrize("verb,fn", [("start", "router_start"), ("stop", "router_stop"), ("restart", "router_restart")])
def test_main_router_restart_and_all_synonym_dispatch(
    verb: str, fn: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `router restart` (new) dispatches through router_config.router_restart; and
    # `router <verb> --all` is the same SYNONYM (one router per host), routing to
    # the SAME singular fn. start/restart pass env_path; stop/restart do not need it
    # but the dispatch must at least pass the home and propagate the exit code.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    async def _fn(home_arg, **kwargs):
        captured.update(home=home_arg)
        return 0

    monkeypatch.setattr(router_config, fn, _fn)
    assert main(["router", verb, "--all"]) == 0
    assert captured["home"] == home


def test_main_router_restart_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["router", "restart", "--help"])
    assert exc.value.code == 0


# --- explain: read-only teaching screens (no native-install guard) ----------
#
# `explain` is a verb group whose only topic today is `topology`. It is a PURE
# teaching screen — no supervisor, no broker, no install home — so it dispatches
# without the native-install guard every supervisor-scoped verb carries, and runs
# identically in dev and on a native install.


def test_main_explain_requires_subcommand() -> None:
    # `explain` is a verb group: a bare `calfcord explain` must error (exit 2),
    # never silently no-op — the required sub-subparser enforces this so the group
    # can grow further topics later.
    with pytest.raises(SystemExit) as exc:
        main(["explain"])
    assert exc.value.code == 2


def test_main_explain_topology_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["explain", "topology", "--help"])
    assert exc.value.code == 0


def test_main_explain_topology_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    # `explain topology` dispatches to explain.run with the topology topic and
    # propagates its exit code. The topic registry (explain.run) is the single
    # source of truth for what can be taught.
    captured: dict[str, object] = {}

    def _run(topic: str) -> int:
        captured["topic"] = topic
        return 0

    monkeypatch.setattr(explain, "run", _run)
    assert main(["explain", "topology"]) == 0
    assert captured["topic"] == "topology"


def test_main_explain_propagates_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explain, "run", lambda topic: 1)
    assert main(["explain", "topology"]) == 1


def test_main_explain_needs_no_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pure teaching screen must run in a dev tree (no CALFCORD_HOME) WITHOUT the
    # native-install guard the supervisor-scoped verbs carry.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    captured: dict[str, object] = {}

    def _run(topic: str) -> int:
        captured["topic"] = topic
        return 0

    monkeypatch.setattr(explain, "run", _run)
    assert main(["explain", "topology"]) == 0
    assert captured["topic"] == "topology"
    assert "native install" not in capsys.readouterr().out


# --- logs: tail unified or per-component supervisor logs ---------------------
#
# `logs [component] [-f]` reads the install's `state/logs/<name>.log` files, so it
# carries the native-install guard (a dev run has no $CALFCORD_HOME state dir).
# main resolves home + agents_dir once and forwards the optional component + the
# follow flag; the file-reading logic lives in the cohesive logs module.


def test_main_logs_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["logs", "--help"])
    assert exc.value.code == 0


def test_main_logs_no_component_dispatches_with_resolved_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A bare `calfcord logs` tails ALL component logs: main resolves the install
    # home from CALFCORD_HOME and the agents dir via init.resolve_paths, then hands
    # logs.tail home + agents_dir with component=None and follow=False.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _tail(home_arg: Path, *, agents_dir: Path, component: str | None, follow: bool) -> int:
        captured.update(home=home_arg, agents_dir=agents_dir, component=component, follow=follow)
        return 0

    monkeypatch.setattr(logs, "tail", _tail)
    assert main(["logs"]) == 0
    assert captured["home"] == home
    assert captured["agents_dir"] == home / "agents"
    assert captured["component"] is None
    assert captured["follow"] is False


def test_main_logs_named_component_is_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _tail(home_arg: Path, *, agents_dir: Path, component: str | None, follow: bool) -> int:
        captured.update(component=component, follow=follow)
        return 0

    monkeypatch.setattr(logs, "tail", _tail)
    assert main(["logs", "broker"]) == 0
    assert captured["component"] == "broker"
    assert captured["follow"] is False


def test_main_logs_follow_flag_is_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Both `-f` and the long `--follow` form set follow=True.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _tail(home_arg: Path, *, agents_dir: Path, component: str | None, follow: bool) -> int:
        captured.update(component=component, follow=follow)
        return 0

    monkeypatch.setattr(logs, "tail", _tail)
    assert main(["logs", "bridge", "-f"]) == 0
    assert captured == {"component": "bridge", "follow": True}
    assert main(["logs", "--follow"]) == 0
    assert captured == {"component": None, "follow": True}


def test_main_logs_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.setattr(logs, "tail", lambda *a, **k: 1)
    assert main(["logs", "nope"]) == 1


def test_main_logs_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `logs` reads $CALFCORD_HOME/state/logs/*, so a dev run with no home must
    # refuse with the same actionable native-install message every supervisor-
    # scoped verb uses — rather than reading a nonexistent dev log dir.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("logs.tail must not run without a home")

    monkeypatch.setattr(logs, "tail", _boom)
    assert main(["logs"]) == 1
    assert "native install" in capsys.readouterr().out


# --- deploy: generate graduation manifests ----------------------------------
#
# `deploy <systemd|k8s|docker> [--output PATH]` renders heavier-tier manifests
# from the install's roster + paths. It emits the install shim path (systemd), so
# it carries the native-install guard. main resolves home, env_path, agents_dir
# and server_urls, then forwards target + out_path to the cohesive deploy module.


def test_main_deploy_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["deploy", "--help"])
    assert exc.value.code == 0


def test_main_deploy_requires_target() -> None:
    # `deploy` needs a positional target: a bare `calfcord deploy` must error
    # (exit 2), never silently act on nothing.
    with pytest.raises(SystemExit) as exc:
        main(["deploy"])
    assert exc.value.code == 2


def test_main_deploy_rejects_unknown_target() -> None:
    # The target is constrained by argparse `choices=`, so a bad target errors at
    # parse time (exit 2) before any handler runs.
    with pytest.raises(SystemExit) as exc:
        main(["deploy", "nope"])
    assert exc.value.code == 2


@pytest.mark.parametrize("target", ["systemd", "k8s", "docker"])
def test_main_deploy_dispatches_with_resolved_args(
    target: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Each target resolves home from CALFCORD_HOME, env_path + agents_dir via
    # init.resolve_paths, server_urls from CALF_HOST_URL, and forwards them (with
    # out_path defaulting to None for stdout) to deploy.run, propagating its code.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.setenv("CALF_HOST_URL", "broker.example:9092")
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(
        target_arg: str,
        *,
        home: Path,
        env_path: Path,
        agents_dir: Path,
        server_urls: str,
        out_path: Path | None = None,
    ) -> int:
        captured.update(
            target=target_arg,
            home=home,
            env_path=env_path,
            agents_dir=agents_dir,
            server_urls=server_urls,
            out_path=out_path,
        )
        return 0

    monkeypatch.setattr(deploy, "run", _run)
    assert main(["deploy", target]) == 0
    assert captured["target"] == target
    assert captured["home"] == home
    assert captured["env_path"] == home / "config" / ".env"
    assert captured["agents_dir"] == home / "agents"
    assert captured["server_urls"] == "broker.example:9092"
    assert captured["out_path"] is None


def test_main_deploy_output_flag_is_forwarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `--output PATH` (and the `-o` short form) writes the manifest to a file
    # instead of stdout: main forwards the resolved Path as out_path.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(target_arg: str, *, out_path: Path | None = None, **kwargs: object) -> int:
        captured["out_path"] = out_path
        return 0

    monkeypatch.setattr(deploy, "run", _run)
    out_file = tmp_path / "calfcord.service"
    assert main(["deploy", "systemd", "--output", str(out_file)]) == 0
    assert captured["out_path"] == out_file
    assert main(["deploy", "systemd", "-o", str(out_file)]) == 0
    assert captured["out_path"] == out_file


def test_main_deploy_defaults_host_url_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # CALF_HOST_URL unset → "localhost" (the same default the runners + start use).
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALF_HOST_URL", raising=False)
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _run(target_arg: str, *, server_urls: str, **kwargs: object) -> int:
        captured["server_urls"] = server_urls
        return 0

    monkeypatch.setattr(deploy, "run", _run)
    assert main(["deploy", "k8s"]) == 0
    assert captured["server_urls"] == "localhost"


def test_main_deploy_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.setattr(deploy, "run", lambda *a, **k: 2)
    assert main(["deploy", "systemd"]) == 2


def test_main_deploy_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # deploy emits the install shim path (`<home>/shims/calfcord`), which has no
    # meaning on a dev run, so a missing CALFCORD_HOME refuses with the actionable
    # native-install message rather than rendering a manifest pointing at nothing.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("deploy.run must not run without a home")

    monkeypatch.setattr(deploy, "run", _boom)
    assert main(["deploy", "systemd"]) == 1
    assert "native install" in capsys.readouterr().out


# --- mcp lifecycle verbs ------------------------------------------------------


def _mcp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    return home


def test_main_mcp_start_dispatches_named_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def _start(home_arg, *, server, **kwargs):
        captured.update(home=home_arg, server=server)
        return 0

    monkeypatch.setattr(mcp_roster, "mcp_start", _start)
    assert main(["mcp", "start", "github"]) == 0
    assert captured == {"home": home, "server": "github"}


def test_main_mcp_start_all_passes_configured_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `mcp start --all` enumerates mcp.json via the no-secrets reader and
    # passes the names — the "re-pick up mcp.json" sweep.
    home = _mcp_home(tmp_path, monkeypatch)
    config = home / "config"
    config.mkdir()
    (config / "mcp.json").write_text(
        '{"mcpServers": {"github": {"command": "x"}, "docs": {"type": "http", "url": "https://d"}}}'
    )
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    async def _start_all(home_arg, *, servers, **kwargs):
        captured.update(home=home_arg, servers=list(servers))
        return 0

    def _single_boom(*args, **kwargs):
        raise AssertionError("--all must dispatch to mcp_start_all, not the singular")

    monkeypatch.setattr(mcp_roster, "mcp_start_all", _start_all)
    monkeypatch.setattr(mcp_roster, "mcp_start", _single_boom)
    assert main(["mcp", "start", "--all"]) == 0
    assert captured == {"home": home, "servers": ["github", "docs"]}


def test_main_mcp_start_all_invalid_config_errors_actionably(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    config = home / "config"
    config.mkdir()
    (config / "mcp.json").write_text("{not json")
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    assert main(["mcp", "start", "--all"]) == 1
    assert "error" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_main_mcp_stop_restart_dispatch_named(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def _op(home_arg, *, server, **kwargs):
        captured.update(home=home_arg, server=server)
        return 0

    monkeypatch.setattr(mcp_roster, f"mcp_{verb}", _op)
    assert main(["mcp", verb, "github"]) == 0
    assert captured == {"home": home, "server": "github"}


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_main_mcp_stop_restart_all_dispatch_home_only(
    verb: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def _op_all(home_arg, **kwargs):
        captured.update(home=home_arg)
        return 0

    monkeypatch.setattr(mcp_roster, f"mcp_{verb}_all", _op_all)
    assert main(["mcp", verb, "--all"]) == 0
    assert captured == {"home": home}


def test_main_mcp_requires_exactly_one_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mcp_home(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        main(["mcp", "start"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["mcp", "start", "github", "--all"])
    assert excinfo.value.code == 2


def test_main_mcp_without_home_errors_native_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    assert main(["mcp", "start", "github"]) == 1
    assert "CALFCORD_HOME" in capsys.readouterr().out


def test_main_start_passes_mcp_servers_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `calfcord start` enumerates mcp.json alongside the agents dir so the
    # generated project declares one disabled slot per server.
    home = tmp_path / "home"
    agents = home / "agents"
    agents.mkdir(parents=True)
    (agents / "assistant.md").write_text(
        "---\nname: assistant\nmodel: gpt-5-nano\n---\nYou are assistant.\n"
    )
    config = home / "config"
    config.mkdir()
    (config / "mcp.json").write_text('{"mcpServers": {"github": {"command": "x"}}}')
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)

    captured: dict[str, object] = {}

    async def _start(home_arg, *, server_urls, launcher, agent_ids, mcp_servers=(), **kwargs):
        captured.update(agent_ids=list(agent_ids), mcp_servers=list(mcp_servers))
        return 0

    monkeypatch.setattr(lifecycle, "start", _start)
    assert main(["start"]) == 0
    assert captured["agent_ids"] == ["assistant"]
    assert captured["mcp_servers"] == ["github"]


def test_main_mcp_add_dispatches_with_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    def _add(prompter, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(main_mod.mcp_admin, "run_add", _add)
    assert (
        main(
            [
                "mcp", "add", "github",
                "--command", "npx -y srv",
                "--env", "GITHUB_TOKEN",
                "--force", "--start",
            ]
        )
        == 0
    )
    assert captured["server"] == "github"
    assert captured["command"] == "npx -y srv"
    assert captured["env"] == ["GITHUB_TOKEN"]
    assert captured["force"] is True
    assert captured["start"] is True
    assert captured["home"] == home
    assert captured["config_path"] == home / "config" / "mcp.json"


def test_main_mcp_add_works_without_home_dev_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """add/list/remove are config edits — they must work on dev runs (no
    CALFCORD_HOME), targeting ./mcp.json; only the lifecycle verbs need the
    supervisor home."""
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    def _add(prompter, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(main_mod.mcp_admin, "run_add", _add)
    assert main(["mcp", "add", "github", "--command", "srv"]) == 0
    assert captured["home"] is None
    assert captured["config_path"] == Path("mcp.json")


def test_main_mcp_list_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _mcp_home(tmp_path, monkeypatch)
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    captured: dict[str, object] = {}

    def _list(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(main_mod.mcp_admin, "run_list", _list)
    assert main(["mcp", "list"]) == 0
    assert captured["home"] == home


def test_main_mcp_remove_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mcp_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def _remove(prompter, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(main_mod.mcp_admin, "run_remove", _remove)
    assert main(["mcp", "remove", "github", "--force"]) == 0
    assert captured["server"] == "github"
    assert captured["force"] is True


# --- tools alias subcommand --------------------------------------------------


def _fail_if_called(*args: object, **kwargs: object) -> int:
    raise AssertionError("should not have been called")


def _patch_workspace(monkeypatch: pytest.MonkeyPatch, *, up: bool) -> None:
    """Stub the supervisor workspace probe ``_apply_alias_restart`` uses."""
    from calfcord.supervisor import _workspace

    monkeypatch.setattr(_workspace, "resolve_client", lambda client, home: object())

    async def _is_up(client: object) -> bool:
        return up

    monkeypatch.setattr(_workspace, "workspace_is_up", _is_up)


def test_main_tools_alias_add_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)
    captured: dict[str, object] = {}

    def _add(*, env_path, src, dst, tool_names, aliasable_names, apply_restart):
        captured.update(
            env_path=env_path, src=src, dst=dst,
            tool_names=set(tool_names), aliasable_names=set(aliasable_names),
            apply_restart=apply_restart,
        )
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_add", _add)
    assert main(["tools", "alias", "add", "terminal", "terminal_eu"]) == 0
    expected_env, _ = init.resolve_paths(home)
    assert captured["env_path"] == expected_env
    assert captured["src"] == "terminal"
    assert captured["dst"] == "terminal_eu"
    assert captured["apply_restart"] is None  # no --restart → hint, not actuation
    # The canonical surface is computed from ALL_TOOLS: terminal is aliasable,
    # todo (per-session state) is a real tool but NOT aliasable.
    assert "terminal" in captured["tool_names"]
    assert "terminal" in captured["aliasable_names"]
    assert "todo" in captured["tool_names"]
    assert "todo" not in captured["aliasable_names"]


def test_main_tools_alias_list_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    captured: dict[str, object] = {}

    def _list(*, env_path):
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_list", _list)
    assert main(["tools", "alias", "list"]) == 0
    expected_env, _ = init.resolve_paths(home)
    assert captured["env_path"] == expected_env


def test_main_tools_alias_remove_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    captured: dict[str, object] = {}

    def _remove(*, env_path, dst, apply_restart):
        captured.update(env_path=env_path, dst=dst, apply_restart=apply_restart)
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_remove", _remove)
    assert main(["tools", "alias", "remove", "terminal_eu"]) == 0
    assert captured["dst"] == "terminal_eu"
    assert captured["apply_restart"] is None


def test_main_tools_alias_add_restart_injects_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    captured: dict[str, object] = {}

    def _add(*, env_path, src, dst, tool_names, aliasable_names, apply_restart):
        captured["apply_restart"] = apply_restart
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_add", _add)
    assert main(["tools", "alias", "add", "terminal", "terminal_eu", "--restart"]) == 0
    # --restart injects the actuation callback (the workspace-gated restart).
    assert callable(captured["apply_restart"])
    assert captured["apply_restart"] is main_mod._apply_alias_restart


def test_main_tools_alias_remove_restart_injects_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
    captured: dict[str, object] = {}

    def _remove(*, env_path, dst, apply_restart):
        captured["apply_restart"] = apply_restart
        return 0

    monkeypatch.setattr(tool_aliases, "run_alias_remove", _remove)
    assert main(["tools", "alias", "remove", "terminal_eu", "--restart"]) == 0
    assert callable(captured["apply_restart"])


class TestApplyAliasRestart:
    """``_apply_alias_restart`` — the ``--restart`` actuation: gated on a
    running workspace, then restart the tools host + running agents."""

    def test_dev_tree_no_supervisor(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(main_mod, "_resolve_home", lambda: None)
        # _run_component must NOT be called on a dev tree.
        monkeypatch.setattr(main_mod, "_run_component", _fail_if_called)
        main_mod._apply_alias_restart()
        assert "next" in capsys.readouterr().out.lower()

    def test_workspace_down_does_not_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(main_mod, "_resolve_home", lambda: tmp_path)
        _patch_workspace(monkeypatch, up=False)
        monkeypatch.setattr(main_mod, "_run_component", _fail_if_called)
        main_mod._apply_alias_restart()
        assert "workspace not running" in capsys.readouterr().out.lower()

    def test_workspace_up_restarts_tools_and_agents(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(main_mod, "_resolve_home", lambda: tmp_path)
        _patch_workspace(monkeypatch, up=True)
        calls: list[object] = []
        monkeypatch.setattr(
            main_mod, "_run_component",
            lambda comp, verb: calls.append((comp, verb)) or 0,
        )

        async def _restart_all(home):
            calls.append(("agents", home))
            return 0

        monkeypatch.setattr(roster, "agent_restart_all", _restart_all)
        main_mod._apply_alias_restart()
        assert ("tools", "restart") in calls
        assert ("agents", tmp_path) in calls


def test_main_tools_start_still_dispatches_to_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias branch must not break the start/stop/restart verbs."""
    captured: dict[str, object] = {}

    def _run_component(comp, verb):
        captured.update(comp=comp, verb=verb)
        return 0

    monkeypatch.setattr(main_mod, "_run_component", _run_component)
    assert main(["tools", "start"]) == 0
    assert captured == {"comp": "tools", "verb": "start"}
