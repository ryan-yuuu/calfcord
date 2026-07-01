"""Tests for the ``disco tools alias`` command handlers.

The handlers are pure-ish: given an install ``.env`` path (+ the tool surface
for ``add``), they read/validate/edit the ``CALFCORD_TOOLS_ALIAS`` line and
print operator-facing output. Tests use a tmp ``.env`` and a synthetic tool
surface so they need no broker, Discord, or real install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.cli import tool_aliases
from calfcord.cli._envfile import read_env

_TOOLS = {"terminal", "read_file", "todo"}
_ALIASABLE = {"terminal", "read_file"}  # todo holds per-session state
_KEY = "CALFCORD_TOOLS_ALIAS"


def _env(tmp_path: Path) -> Path:
    return tmp_path / ".env"


def _seed(path: Path, value: str) -> None:
    path.write_text(f"{_KEY}={value}\n", encoding="utf-8")


class TestAliasAdd:
    def test_writes_alias(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        p = _env(tmp_path)
        rc = tool_aliases.run_alias_add(
            env_path=p, src="terminal", dst="terminal_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        assert rc == 0
        assert read_env(p)[_KEY] == "terminal=terminal_eu"
        # Assert the success shape — "aliased" alone also appears in error
        # messages ("is already aliased", "can't be aliased").
        assert capsys.readouterr().out.startswith("aliased 'terminal' → 'terminal_eu'")

    def test_appends_to_existing_sorted(self, tmp_path: Path) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        rc = tool_aliases.run_alias_add(
            env_path=p, src="read_file", dst="read_file_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        assert rc == 0
        assert read_env(p)[_KEY] == "read_file=read_file_eu,terminal=terminal_eu"

    def test_preserves_other_env_keys(self, tmp_path: Path) -> None:
        p = _env(tmp_path)
        p.write_text(f"FOO=bar\n{_KEY}=terminal=terminal_eu\n", encoding="utf-8")
        tool_aliases.run_alias_add(
            env_path=p, src="read_file", dst="read_file_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        env = read_env(p)
        assert env["FOO"] == "bar"
        assert env[_KEY] == "read_file=read_file_eu,terminal=terminal_eu"

    def test_idempotent_readd(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        rc = tool_aliases.run_alias_add(
            env_path=p, src="terminal", dst="terminal_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        assert rc == 0
        assert "already" in capsys.readouterr().out.lower()
        assert read_env(p)[_KEY] == "terminal=terminal_eu"

    def test_unknown_src_returns_1_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = _env(tmp_path)
        rc = tool_aliases.run_alias_add(
            env_path=p, src="ghost", dst="ghost_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        assert rc == 1
        assert capsys.readouterr().out.startswith("error:")
        assert not p.exists()

    def test_non_aliasable_src_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = _env(tmp_path)
        rc = tool_aliases.run_alias_add(
            env_path=p, src="todo", dst="todo_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        assert rc == 1
        assert "can't be aliased" in capsys.readouterr().out

    def test_prints_restart_hint(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        tool_aliases.run_alias_add(
            env_path=_env(tmp_path), src="terminal", dst="terminal_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        assert "restart" in capsys.readouterr().out.lower()

    def test_malformed_existing_value_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = _env(tmp_path)
        _seed(p, "garbage")  # no '=' in the existing value
        rc = tool_aliases.run_alias_add(
            env_path=p, src="terminal", dst="terminal_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
        )
        assert rc == 1
        assert "malformed" in capsys.readouterr().out


class TestAliasList:
    def test_empty(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = tool_aliases.run_alias_list(env_path=_env(tmp_path))
        assert rc == 0
        assert "no tool aliases configured" in capsys.readouterr().out

    def test_lists_sorted(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu,read_file=read_file_eu")
        rc = tool_aliases.run_alias_list(env_path=p)
        assert rc == 0
        out = capsys.readouterr().out
        assert "read_file" in out and "terminal_eu" in out
        # Sorted by the source name (the actual sort key), not the target.
        assert out.index("read_file") < out.index("terminal")

    def test_malformed_value_errors(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        p = _env(tmp_path)
        _seed(p, "garbage")  # no '='
        rc = tool_aliases.run_alias_list(env_path=p)
        assert rc == 1
        assert capsys.readouterr().out.startswith("error:")


class TestAliasRemove:
    def test_remove_by_dst_writes_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        rc = tool_aliases.run_alias_remove(env_path=p, dst="terminal_eu")
        assert rc == 0
        assert read_env(p)[_KEY] == ""  # empty value when none remain
        assert "removed" in capsys.readouterr().out

    def test_remove_preserves_others(self, tmp_path: Path) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu,read_file=read_file_eu")
        tool_aliases.run_alias_remove(env_path=p, dst="terminal_eu")
        assert read_env(p)[_KEY] == "read_file=read_file_eu"

    def test_remove_nonexistent_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        rc = tool_aliases.run_alias_remove(env_path=p, dst="ghost_eu")
        assert rc == 1
        assert "no alias" in capsys.readouterr().out
        assert read_env(p)[_KEY] == "terminal=terminal_eu"  # unchanged

    def test_prints_restart_hint(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        tool_aliases.run_alias_remove(env_path=p, dst="terminal_eu")
        assert "restart" in capsys.readouterr().out.lower()

    def test_malformed_existing_value_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = _env(tmp_path)
        _seed(p, "garbage")  # no '=' in the existing value
        rc = tool_aliases.run_alias_remove(env_path=p, dst="terminal_eu")
        assert rc == 1
        assert "malformed" in capsys.readouterr().out


class TestApplyRestartCallback:
    """When ``--restart`` is requested the CLI injects an ``apply_restart``
    callback; the handler calls it on an ACTUAL change (and falls back to the
    hint when it isn't given). It must NOT fire on a validation error or an
    idempotent no-op."""

    def test_add_calls_apply_restart_on_change(self, tmp_path: Path) -> None:
        called: list[bool] = []
        rc = tool_aliases.run_alias_add(
            env_path=_env(tmp_path), src="terminal", dst="terminal_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
            apply_restart=lambda: called.append(True),
        )
        assert rc == 0
        assert called == [True]

    def test_add_does_not_call_on_validation_error(self, tmp_path: Path) -> None:
        called: list[bool] = []
        rc = tool_aliases.run_alias_add(
            env_path=_env(tmp_path), src="ghost", dst="ghost_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
            apply_restart=lambda: called.append(True),
        )
        assert rc == 1
        assert called == []

    def test_add_idempotent_does_not_call(self, tmp_path: Path) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        called: list[bool] = []
        rc = tool_aliases.run_alias_add(
            env_path=p, src="terminal", dst="terminal_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE,
            apply_restart=lambda: called.append(True),
        )
        assert rc == 0
        assert called == []  # no change → no restart

    def test_remove_calls_apply_restart_on_change(self, tmp_path: Path) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        called: list[bool] = []
        tool_aliases.run_alias_remove(
            env_path=p, dst="terminal_eu",
            apply_restart=lambda: called.append(True),
        )
        assert called == [True]

    def test_remove_nonexistent_does_not_call(self, tmp_path: Path) -> None:
        p = _env(tmp_path)
        _seed(p, "terminal=terminal_eu")
        called: list[bool] = []
        rc = tool_aliases.run_alias_remove(
            env_path=p, dst="ghost_eu",
            apply_restart=lambda: called.append(True),
        )
        assert rc == 1
        assert called == []


class TestEndToEndPropagation:
    """CLI write → runtime read: the alias the CLI writes to ``.env`` is exactly
    what ``apply_deploy_filters`` reads at boot. This guards the single grammar
    (``serialize_alias_map`` on write, ``parse_alias_csv`` on read — spec §6/§8)
    across the CLI/runtime boundary against the REAL tool surface."""

    def test_cli_written_alias_registers_at_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from calfcord.tools import ALL_TOOLS
        from calfcord.tools.deploy_filters import apply_deploy_filters, is_aliasable

        tool_names = {n.tool_schema.name for n in ALL_TOOLS}
        aliasable = {n.tool_schema.name for n in ALL_TOOLS if is_aliasable(n)}

        p = _env(tmp_path)
        rc = tool_aliases.run_alias_add(
            env_path=p, src="terminal", dst="terminal_eu",
            tool_names=tool_names, aliasable_names=aliasable,
        )
        assert rc == 0

        # Load exactly what the CLI wrote into the process env (as --env-file
        # would), then compose the registry the way every role does at boot.
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", read_env(p)[_KEY])
        monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
        registry = apply_deploy_filters(ALL_TOOLS)
        assert "terminal" in registry  # original kept (no include filter)
        assert "terminal_eu" in registry  # the CLI-written alias resolved
        assert registry["terminal_eu"].subscribe_topics == ["tool.terminal_eu.input"]
