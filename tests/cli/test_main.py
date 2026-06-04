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

from calfcord.cli import init
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
