"""Tests for the ``calfcord-mcp-add`` command.

The command never connects to an MCP server, so these are pure, offline tests:
they build entries from flags, exercise the secret-policy and schema gates, and
drive ``main`` end to end against a ``tmp_path`` ``mcp.json`` (located via the
``CALFCORD_MCP_CONFIG`` env var the command already honors).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from calfcord.mcp import config_cli

# --------------------------------------------------------------------------
# _build_entry — transport shaping
# --------------------------------------------------------------------------


def test_build_entry_stdio_splits_command_and_references_env() -> None:
    args = config_cli._parse_args(["gmail", "--command", "npx -y @org/srv", "--env", "GMAIL_TOKEN"])
    entry = config_cli._build_entry(args)
    assert entry == {
        "command": "npx",
        "args": ["-y", "@org/srv"],
        "env": {"GMAIL_TOKEN": "$GMAIL_TOKEN"},  # shorthand → reference, never a literal
    }


def test_build_entry_stdio_single_token_omits_args_and_env() -> None:
    args = config_cli._parse_args(["srv", "--command", "my-server"])
    assert config_cli._build_entry(args) == {"command": "my-server"}


def test_build_entry_http_writes_explicit_type_and_header() -> None:
    args = config_cli._parse_args(["drive", "--url", "https://x/drive", "--header", "Authorization=Bearer $TOK"])
    assert config_cli._build_entry(args) == {
        "type": "http",
        "url": "https://x/drive",
        "headers": {"Authorization": "Bearer $TOK"},
    }


def test_build_entry_env_explicit_key_value_form() -> None:
    args = config_cli._parse_args(["srv", "--command", "x", "--env", "API_KEY=${HOST_KEY}"])
    assert config_cli._build_entry(args)["env"] == {"API_KEY": "${HOST_KEY}"}


def test_build_entry_empty_command_exits() -> None:
    args = config_cli._parse_args(["srv", "--command", "   "])
    with pytest.raises(SystemExit):
        config_cli._build_entry(args)


# --------------------------------------------------------------------------
# secret policy — literals refused unless explicitly allowed
# --------------------------------------------------------------------------


def test_literal_header_value_is_refused() -> None:
    args = config_cli._parse_args(["drive", "--url", "https://x", "--header", "Authorization=Bearer abc123"])
    with pytest.raises(SystemExit) as ei:
        config_cli._build_entry(args)
    assert "no $VAR reference" in str(ei.value)


def test_literal_header_value_allowed_with_flag() -> None:
    args = config_cli._parse_args(
        ["drive", "--url", "https://x", "--header", "Content-Type=application/json", "--allow-literal"]
    )
    assert config_cli._build_entry(args)["headers"] == {"Content-Type": "application/json"}


def test_env_shorthand_rejects_invalid_var_name() -> None:
    args = config_cli._parse_args(["srv", "--command", "x", "--env", "not-a-var"])
    with pytest.raises(SystemExit) as ei:
        config_cli._build_entry(args)
    assert "valid env var name" in str(ei.value)


def test_duplicate_key_is_refused() -> None:
    args = config_cli._parse_args(["srv", "--command", "x", "--env", "A=$A", "--env", "A=$B"])
    with pytest.raises(SystemExit) as ei:
        config_cli._build_entry(args)
    assert "more than once" in str(ei.value)


def test_header_without_equals_is_refused() -> None:
    args = config_cli._parse_args(["drive", "--url", "https://x", "--header", "Authorization"])
    with pytest.raises(SystemExit) as ei:
        config_cli._build_entry(args)
    assert "must be KEY=VALUE" in str(ei.value)


# --------------------------------------------------------------------------
# _parse_args — validation gates (argparse exits 2)
# --------------------------------------------------------------------------


def test_invalid_server_name_exits() -> None:
    with pytest.raises(SystemExit) as ei:
        config_cli._parse_args(["Gmail", "--command", "x"])  # uppercase: invalid grammar
    assert ei.value.code == 2


def test_missing_transport_exits() -> None:
    with pytest.raises(SystemExit) as ei:
        config_cli._parse_args(["gmail"])
    assert ei.value.code == 2


def test_both_transports_exit() -> None:
    with pytest.raises(SystemExit) as ei:
        config_cli._parse_args(["gmail", "--command", "x", "--url", "https://x"])
    assert ei.value.code == 2


def test_env_with_url_exits() -> None:
    with pytest.raises(SystemExit) as ei:
        config_cli._parse_args(["drive", "--url", "https://x", "--env", "T"])
    assert ei.value.code == 2


def test_header_with_command_exits() -> None:
    with pytest.raises(SystemExit) as ei:
        config_cli._parse_args(["gmail", "--command", "x", "--header", "A=$B"])
    assert ei.value.code == 2


# --------------------------------------------------------------------------
# _validate_entry — schema gate
# --------------------------------------------------------------------------


def test_validate_entry_accepts_good_entry() -> None:
    config_cli._validate_entry("gmail", {"command": "npx", "args": ["-y", "x"]})  # no raise


def test_validate_entry_rejects_entry_without_transport() -> None:
    with pytest.raises(SystemExit) as ei:
        config_cli._validate_entry("gmail", {"args": ["x"]})  # neither command nor url
    assert "schema validation" in str(ei.value)


# --------------------------------------------------------------------------
# _load_config — read / skeleton / guards
# --------------------------------------------------------------------------


def test_load_config_absent_returns_skeleton(tmp_path: Path) -> None:
    assert config_cli._load_config(tmp_path / "nope.json") == {"mcpServers": {}}


def test_load_config_invalid_json_exits(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        config_cli._load_config(p)
    assert "not valid JSON" in str(ei.value)


def test_load_config_bare_shape_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"gmail": {"command": "npx"}}), encoding="utf-8")  # bare, no mcpServers
    with pytest.raises(SystemExit) as ei:
        config_cli._load_config(p)
    assert "bare/legacy" in str(ei.value)


# --------------------------------------------------------------------------
# main — end-to-end against a tmp mcp.json
# --------------------------------------------------------------------------


def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the command at a tmp mcp.json and silence the no-schema warning."""
    path = tmp_path / "mcp.json"
    monkeypatch.setenv("CALFCORD_MCP_CONFIG", str(path))
    # Pretend every test server has a committed schema so the warning branch
    # (covered separately) doesn't clutter unrelated assertions.
    monkeypatch.setattr(config_cli, "MCP_CATALOG", {"gmail": [], "drive": [], "srv": []})
    return path


def test_main_creates_file_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _cfg(tmp_path, monkeypatch)
    config_cli.main(["gmail", "--command", "npx -y @org/srv", "--env", "GMAIL_TOKEN"])
    written = json.loads(path.read_text())
    assert written == {
        "mcpServers": {"gmail": {"command": "npx", "args": ["-y", "@org/srv"], "env": {"GMAIL_TOKEN": "$GMAIL_TOKEN"}}}
    }
    assert path.read_text().endswith("\n")  # trailing newline


def test_main_merges_and_preserves_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _cfg(tmp_path, monkeypatch)
    path.write_text(json.dumps({"mcpServers": {"existing": {"command": "keep"}}}), encoding="utf-8")
    config_cli.main(["drive", "--url", "https://x/drive"])
    written = json.loads(path.read_text())
    assert list(written["mcpServers"]) == ["existing", "drive"]  # order preserved, new appended
    assert written["mcpServers"]["existing"] == {"command": "keep"}
    assert written["mcpServers"]["drive"] == {"type": "http", "url": "https://x/drive"}


def test_main_refuses_existing_without_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _cfg(tmp_path, monkeypatch)
    path.write_text(json.dumps({"mcpServers": {"gmail": {"command": "old"}}}), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        config_cli.main(["gmail", "--command", "new"])
    assert "--force" in str(ei.value)
    assert json.loads(path.read_text())["mcpServers"]["gmail"] == {"command": "old"}  # untouched


def test_main_force_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _cfg(tmp_path, monkeypatch)
    path.write_text(json.dumps({"mcpServers": {"gmail": {"command": "old"}}}), encoding="utf-8")
    config_cli.main(["gmail", "--command", "new", "--force"])
    assert json.loads(path.read_text())["mcpServers"]["gmail"] == {"command": "new"}


def test_main_dry_run_prints_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _cfg(tmp_path, monkeypatch)
    config_cli.main(["gmail", "--command", "npx -y srv", "--dry-run"])
    assert not path.exists()  # nothing written
    out = capsys.readouterr().out
    assert json.loads(out) == {"mcpServers": {"gmail": {"command": "npx", "args": ["-y", "srv"]}}}


def test_main_warns_when_no_schema_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "mcp.json"
    monkeypatch.setenv("CALFCORD_MCP_CONFIG", str(path))
    monkeypatch.setattr(config_cli, "MCP_CATALOG", {})  # nothing codegen'd
    config_cli.main(["gmail", "--command", "npx -y srv"])
    err = capsys.readouterr().err
    assert "no schema module is committed" in err
    assert "calfcord-mcp-codegen gmail" in err
