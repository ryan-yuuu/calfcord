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
    # seed_agents pre-creates exactly the dir the runtime's CALFKIT_STATE_DIR
    # points at (.../state/agents), not just the parent .../state.
    assert (home / "state" / "agents").is_dir()


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
    assert (home / "state" / "agents").is_dir()
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


def test_shim_empty_env_value_does_not_defeat_default(tmp_path: Path) -> None:
    """A bare ``KEY=`` in config/.env counts as UNSET, so the default still applies.

    ``.env.example`` ships ``CALFCORD_WORKSPACE_DIR=`` (empty); the shim must
    treat that as "not set" and still export the launch dir, otherwise
    ``uv run --env-file`` would inject an empty value and the documented
    "workspace = launch dir" default would never happen on a default install.
    """
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch, env_file="CALFCORD_WORKSPACE_DIR=\n")
    assert seen["CALFCORD_WORKSPACE_DIR"] != _UNSET
    assert os.path.realpath(seen["CALFCORD_WORKSPACE_DIR"]) == os.path.realpath(str(launch))


def test_shim_defers_to_nonempty_dotenv_agents_dir(tmp_path: Path) -> None:
    """A NON-empty ``CALFKIT_AGENTS_DIR=`` in config/.env still defers to --env-file.

    The shim must not export its own default over a real pinned value (it leaves
    it for ``uv run --env-file``), so the fake uv — which ignores --env-file —
    sees it unset. The unrelated CALFKIT_STATE_DIR still gets the home default.
    """
    home = tmp_path / "home"
    _install_shims(home)
    launch = tmp_path / "workdir"
    launch.mkdir()

    seen = _run_shim(home, cwd=launch, env_file="CALFKIT_AGENTS_DIR=/from/dotenv\n")
    assert seen["CALFKIT_AGENTS_DIR"] == _UNSET
    assert seen["CALFKIT_STATE_DIR"] == str(home / "state" / "agents")


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


def test_shim_dispatches_router_setup_to_calfcord_cli(tmp_path: Path) -> None:
    """``calfcord router setup`` must exec ``calfcord-cli router setup`` unchanged."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["router", "setup"]) == "calfcord-cli router setup"


def test_shim_dispatches_agent_to_calfcord_cli(tmp_path: Path) -> None:
    """``calfcord agent tools`` must exec ``calfcord-cli agent tools`` unchanged."""
    home = tmp_path / "home"
    _install_shims(home)
    assert _run_shim_argv(home, ["agent", "tools"]) == "calfcord-cli agent tools"


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


# --------------------------------------------- version lifecycle (source+invoke) ---
# These drive the activate/gc/version-marker machinery the same way the shim
# tests drive seeding: source ``install.sh`` (main guarded off) and call the
# individual functions against a throwaway ``$CALFCORD_HOME``. All offline.


def _make_version(home: Path, sha: str) -> Path:
    """Create a built ``versions/<sha>`` dir (the ``.calfcord-ok`` marker present)."""
    vdir = home / "versions" / sha
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / ".calfcord-ok").write_text("")
    return vdir


def _version_field(home: Path, key: str) -> str:
    """Read one ``KEY=value`` field out of the install's version marker (data, not source)."""
    text = (home / "version").read_text()
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :]
    raise AssertionError(f"{key} not found in version marker:\n{text}")


# ------------------------------------------------------------ activate_version ---


def test_activate_version_first_activation_has_empty_previous(tmp_path: Path) -> None:
    """The very first activation points ``current`` at the dir and records no previous."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    result = _source_and_run(f'activate_version "{aaa}"', home=home)
    assert result.returncode == 0, result.stderr

    assert (home / "current").resolve() == aaa.resolve()
    assert _version_field(home, "CALFCORD_COMMIT") == "aaa"
    # No outgoing version on a first install → previous is empty.
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == ""


def test_activate_version_records_outgoing_as_previous(tmp_path: Path) -> None:
    """A normal A→B update records prev=A and leaves A's dir in place."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")

    result = _source_and_run(
        f'activate_version "{aaa}"\nactivate_version "{bbb}"', home=home
    )
    assert result.returncode == 0, result.stderr

    assert (home / "current").resolve() == bbb.resolve()
    assert _version_field(home, "CALFCORD_COMMIT") == "bbb"
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == "aaa"
    # The predecessor's dir survives so it can serve as a rollback target.
    assert aaa.is_dir()


def test_reactivating_current_sha_preserves_rollback_target(tmp_path: Path) -> None:
    """Re-activating the already-current sha must NOT make it its own predecessor.

    The headline Critical fix: a no-op re-install (or ``self update`` while
    already current, which has no up-to-date short-circuit) re-runs
    ``activate_version`` for the same sha. If it recorded prev == current, the
    following ``gc_versions`` would delete the genuine rollback target. Instead
    the existing previous must be preserved across the re-activation, and the
    real predecessor's dir must survive GC.
    """
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = home / "versions" / "bbb"

    # Mirror the real install ordering: each version dir is only built right
    # before it's activated (the installer downloads B after A is current), so
    # the GC after each activation can't prune a not-yet-built sibling. ``bbb``
    # is materialised mid-script, just before its first activation.
    script = "\n".join(
        [
            f'activate_version "{aaa}"',  # A first
            'gc_versions aaa "$PREVIOUS_SHA"',
            f'mkdir -p "{bbb}" && : > "{bbb}/.calfcord-ok"',
            f'activate_version "{bbb}"',  # A → B; prev becomes aaa
            'gc_versions bbb "$PREVIOUS_SHA"',
            f'activate_version "{bbb}"',  # re-activate the current sha (the bug)
            'gc_versions bbb "$PREVIOUS_SHA"',
        ]
    )
    result = _source_and_run(script, home=home)
    assert result.returncode == 0, result.stderr

    # Previous still points at the genuine predecessor (aaa), NOT at bbb itself.
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == "aaa"
    assert _version_field(home, "CALFCORD_COMMIT") == "bbb"
    # The rollback target survived the re-activation + GC.
    assert aaa.is_dir()
    assert bbb.is_dir()


# ----------------------------------------------------------------- gc_versions ---


def test_gc_versions_keeps_current_and_previous_prunes_a_third(tmp_path: Path) -> None:
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")
    ccc = _make_version(home, "ccc")

    result = _source_and_run('gc_versions ccc bbb', home=home)
    assert result.returncode == 0, result.stderr

    # Current + previous kept; the unrelated third is pruned.
    assert ccc.is_dir()
    assert bbb.is_dir()
    assert not aaa.exists()


def test_gc_versions_prunes_nothing_with_only_cur_and_prev(tmp_path: Path) -> None:
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")

    result = _source_and_run('gc_versions bbb aaa', home=home)
    assert result.returncode == 0, result.stderr

    assert aaa.is_dir()
    assert bbb.is_dir()


# -------------------------------------------------------- calfcord-self rollback ---


def _run_self(home: Path, argv: list[str]) -> subprocess.CompletedProcess:
    """Invoke the generated ``calfcord-self`` shim against ``$CALFCORD_HOME=home``."""
    _install_shims(home)
    env = {**os.environ, "CALFCORD_HOME": str(home)}
    return subprocess.run(
        [str(home / "shims" / "calfcord-self"), *argv],
        env=env, capture_output=True, text=True, check=False,
    )


def test_self_rollback_flips_current_and_swaps_version_fields(tmp_path: Path) -> None:
    """After A→B, ``rollback`` points current at A and swaps the version fields."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")
    # Reach the post-update state (current=B, prev=A) via the real activate path.
    prep = _source_and_run(
        f'activate_version "{aaa}"\nactivate_version "{bbb}"', home=home
    )
    assert prep.returncode == 0, prep.stderr

    result = _run_self(home, ["rollback"])
    assert result.returncode == 0, result.stderr

    # current now points at A, and the marker swapped: commit=A, previous=B.
    assert (home / "current").resolve() == aaa.resolve()
    assert _version_field(home, "CALFCORD_COMMIT") == "aaa"
    assert _version_field(home, "CALFCORD_PREVIOUS_COMMIT") == "bbb"


def test_self_rollback_refuses_when_previous_lacks_ok_marker(tmp_path: Path) -> None:
    """``rollback`` refuses (exit 1) when the previous version dir is not built."""
    home = tmp_path / "home"
    aaa = _make_version(home, "aaa")
    bbb = _make_version(home, "bbb")
    prep = _source_and_run(
        f'activate_version "{aaa}"\nactivate_version "{bbb}"', home=home
    )
    assert prep.returncode == 0, prep.stderr
    # Remove the predecessor's build marker so it's no longer a valid target.
    (aaa / ".calfcord-ok").unlink()

    result = _run_self(home, ["rollback"])
    assert result.returncode == 1
    assert "no valid previous version" in result.stderr
    # current is untouched — still B.
    assert (home / "current").resolve() == bbb.resolve()


# ------------------------------------------------------ calfcord-self set-broker ---


def _read_config_env(home: Path) -> str:
    return (home / "config" / ".env").read_text()


def test_self_set_broker_writes_value_at_mode_600(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_self(home, ["set-broker", "broker.example.com:9092"])
    assert result.returncode == 0, result.stderr

    env_file = home / "config" / ".env"
    assert "CALF_HOST_URL=broker.example.com:9092" in env_file.read_text()
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_self_set_broker_replaces_not_appends_and_keeps_other_keys(tmp_path: Path) -> None:
    """A second set-broker REPLACES the line (single occurrence) and keeps unrelated keys."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / ".env").write_text("DISCORD_BOT_TOKEN=keepme\nCALF_HOST_URL=old:9092\n")

    result = _run_self(home, ["set-broker", "new-broker:9092"])
    assert result.returncode == 0, result.stderr

    text = _read_config_env(home)
    # Exactly one CALF_HOST_URL line, carrying the new value.
    broker_lines = [ln for ln in text.splitlines() if ln.startswith("CALF_HOST_URL=")]
    assert broker_lines == ["CALF_HOST_URL=new-broker:9092"]
    # The unrelated key is preserved.
    assert "DISCORD_BOT_TOKEN=keepme" in text


# -------------------------------------------------------------- seed_config ---


def test_seed_config_keeps_existing_env_untouched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    existing = "OPERATOR=edits\n"
    (home / "config" / ".env").write_text(existing)
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / ".env.example").write_text("EXAMPLE=value\n")

    result = _source_and_run(f'seed_config "{dest}"', home=home)
    assert result.returncode == 0, result.stderr
    # An operator's existing config is never clobbered by the seed.
    assert _read_config_env(home) == existing


def test_seed_config_creates_new_env_at_mode_600(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / ".env.example").write_text("EXAMPLE=value\n")

    result = _source_and_run(f'seed_config "{dest}"', home=home)
    assert result.returncode == 0, result.stderr

    env_file = home / "config" / ".env"
    assert env_file.read_text() == "EXAMPLE=value\n"
    assert (env_file.stat().st_mode & 0o777) == 0o600


# --------------------------------------------------------------- ensure_path ---


def test_ensure_path_is_idempotent(tmp_path: Path) -> None:
    """A second ``ensure_path`` must not re-append the export block to a profile.

    ``ensure_path`` edits the real ``$HOME`` rc files, so we point HOME at a temp
    dir holding a single seeded ``.zshrc`` and run the function twice; the block
    that adds ``$SHIM_DIR`` must appear exactly once.
    """
    home = tmp_path / "home"
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    zshrc = fake_home / ".zshrc"
    zshrc.write_text("# existing profile\n")

    extra = {"HOME": str(fake_home)}
    first = _source_and_run("ensure_path", home=home, extra_env=extra)
    assert first.returncode == 0, first.stderr
    second = _source_and_run("ensure_path", home=home, extra_env=extra)
    assert second.returncode == 0, second.stderr

    text = zshrc.read_text()
    shim_dir = str(home / "shims")
    # The shim dir is wired into PATH exactly once, not appended on every run.
    assert text.count(shim_dir) == 1
    assert text.count("# calfcord") == 1


# -------------------------------------------------- meta() parses, never sources ---


def test_self_meta_parses_value_as_data_never_sources(tmp_path: Path) -> None:
    """A version-marker value with shell metacharacters is data, never executed.

    ``meta()`` reads the marker by line-parsing, so a value containing a command
    substitution / backticks must NOT run. We plant such a value, run a
    ``calfcord-self`` command that reads the marker (``version``), and assert no
    side-effect file was created.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True)
    pwned = home / "PWNED"
    # A repo/ref value an attacker might try to smuggle into a sourced file.
    (home / "version").write_text(
        "CALFCORD_COMMIT=aaa\n"
        f'CALFCORD_REPO=$(touch {pwned})`touch {pwned}`\n'
        "CALFCORD_REF=main\n"
    )

    result = _run_self(home, ["version"])
    assert result.returncode == 0, result.stderr
    # The metacharacter value was treated as data — nothing executed it.
    assert not pwned.exists()
    # And the value still surfaced verbatim in the output (read, not run).
    assert "$(touch" in result.stdout
