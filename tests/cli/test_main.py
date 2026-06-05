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

from calfcord.cli import agent_create, agent_edit, agent_inspect, agent_lifecycle, init, router_setup
from calfcord.cli import main as main_mod
from calfcord.cli.main import main


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
    # via init.resolve_paths and hand exactly that path to router_setup.run.
    home = tmp_path / "home"
    monkeypatch.setenv("CALFCORD_HOME", str(home))
    monkeypatch.delenv("CALFKIT_AGENTS_DIR", raising=False)

    captured: dict[str, object] = {}

    def _sentinel(prompter: object, *, env_path: Path) -> int:
        captured["env_path"] = env_path
        return 0

    monkeypatch.setattr(router_setup, "run", _sentinel)

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
