"""Behavioural tests for the native installer's seeding + shim env wiring.

``scripts/install.sh`` is the no-prerequisites ``curl | bash`` installer. Two
pieces of its logic are easy to get subtly wrong and impossible to unit-test
from Python directly, so we drive the *actual shell* here:

* ``seed_agents`` — must give the native install a stable agents/state home and
  drop in the starter agent on first install, **without** clobbering an
  operator who removed the starter or added their own agents.
* the generated ``calfcord`` shim's ``_default_env`` block — must default
  ``CALFKIT_AGENTS_DIR`` / ``CALFKIT_STATE_DIR`` under the install home and
  ``CALFCORD_WORKSPACE_DIR`` to the *launch* directory, while letting an
  operator override any of them via the shell env or ``config/.env``.

The installer guards ``main "$@"`` so the file can be *sourced* (rather than
executed, which would hit the network), letting these tests call individual
functions in a throwaway ``CALFCORD_HOME``. The shim env behaviour is observed
end-to-end via a fake ``uv`` that simply prints the three env vars it inherits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"

_UNSET = "__UNSET__"

# A stand-in for ``uv`` that ignores its args and reports the three dir env vars
# the shim is responsible for defaulting. ``${VAR-__UNSET__}`` (no colon)
# distinguishes "shim did not export it" (unset) from "exported as empty".
_FAKE_UV = """#!/usr/bin/env bash
printf 'CALFKIT_AGENTS_DIR=%s\\n' "${CALFKIT_AGENTS_DIR-__UNSET__}"
printf 'CALFKIT_STATE_DIR=%s\\n' "${CALFKIT_STATE_DIR-__UNSET__}"
printf 'CALFCORD_WORKSPACE_DIR=%s\\n' "${CALFCORD_WORKSPACE_DIR-__UNSET__}"
"""


def _source_and_run(
    snippet: str, *, home: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Source ``install.sh`` (main is guarded off) and run ``snippet`` in bash."""
    env = {**os.environ, "CALFCORD_HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    script = f'source "{INSTALL_SH}"\n{snippet}'
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )


def _make_source_dest(tmp: Path, *, with_assistant: bool = True) -> Path:
    """Build a fake unpacked-source dir (the installer's ``$INSTALLED_DEST``)."""
    dest = tmp / "src"
    (dest / "agents").mkdir(parents=True)
    if with_assistant:
        (dest / "agents" / "assistant.md").write_text("---\nname: assistant\n---\nhi\n")
    return dest


def _install_shims(home: Path) -> None:
    result = _source_and_run("write_shims", home=home)
    assert result.returncode == 0, result.stderr
    assert (home / "shims" / "calfcord").exists()


def _run_shim(
    home: Path, *, cwd: Path, env_file: str = "", extra_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Invoke the generated ``calfcord`` shim and capture the env the fake uv saw."""
    (home / "bin").mkdir(parents=True, exist_ok=True)
    uv = home / "bin" / "uv"
    uv.write_text(_FAKE_UV)
    uv.chmod(0o755)
    (home / "current").mkdir(exist_ok=True)
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / ".env").write_text(env_file)

    env = {**os.environ, "CALFCORD_HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [str(home / "shims" / "calfcord"), "calfkit-agent"],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"shim failed: {result.stderr}"
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        parsed[key] = value
    return parsed


# --------------------------------------------------------------- seed_agents ---

def test_seed_agents_seeds_starter_and_state_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = _make_source_dest(tmp_path)
    result = _source_and_run(f'seed_agents "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    assert (home / "agents" / "assistant.md").read_text().startswith("---")
    assert (home / "state").is_dir()


def test_seed_agents_does_not_clobber_existing_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "mine.md").write_text("---\nname: mine\n---\nkeep me\n")
    dest = _make_source_dest(tmp_path)

    result = _source_and_run(f'seed_agents "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    # Operator's agent untouched, and the starter was NOT injected alongside it.
    assert (home / "agents" / "mine.md").read_text() == "---\nname: mine\n---\nkeep me\n"
    assert not (home / "agents" / "assistant.md").exists()


def test_seed_agents_is_noop_when_source_lacks_starter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = _make_source_dest(tmp_path, with_assistant=False)
    result = _source_and_run(f'seed_agents "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    assert (home / "agents").is_dir()
    assert (home / "state").is_dir()
    assert list((home / "agents").iterdir()) == []


# ---------------------------------------------------------- shim _default_env ---

def test_shim_defaults_to_home_dirs_and_launch_cwd(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch)
    assert seen["CALFKIT_AGENTS_DIR"] == str(home / "agents")
    assert seen["CALFKIT_STATE_DIR"] == str(home / "state" / "agents")
    # Workspace follows the directory the command was launched from.
    assert os.path.realpath(seen["CALFCORD_WORKSPACE_DIR"]) == os.path.realpath(str(launch))


def test_shim_defers_to_env_file_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    # When config/.env pins the workspace, the shim must NOT export $PWD over it
    # (it leaves the value for `uv run --env-file` to apply). The fake uv, which
    # does not read --env-file, therefore sees it unset.
    seen = _run_shim(home, cwd=launch, env_file="CALFCORD_WORKSPACE_DIR=/pinned/ws\n")
    assert seen["CALFCORD_WORKSPACE_DIR"] == _UNSET


def test_shim_defers_to_preset_shell_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch, extra_env={"CALFKIT_AGENTS_DIR": "/preset/agents"})
    assert seen["CALFKIT_AGENTS_DIR"] == "/preset/agents"
    # The vars the operator did not preset still get their install-home default.
    assert seen["CALFKIT_STATE_DIR"] == str(home / "state" / "agents")


# --------------------------------------------------------- shim subcommands ---

# A fake ``uv`` that echoes the arguments it was exec'd with, so we can assert
# how the shim translated the user's command line (e.g. ``init`` becoming
# ``calfcord-cli init``). It strips the leading ``run --frozen ... --`` wrapper
# the shim always adds and prints just the trailing user-program argv.
_FAKE_UV_ECHO_ARGS = """#!/usr/bin/env bash
seen=()
take=0
for a in "$@"; do
  if [ "$take" -eq 1 ]; then seen+=("$a"); fi
  if [ "$a" = "--" ]; then take=1; fi
done
printf 'ARGV=%s\\n' "${seen[*]}"
"""


def _run_shim_argv(home: Path, argv: list[str]) -> str:
    """Invoke the shim with ``argv`` and return the user-program argv the fake uv saw."""
    (home / "bin").mkdir(parents=True, exist_ok=True)
    uv = home / "bin" / "uv"
    uv.write_text(_FAKE_UV_ECHO_ARGS)
    uv.chmod(0o755)
    (home / "current").mkdir(exist_ok=True)
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / ".env").write_text("")

    env = {**os.environ, "CALFCORD_HOME": str(home)}
    result = subprocess.run(
        [str(home / "shims" / "calfcord"), *argv],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, f"shim failed: {result.stderr}"
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "ARGV":
            return value
    raise AssertionError(f"fake uv did not report ARGV; stdout was: {result.stdout!r}")


def test_shim_dispatches_init_to_calfcord_cli(tmp_path: Path) -> None:
    """``calfcord init`` must exec ``calfcord-cli init`` through the same `uv run`."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["init"]) == "calfcord-cli init"


def test_shim_passes_runner_commands_through_unchanged(tmp_path: Path) -> None:
    """A non-management command (e.g. a runner) is not rewritten by the dispatch."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["calfkit-bridge"]) == "calfkit-bridge"


def test_shim_exports_calfcord_home(tmp_path: Path) -> None:
    """The shim must export CALFCORD_HOME so calfcord-cli can locate config + agents."""
    home = tmp_path / "home"
    _install_shims(home)
    shim_text = (home / "shims" / "calfcord").read_text()
    assert 'export CALFCORD_HOME="$H"' in shim_text


@pytest.mark.skipif(not INSTALL_SH.exists(), reason="installer script missing")
def test_install_sh_parses() -> None:
    """The outer script must stay syntactically valid (``bash -n``)."""
    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
