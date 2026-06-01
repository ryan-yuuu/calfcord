"""Unit tests for memory-prompt loading, rendering, and the instructions hook.

The bridge is the single reader of the prompt file; agents receive the raw
template in ``deps`` and a per-agent hook localizes it at runtime. These tests
cover all three pure pieces without standing up Kafka or an LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from calfkit_organization.agents import memory


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts and ends with a clean prompt cache."""
    memory._reset_cache_for_tests()
    yield
    memory._reset_cache_for_tests()


class TestLoadMemoryPrompt:
    def test_default_bundled_prompt_loads_nonempty(self) -> None:
        text = memory.load_memory_prompt()
        assert text.strip()
        # The raw template still carries the placeholder (localization is the
        # hook's job, not the loader's).
        assert memory._MEMORY_DIR_PLACEHOLDER in text

    def test_caches_after_first_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = memory.load_memory_prompt()
        # Point the env at a bad path; the cache must make this a no-op.
        monkeypatch.setenv(memory._PROMPT_PATH_ENV, "/nonexistent/memory.md")
        assert memory.load_memory_prompt() is first

    def test_env_override_is_read(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        custom = tmp_path / "custom.md"
        custom.write_text("CUSTOM {{MEMORY_DIR}} PROMPT", encoding="utf-8")
        monkeypatch.setenv(memory._PROMPT_PATH_ENV, str(custom))
        memory._reset_cache_for_tests()
        assert memory.load_memory_prompt() == "CUSTOM {{MEMORY_DIR}} PROMPT"

    def test_missing_override_raises(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(memory._PROMPT_PATH_ENV, str(tmp_path / "nope.md"))
        memory._reset_cache_for_tests()
        with pytest.raises(ValueError, match="cannot read memory prompt"):
            memory.load_memory_prompt()

    def test_empty_override_raises(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        empty = tmp_path / "empty.md"
        empty.write_text("   \n", encoding="utf-8")
        monkeypatch.setenv(memory._PROMPT_PATH_ENV, str(empty))
        memory._reset_cache_for_tests()
        with pytest.raises(ValueError, match="empty"):
            memory.load_memory_prompt()


class TestRenderMemoryBlock:
    def test_interpolates_agent_dir(self) -> None:
        assert memory.render_memory_block("at {{MEMORY_DIR}} ok", "scribe") == "at memory/scribe/ ok"

    def test_default_template_renders_without_placeholder_leak(self) -> None:
        rendered = memory.render_memory_block(memory.load_memory_prompt(), "scribe")
        assert "{{MEMORY_DIR}}" not in rendered
        assert "memory/scribe/" in rendered


class TestMemoryInstructionsHook:
    @staticmethod
    def _ctx(deps: object) -> SimpleNamespace:
        # The hook only reads ``ctx.deps``; a namespace stands in for the
        # pydantic-ai RunContext so we needn't construct a real one.
        return SimpleNamespace(deps=deps)

    def test_returns_localized_block_from_deps(self) -> None:
        hook = memory.memory_instructions("scribe")
        ctx = self._ctx({memory.MEMORY_PROMPT_DEPS_KEY: "mem at {{MEMORY_DIR}}"})
        assert hook(ctx) == "mem at memory/scribe/"

    def test_none_when_key_absent(self) -> None:
        hook = memory.memory_instructions("scribe")
        assert hook(self._ctx({"phonebook": []})) is None

    def test_none_when_deps_not_a_dict(self) -> None:
        hook = memory.memory_instructions("scribe")
        assert hook(self._ctx(None)) is None

    def test_none_when_template_blank(self) -> None:
        hook = memory.memory_instructions("scribe")
        assert hook(self._ctx({memory.MEMORY_PROMPT_DEPS_KEY: ""})) is None

    def test_localizes_per_agent_id(self) -> None:
        deps = {memory.MEMORY_PROMPT_DEPS_KEY: "{{MEMORY_DIR}}"}
        assert memory.memory_instructions("scribe")(self._ctx(deps)) == "memory/scribe/"
        assert memory.memory_instructions("conan")(self._ctx(deps)) == "memory/conan/"
