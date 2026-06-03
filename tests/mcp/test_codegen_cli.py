"""Tests for the ``calfcord-mcp-codegen`` thin wrapper CLI.

The wrapper never actually spawns ``calfkit`` or talks to an MCP server
here: ``subprocess.run`` is monkeypatched to a recorder, so the tests assert
on the argv the wrapper *would* run (server positional reused, ``-o``
injected into the schemas dir, unknown flags forwarded) and on the
validation / verification behavior the wrapper owns.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from calfkit.mcp import McpToolDef

from calfcord.mcp import codegen_cli


def _record_run(returncode: int = 0):
    """Return a fake ``subprocess.run`` plus the list it records argv into."""
    calls: list[list[str]] = []

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=returncode)

    return _run, calls


# --------------------------------------------------------------------------
# argv assembly (pure, no subprocess)
# --------------------------------------------------------------------------


def test_build_command_stdio_injects_output_and_reuses_server() -> None:
    args, extras = codegen_cli._parse_args(["gmail", "--command", "npx -y srv"])
    cmd = codegen_cli._build_calfkit_command("calfkit", args, extras, Path("/x/schemas/gmail.py"))
    assert cmd == [
        "calfkit", "mcp", "codegen", "gmail",
        "--command", "npx -y srv",
        "-o", "/x/schemas/gmail.py",
    ]


def test_build_command_forwards_unknown_flags_and_known_options() -> None:
    args, extras = codegen_cli._parse_args(
        ["gmail", "--url", "http://x", "--token", "t", "--check", "--future", "--futval", "v"],
    )
    # Genuinely-unknown flags fall through to extras and are forwarded verbatim.
    assert extras == ["--future", "--futval", "v"]
    cmd = codegen_cli._build_calfkit_command("calfkit", args, extras, Path("/x/schemas/gmail.py"))
    assert cmd[:4] == ["calfkit", "mcp", "codegen", "gmail"]
    assert cmd[-2:] == ["-o", "/x/schemas/gmail.py"]
    for token in ("--url", "http://x", "--token", "t", "--check", "--future", "--futval", "v"):
        assert token in cmd


def test_known_value_flag_before_positional_does_not_capture_server() -> None:
    """The reason the value-flags are declared: argparse must consume
    ``--command``'s value so a flag *preceding* the positional can't make the
    value masquerade as the ``server`` name."""
    args, extras = codegen_cli._parse_args(["--command", "npx -y srv", "gmail"])
    assert args.server == "gmail"
    assert args.command == "npx -y srv"
    assert extras == []


# --------------------------------------------------------------------------
# validation gates (must fail before spawning calfkit)
# --------------------------------------------------------------------------


def test_invalid_server_name_exits_before_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _record_run()
    monkeypatch.setattr(codegen_cli.subprocess, "run", run)
    monkeypatch.setattr(codegen_cli, "_resolve_calfkit_executable", lambda: "calfkit")
    with pytest.raises(SystemExit) as ei:
        codegen_cli.main(["Gmail", "--command", "x"])  # uppercase: invalid server grammar
    assert ei.value.code == 2
    assert calls == []


def test_output_flag_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    run, calls = _record_run()
    monkeypatch.setattr(codegen_cli.subprocess, "run", run)
    monkeypatch.setattr(codegen_cli, "_resolve_calfkit_executable", lambda: "calfkit")
    with pytest.raises(SystemExit) as ei:
        codegen_cli.main(["gmail", "--command", "x", "-o", "/tmp/foo.py"])
    assert ei.value.code == 2
    assert calls == []


# --------------------------------------------------------------------------
# main(): delegation, return-code passthrough, verify gating
# --------------------------------------------------------------------------


def test_main_runs_verify_on_success_and_forwards_returncode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(codegen_cli, "SCHEMAS_DIR", tmp_path)
    (tmp_path / "gmail.py").write_text("# generated\n")  # so out.exists() is True
    run, calls = _record_run(returncode=0)
    monkeypatch.setattr(codegen_cli.subprocess, "run", run)
    monkeypatch.setattr(codegen_cli, "_resolve_calfkit_executable", lambda: "calfkit")
    verified: list[str] = []
    monkeypatch.setattr(codegen_cli, "_verify_in_catalog", lambda s, o: verified.append(s))

    with pytest.raises(SystemExit) as ei:
        codegen_cli.main(["gmail", "--command", "npx -y srv"])

    assert ei.value.code == 0
    assert verified == ["gmail"]
    # Output path computed into the (patched) schemas dir — not operator-chosen.
    assert calls[0][-2:] == ["-o", str(tmp_path / "gmail.py")]


def test_main_skips_verify_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(codegen_cli, "SCHEMAS_DIR", tmp_path)
    run, _calls = _record_run(returncode=2)
    monkeypatch.setattr(codegen_cli.subprocess, "run", run)
    monkeypatch.setattr(codegen_cli, "_resolve_calfkit_executable", lambda: "calfkit")
    called: list[str] = []
    monkeypatch.setattr(codegen_cli, "_verify_in_catalog", lambda s, o: called.append(s))

    with pytest.raises(SystemExit) as ei:
        codegen_cli.main(["gmail", "--command", "x"])

    assert ei.value.code == 2
    assert called == []


# --------------------------------------------------------------------------
# _verify_in_catalog(): present vs absent
# --------------------------------------------------------------------------


def test_verify_reports_registered(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        codegen_cli.discovery,
        "discover_mcp_catalog",
        lambda pkg: {"gmail": [McpToolDef(name="search"), McpToolDef(name="send")]},
    )
    codegen_cli._verify_in_catalog("gmail", Path("/x/gmail.py"))
    out = capsys.readouterr().out
    assert "registered with 2 tool(s)" in out
    assert "search" in out and "send" in out


def test_verify_warns_when_absent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(codegen_cli.discovery, "discover_mcp_catalog", lambda pkg: {})
    codegen_cli._verify_in_catalog("ghost", Path("/x/ghost.py"))
    err = capsys.readouterr().err
    assert "NOT in the discovered catalog" in err
    assert "ghost" in err


# --------------------------------------------------------------------------
# calfkit executable resolution
# --------------------------------------------------------------------------


def test_resolve_calfkit_prefers_colocated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "python").write_text("")
    (tmp_path / "calfkit").write_text("")
    monkeypatch.setattr(codegen_cli.sys, "executable", str(tmp_path / "python"))
    assert codegen_cli._resolve_calfkit_executable() == str(tmp_path / "calfkit")


def test_resolve_calfkit_falls_back_to_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "python").write_text("")  # no co-located calfkit next to it
    monkeypatch.setattr(codegen_cli.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(codegen_cli.shutil, "which", lambda name: "/usr/bin/calfkit")
    assert codegen_cli._resolve_calfkit_executable() == "/usr/bin/calfkit"
