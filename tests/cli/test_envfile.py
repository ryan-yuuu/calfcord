"""Tests for the position-preserving, atomic, 0600 dotenv upsert.

The install's ``config/.env`` is the seeded, heavily-commented ``.env.example``;
the whole point of :mod:`calfcord.cli._envfile` is to set a few keys without
disturbing those comments, the key order, or the file's permissions. These
tests pin exactly that: in-place replacement (comments survive), append for new
keys, byte-for-byte idempotency, and ``chmod 0600``.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from calfcord.cli._envfile import read_env, upsert


def test_read_env_parses_and_ignores_comments_and_blanks(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "FOO=bar\n"
        "  # indented comment\n"
        "BAZ = qux \n"  # surrounding whitespace stripped on both sides
        "QUOTED=\"with spaces\"\n"
        "SINGLE='single'\n"
        "NO_EQUALS_LINE\n"
    )
    assert read_env(env) == {
        "FOO": "bar",
        "BAZ": "qux",
        "QUOTED": "with spaces",
        "SINGLE": "single",
    }


def test_read_env_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_env(tmp_path / "nope.env") == {}


def test_read_env_last_assignment_wins(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("K=first\nK=second\n")
    assert read_env(env)["K"] == "second"


def test_upsert_replaces_in_place_preserving_preceding_comment(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# leading explanation for FOO\n"
        "FOO=old\n"
        "# trailing explanation for BAR\n"
        "BAR=keepme\n"
    )
    upsert(env, {"FOO": "new"})

    lines = env.read_text().splitlines()
    # The comment immediately preceding FOO must still be there, still adjacent.
    assert "# leading explanation for FOO" in lines
    assert lines.index("# leading explanation for FOO") + 1 == lines.index("FOO=new")
    # The unrelated key and its comment are untouched.
    assert "# trailing explanation for BAR" in lines
    assert "BAR=keepme" in lines
    assert read_env(env) == {"FOO": "new", "BAR": "keepme"}


def test_upsert_appends_new_keys_after_existing_content(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# header\nEXISTING=1\n")
    upsert(env, {"NEW_KEY": "value"})

    lines = env.read_text().splitlines()
    assert lines[0] == "# header"
    assert lines[1] == "EXISTING=1"
    assert lines[-1] == "NEW_KEY=value"


def test_upsert_creates_file_and_parent_dir(tmp_path: Path) -> None:
    env = tmp_path / "config" / ".env"  # parent does not exist yet
    upsert(env, {"K": "v"})
    assert env.exists()
    assert read_env(env) == {"K": "v"}


def test_upsert_is_byte_identical_on_rerun(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nFOO=old\nBAR=keep\n")
    upsert(env, {"FOO": "new", "NEW": "added"})
    first = env.read_bytes()
    upsert(env, {"FOO": "new", "NEW": "added"})
    second = env.read_bytes()
    assert first == second


def test_upsert_empty_updates_is_noop(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n")
    before = env.read_bytes()
    upsert(env, {})
    assert env.read_bytes() == before


def test_upsert_rejects_newline_value_and_leaves_file_untouched(tmp_path: Path) -> None:
    """A value with an embedded newline raises before any disk write.

    A second line would silently corrupt the file (and re-append on the next
    run), so the writer rejects it up front — and because the rejection happens
    before the atomic write, a not-yet-existing file is never created.
    """
    env = tmp_path / ".env"
    with pytest.raises(ValueError):
        upsert(env, {"K": "a\nb"})
    # Nothing was written: the file the upsert would have created is still absent.
    assert not env.exists()


def test_read_env_matches_loaders_on_hand_edited_lines(tmp_path: Path) -> None:
    """``read_env`` decodes hand-edited lines the way the runtime dotenv loaders do.

    The wizard's "current value" must agree with what the processes load, so a
    leading ``export``, a trailing `` # inline comment`` (space + hash), and a
    literal ``#`` with no preceding space are each parsed exactly as the loaders
    parse them. (Quoted-value cases are covered separately.)
    """
    env = tmp_path / ".env"
    env.write_text(
        "export FOO=bar\n"  # shell/dotenv export prefix → key is FOO
        "TOK=val # inline comment\n"  # space+hash starts an inline comment
        "HASH=ab#cd\n"  # no space before # → the hash is literal
    )
    parsed = read_env(env)
    assert parsed["FOO"] == "bar"
    assert parsed["TOK"] == "val"
    assert parsed["HASH"] == "ab#cd"


def test_upsert_sets_mode_0600(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    upsert(env, {"SECRET": "value"})
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600

    # And re-upsert keeps it 0600 even though it went through a fresh temp file.
    upsert(env, {"SECRET": "value2"})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
