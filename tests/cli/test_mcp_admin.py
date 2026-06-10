"""Tests for ``calfcord mcp add|list|remove`` (:mod:`calfcord.cli.mcp_admin`).

The add command is dual-mode: an interactive wizard over the injected
:class:`Prompter` seam when no transport flag is given, and a flag-driven
non-interactive path (scripting parity with the old ``calfcord-mcp-add``).
Both funnel into the same validated writer, so a wizard answer and a flag
can never diverge in what they persist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from calfcord.cli import mcp_admin
from calfcord.cli._prompts import Choice, Prompter


class FakePrompter:
    """Scripted Prompter: queues per-shape answers, raises on unscripted use."""

    def __init__(
        self,
        *,
        text_results: list[str] | None = None,
        select_results: list[str] | None = None,
        confirm_results: list[bool] | None = None,
    ) -> None:
        self._text = list(text_results or [])
        self._select = list(select_results or [])
        self._confirm = list(confirm_results or [])
        self.confirm_messages: list[str] = []

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        if not self._select:
            raise AssertionError(f"unexpected select(): {message!r}")
        return self._select.pop(0)

    def text(self, message: str, *, default: str = "") -> str:
        if not self._text:
            raise AssertionError(f"unexpected text(): {message!r}")
        return self._text.pop(0)

    def secret(self, message: str) -> str:
        raise AssertionError(f"unexpected secret(): {message!r}")

    def confirm(self, message: str, *, default: bool = False) -> bool:
        if not self._confirm:
            raise AssertionError(f"unexpected confirm(): {message!r}")
        self.confirm_messages.append(message)
        return self._confirm.pop(0)

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        raise AssertionError(f"unexpected checkbox(): {message!r}")


def test_fake_prompter_satisfies_protocol() -> None:
    assert isinstance(FakePrompter(), Prompter)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {}}\n')
    return path


# ------------------------------------------------------------------ add: flags


def test_add_flags_stdio_writes_entry(tmp_path: Path, capsys) -> None:
    path = _config(tmp_path)
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="github",
        command="npx -y @modelcontextprotocol/server-github",
        env=["GITHUB_TOKEN=$GITHUB_TOKEN"],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    entry = json.loads(path.read_text())["mcpServers"]["github"]
    assert entry == {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"},
    }


def test_add_flags_env_name_shorthand_expands_to_var_ref(tmp_path: Path) -> None:
    """``--env NAME`` is shorthand for ``NAME=$NAME`` (pass the host var through)."""
    path = _config(tmp_path)
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="github",
        command="srv",
        env=["GITHUB_TOKEN"],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    entry = json.loads(path.read_text())["mcpServers"]["github"]
    assert entry["env"] == {"GITHUB_TOKEN": "$GITHUB_TOKEN"}


def test_add_flags_http_writes_typed_entry(tmp_path: Path) -> None:
    path = _config(tmp_path)
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="docs",
        command=None,
        env=[],
        url="https://docs.example.com/mcp",
        header=["Authorization=Bearer $DOCS_TOKEN"],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    entry = json.loads(path.read_text())["mcpServers"]["docs"]
    assert entry == {
        "type": "http",
        "url": "https://docs.example.com/mcp",
        "headers": {"Authorization": "Bearer $DOCS_TOKEN"},
    }


def test_add_flags_dry_run_prints_without_writing(tmp_path: Path, capsys) -> None:
    path = _config(tmp_path)
    original = path.read_text()
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="github",
        command="srv",
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=True,
        start=False,
        home=None,
    )
    assert rc == 0
    assert path.read_text() == original
    assert "github" in capsys.readouterr().out


def test_add_flags_existing_name_needs_force(tmp_path: Path, capsys) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {"github": {"command": "old"}}}\n')
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="github",
        command="new",
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 1
    assert "force" in capsys.readouterr().out.lower()
    assert json.loads(path.read_text())["mcpServers"]["github"] == {"command": "old"}


def test_add_flags_literal_secret_gets_var_nudge_but_writes(tmp_path: Path, capsys) -> None:
    """Literals are allowed (Cursor/Claude-Code parity) — the command still
    nudges toward a $VAR reference for secret-looking values."""
    path = _config(tmp_path)
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="github",
        command="srv",
        env=["GITHUB_TOKEN=ghp_literal123"],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "$" in out  # the nudge mentions $VAR
    entry = json.loads(path.read_text())["mcpServers"]["github"]
    assert entry["env"] == {"GITHUB_TOKEN": "ghp_literal123"}


# ----------------------------------------------------------------- add: wizard


def test_add_wizard_stdio_full_flow(tmp_path: Path, capsys) -> None:
    """Bare ``mcp add``: name → transport → command → env loop (empty line
    ends it) → preview confirm → write. Start prompt declined."""
    path = _config(tmp_path)
    prompter = FakePrompter(
        text_results=[
            "github",                                   # server name
            "npx -y @modelcontextprotocol/server-github",  # command line
            "GITHUB_TOKEN=$GITHUB_TOKEN",               # env #1
            "",                                          # end env loop
        ],
        select_results=["stdio"],
        confirm_results=[True, False],  # write? yes; start now? no
    )
    rc = mcp_admin.run_add(
        prompter,
        config_path=path,
        server=None,
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    entry = json.loads(path.read_text())["mcpServers"]["github"]
    assert entry == {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"},
    }
    # The preview showed the entry before the confirm.
    assert "mcpServers" in capsys.readouterr().out


def test_add_wizard_http_flow(tmp_path: Path) -> None:
    path = _config(tmp_path)
    prompter = FakePrompter(
        text_results=[
            "docs",
            "https://docs.example.com/mcp",
            "Authorization=Bearer $DOCS_TOKEN",
            "",
        ],
        select_results=["http"],
        confirm_results=[True, False],
    )
    rc = mcp_admin.run_add(
        prompter,
        config_path=path,
        server=None,
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    entry = json.loads(path.read_text())["mcpServers"]["docs"]
    assert entry["type"] == "http"
    assert entry["url"] == "https://docs.example.com/mcp"
    assert entry["headers"] == {"Authorization": "Bearer $DOCS_TOKEN"}


def test_add_wizard_declined_preview_writes_nothing(tmp_path: Path) -> None:
    path = _config(tmp_path)
    original = path.read_text()
    prompter = FakePrompter(
        text_results=["github", "srv", ""],
        select_results=["stdio"],
        confirm_results=[False],  # decline the write
    )
    rc = mcp_admin.run_add(
        prompter,
        config_path=path,
        server=None,
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 1
    assert path.read_text() == original


def test_add_wizard_invalid_name_reprompts(tmp_path: Path) -> None:
    path = _config(tmp_path)
    prompter = FakePrompter(
        text_results=["Bad-Name", "github", "srv", ""],
        select_results=["stdio"],
        confirm_results=[True, False],
    )
    rc = mcp_admin.run_add(
        prompter,
        config_path=path,
        server=None,
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    assert "github" in json.loads(path.read_text())["mcpServers"]


# ------------------------------------------------------------------------ list


def test_list_shows_configured_servers(tmp_path: Path, capsys) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"command": "npx", "args": ["-y", "srv"]},
                    "docs": {"type": "http", "url": "https://docs.example.com/mcp"},
                }
            }
        )
    )
    rc = mcp_admin.run_list(config_path=path, home=None)
    assert rc == 0
    out = capsys.readouterr().out
    assert "github" in out and "npx" in out
    assert "docs" in out and "https://docs.example.com/mcp" in out


def test_list_empty_config_hints_add(tmp_path: Path, capsys) -> None:
    rc = mcp_admin.run_list(config_path=_config(tmp_path), home=None)
    assert rc == 0
    assert "calfcord mcp add" in capsys.readouterr().out


# ---------------------------------------------------------------------- remove


def test_remove_confirms_and_deletes(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {"github": {"command": "x"}}}\n')
    prompter = FakePrompter(confirm_results=[True])
    rc = mcp_admin.run_remove(
        prompter, config_path=path, server="github", force=False, home=None
    )
    assert rc == 0
    assert json.loads(path.read_text())["mcpServers"] == {}


def test_remove_declined_keeps_entry(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {"github": {"command": "x"}}}\n')
    prompter = FakePrompter(confirm_results=[False])
    rc = mcp_admin.run_remove(
        prompter, config_path=path, server="github", force=False, home=None
    )
    assert rc == 1
    assert "github" in json.loads(path.read_text())["mcpServers"]


def test_remove_force_skips_confirm(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {"github": {"command": "x"}}}\n')
    rc = mcp_admin.run_remove(
        FakePrompter(), config_path=path, server="github", force=True, home=None
    )
    assert rc == 0
    assert json.loads(path.read_text())["mcpServers"] == {}


def test_remove_unknown_errors_actionably(tmp_path: Path, capsys) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {"docs": {"type": "http", "url": "https://d"}}}\n')
    rc = mcp_admin.run_remove(
        FakePrompter(), config_path=path, server="nope", force=True, home=None
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "nope" in out and "docs" in out


# ------------------------------------------------------- flags cross-validation



@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"command": "x", "url": "https://y"}, "mutually exclusive"),
        ({"url": "https://y", "env": ["A=B"]}, "--header with --url"),
        ({"command": "x", "header": ["A=B"]}, "--env with --command"),
        ({"url": "https://y", "cwd": "/tmp"}, "--cwd is for stdio"),
        ({"server": None, "command": "x"}, "give the server name"),
        ({"command": "   "}, "command must not be empty"),
        ({"command": "x", "env": ["A=1", "A=2"]}, "duplicate env key"),
        ({"url": "https://y", "header": ["NoEquals"]}, "expected KEY=VALUE"),
        ({"command": "x", "env": ["1BAD"]}, "valid"),
    ],
)
def test_add_flags_validation_rejections(
    tmp_path: Path, capsys, overrides: dict, fragment: str
) -> None:
    """Every flag cross-validation guard rejects with rc 1 and an actionable
    message, leaving the file untouched — re-pinning the contracts the old
    ``calfcord-mcp-add`` suite held before the removal."""
    path = _config(tmp_path)
    original = path.read_text()
    kwargs: dict = dict(
        config_path=path,
        server="github",
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    kwargs.update(overrides)
    rc = mcp_admin.run_add(FakePrompter(), **kwargs)
    assert rc == 1
    assert fragment in capsys.readouterr().out
    assert path.read_text() == original


def test_add_flags_dry_run_still_nudges_literals(tmp_path: Path, capsys) -> None:
    """The literal-secret nudge fires on --dry-run too — the preview is
    exactly when an operator inspects what they're about to commit."""
    path = _config(tmp_path)
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="github",
        command="srv",
        env=["TOKEN=literal123"],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=True,
        start=False,
        home=None,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "literal" in out and "$" in out


# ----------------------------------------------------------- wizard edge paths


def test_add_wizard_env_loop_reprompts_on_bad_then_duplicate_token(tmp_path: Path, capsys) -> None:
    """A malformed env token and a duplicate key each print an error and
    re-prompt; the loop only ends on the empty line."""
    path = _config(tmp_path)
    prompter = FakePrompter(
        text_results=[
            "github",
            "srv",
            "1BAD",            # invalid bare shorthand -> error + reprompt
            "TOKEN=$TOKEN",    # ok
            "TOKEN=$OTHER",    # duplicate key -> error + reprompt
            "",                 # end loop
        ],
        select_results=["stdio"],
        confirm_results=[True, False],
    )
    rc = mcp_admin.run_add(
        prompter,
        config_path=path,
        server=None,
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "1BAD" in out and "duplicate" in out
    entry = json.loads(path.read_text())["mcpServers"]["github"]
    assert entry["env"] == {"TOKEN": "$TOKEN"}


def test_add_wizard_empty_command_reprompts(tmp_path: Path) -> None:
    path = _config(tmp_path)
    prompter = FakePrompter(
        text_results=["github", "   ", "srv --flag", ""],
        select_results=["stdio"],
        confirm_results=[True, False],
    )
    rc = mcp_admin.run_add(
        prompter,
        config_path=path,
        server=None,
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=None,
    )
    assert rc == 0
    entry = json.loads(path.read_text())["mcpServers"]["github"]
    assert entry == {"command": "srv", "args": ["--flag"]}


def test_add_wizard_start_now_dispatches_roster(tmp_path: Path, monkeypatch) -> None:
    """Confirming "Start now?" dispatches mcp_start with the new server —
    the onboarding-grade add-and-you're-done flow."""
    from calfcord.supervisor import mcp_roster

    captured: dict = {}

    async def _start(home_arg, *, server, **kwargs):
        captured.update(home=home_arg, server=server)
        return 0

    monkeypatch.setattr(mcp_roster, "mcp_start", _start)
    path = _config(tmp_path)
    prompter = FakePrompter(
        text_results=["github", "srv", ""],
        select_results=["stdio"],
        confirm_results=[True, True],  # write? yes; start now? yes
    )
    rc = mcp_admin.run_add(
        prompter,
        config_path=path,
        server=None,
        command=None,
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=False,
        home=tmp_path,
    )
    assert rc == 0
    assert captured == {"home": tmp_path, "server": "github"}


def test_add_start_flag_without_home_prints_dev_hint(tmp_path: Path, capsys) -> None:
    path = _config(tmp_path)
    rc = mcp_admin.run_add(
        FakePrompter(),
        config_path=path,
        server="github",
        command="srv",
        env=[],
        url=None,
        header=[],
        cwd=None,
        force=False,
        dry_run=False,
        start=True,
        home=None,
    )
    assert rc == 0
    assert "CALFCORD_HOME" in capsys.readouterr().out


# ------------------------------------------------------------- list state column


def test_list_marks_running_and_stopped(tmp_path: Path, capsys, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(
        '{"mcpServers": {"github": {"command": "x"}, "docs": {"type": "http", "url": "https://d"}}}'
    )
    monkeypatch.setattr(mcp_admin, "_running_servers", lambda home: {"github"})
    rc = mcp_admin.run_list(config_path=path, home=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    github_line = next(line for line in out.splitlines() if line.startswith("github"))
    docs_line = next(line for line in out.splitlines() if line.startswith("docs"))
    assert "[running]" in github_line
    assert "[stopped]" in docs_line
