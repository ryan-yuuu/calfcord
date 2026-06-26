"""Tests for the disk-backed Codex prompt cache."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from calfcord.providers.codex.prompt_cache import (
    CachedEntry,
    PromptCache,
)


def _make_cache(tmp_path: Path) -> PromptCache:
    return PromptCache(base_dir=tmp_path / "codex_prompts")


class TestSaveAndLoad:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"hello world", etag='W/"abc123"')

        loaded = cache.load("models.json")
        assert loaded is not None
        assert loaded.name == "models.json"
        assert loaded.body == b"hello world"
        assert loaded.etag == 'W/"abc123"'
        assert isinstance(loaded.fetched_at, datetime)

    def test_save_handles_none_etag(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("prompt.md", b"body", etag=None)

        loaded = cache.load("prompt.md")
        assert loaded is not None
        assert loaded.etag is None
        assert loaded.body == b"body"

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"v1", etag="etag-1")
        cache.save("models.json", b"v2", etag="etag-2")

        loaded = cache.load("models.json")
        assert loaded is not None
        assert loaded.body == b"v2"
        assert loaded.etag == "etag-2"

    def test_save_persists_empty_body(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("empty", b"", etag="zero")

        loaded = cache.load("empty")
        assert loaded is not None
        assert loaded.body == b""
        assert loaded.etag == "zero"


class TestLoadFailureModes:
    def test_load_returns_none_when_body_missing(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        assert cache.load("never-saved.json") is None

    def test_load_returns_none_when_only_meta_present(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        # Write a meta with no body sibling.
        (cache.base_dir / "ghost.meta").write_text('{"etag": "x", "fetched_at": null}')
        assert cache.load("ghost") is None

    def test_load_treats_corrupt_meta_as_missing_etag(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"body", etag="real-etag")
        # Corrupt the meta in place.
        (cache.base_dir / "models.json.meta").write_text("not valid json {{{")

        loaded = cache.load("models.json")
        assert loaded is not None
        assert loaded.body == b"body"
        assert loaded.etag is None
        assert loaded.fetched_at is None

    def test_load_tolerates_missing_meta_after_body_present(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"body", etag="real-etag")
        (cache.base_dir / "models.json.meta").unlink()

        loaded = cache.load("models.json")
        assert loaded is not None
        assert loaded.etag is None
        assert loaded.fetched_at is None

    def test_load_ignores_corrupt_fetched_at_but_keeps_etag(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"body", etag="real-etag")
        (cache.base_dir / "models.json.meta").write_text('{"etag": "real-etag", "fetched_at": "garbage"}')

        loaded = cache.load("models.json")
        assert loaded is not None
        assert loaded.etag == "real-etag"
        assert loaded.fetched_at is None


class TestClear:
    def test_clear_removes_body_and_meta_files(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"a", etag="e1")
        cache.save("prompt.md", b"b", etag="e2")
        # Make sure files exist before clearing.
        assert len(cache.files()) == 2

        cache.clear()

        assert cache.files() == []
        # Body + meta files should be gone; lock files are preserved
        # (see test_clear_preserves_lock_files for the rationale).
        remaining = sorted(p.name for p in cache.base_dir.iterdir() if p.is_file())
        assert all(name.endswith(".lock") for name in remaining)

    def test_clear_preserves_lock_files(self, tmp_path: Path) -> None:
        """H2 regression: clear() must NOT unlink ``.lock`` files. A concurrent
        writer in another process may be holding ``<name>.lock`` via filelock;
        unlinking the lock file races against the in-flight save (POSIX creates
        a new inode at the same path on next save; Windows raises OSError).

        We can't easily simulate a concurrent live filelock in a unit test
        (filelock cleans up its sentinel files on POSIX after release), so we
        place a sentinel ``.lock`` file directly and verify ``clear()`` leaves
        it alone.
        """
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"a", etag="e1")
        # Simulate a concurrent writer's lock file being present.
        sentinel_lock = cache.base_dir / "in-flight.lock"
        sentinel_lock.write_text("")
        assert sentinel_lock.exists()

        cache.clear()

        # Lock file must survive the clear so the in-flight writer's
        # coordination isn't undermined.
        assert sentinel_lock.exists()
        # Body + meta should be gone, only the lock remains.
        remaining = sorted(p.name for p in cache.base_dir.iterdir() if p.is_file())
        assert remaining == ["in-flight.lock"]

    def test_clear_tolerates_missing_dir(self, tmp_path: Path) -> None:
        cache = PromptCache(base_dir=tmp_path / "never-existed")
        # Wipe the dir created by __init__ to simulate the missing case.
        for p in cache.base_dir.iterdir():
            p.unlink()
        cache.base_dir.rmdir()
        # Must not raise.
        cache.clear()


class TestFiles:
    def test_files_lists_entries(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"a", etag="etag-a")
        cache.save("prompt.md", b"b", etag="etag-b")

        listed = cache.files()
        assert {entry.name for entry in listed} == {"models.json", "prompt.md"}
        by_name = {entry.name: entry for entry in listed}
        assert by_name["models.json"].etag == "etag-a"
        assert by_name["prompt.md"].etag == "etag-b"
        assert by_name["models.json"].body == b"a"
        assert by_name["prompt.md"].body == b"b"

    def test_files_returns_empty_when_dir_empty(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        assert cache.files() == []

    def test_files_skips_lock_and_meta_siblings(self, tmp_path: Path) -> None:
        cache = _make_cache(tmp_path)
        cache.save("models.json", b"a", etag="etag-a")
        # Confirm the lock + meta exist on disk but don't appear as entries.
        assert (cache.base_dir / "models.json.meta").exists()

        listed = cache.files()
        assert len(listed) == 1
        assert listed[0].name == "models.json"


class TestBaseDirResolution:
    def test_explicit_base_dir_wins(self, tmp_path: Path) -> None:
        cache = PromptCache(base_dir=tmp_path / "explicit")
        assert cache.base_dir == tmp_path / "explicit"
        assert cache.base_dir.exists()

    def test_respects_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "from-env"
        monkeypatch.setenv("CALFCORD_PROMPT_CACHE_DIR", str(target))
        cache = PromptCache()
        assert cache.base_dir == target
        assert cache.base_dir.exists()

    def test_lands_under_calfcord_home_when_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The bug: a relocated install must keep the prompt cache beside the
        # rest of the install, not always at ~/.calfcord.
        monkeypatch.delenv("CALFCORD_PROMPT_CACHE_DIR", raising=False)
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "opt" / "calfcord"))
        cache = PromptCache()
        assert cache.base_dir == tmp_path / "opt" / "calfcord" / "codex_prompts"
        assert cache.base_dir.exists()

    def test_override_wins_over_calfcord_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CALFCORD_PROMPT_CACHE_DIR", str(tmp_path / "explicit"))
        cache = PromptCache()
        assert cache.base_dir == tmp_path / "explicit"

    def test_default_path_when_neither_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Redirect HOME so we don't actually pollute the user's filesystem.
        monkeypatch.delenv("CALFCORD_PROMPT_CACHE_DIR", raising=False)
        monkeypatch.delenv("CALFCORD_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        cache = PromptCache()
        assert cache.base_dir == tmp_path / ".calfcord" / "codex_prompts"


class TestEntryShape:
    def test_cached_entry_is_namedtuple(self) -> None:
        # Lightweight assertion that the public type is the expected shape.
        entry = CachedEntry(name="x", body=b"y", etag=None, fetched_at=None)
        assert entry.name == "x"
        assert entry.body == b"y"
        assert entry.etag is None
        assert entry.fetched_at is None
