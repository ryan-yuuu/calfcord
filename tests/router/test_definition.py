"""Unit tests for :func:`build_router_definition`.

The router definition is constructed in code. Its tunable config
(``provider`` / ``model`` / ``thinking_effort`` / ``history_turns``) and its
system prompt come from the bundled ``router.md`` (see
:func:`calfcord.router.prompt.load_router_md`); a field omitted
from the front matter falls through to the ``_DEFAULT_*`` in-code constant.

These tests cover three things:

1. The field invariants the registry depends on (agent_id, display_name,
   role, publish_topic, empty tools, source_path=None).
2. The shipped bundled default (the front matter copied from the old
   ``router.yml``).
3. The config-resolution chain (front matter value > in-code default),
   exercised via the ``CALFKIT_ROUTER_PROMPT_PATH`` override pointing at a
   temp ``router.md``.

Every test runs with the loader cache cleared and the override env var unset,
so the bundled file is read unless a test plants its own override.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.router import prompt
from calfcord.router.definition import (
    _MODEL_ENV,
    _PROVIDER_ENV,
    ROUTER_AGENT_ID,
    build_router_definition,
)


@pytest.fixture(autouse=True)
def _isolate_router_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the loader cache + the router env vars around every test.

    :func:`load_router_md` caches the parsed ``(config, prompt)`` on a module
    global and is eagerly populated at import. Resetting before each test makes
    the bundled-file read deterministic; resetting on teardown prevents a test
    that planted an override from leaking its cache into later tests/files.

    The provider/model override env vars are unset too. They are normally absent
    from the process environment, but an operator's project ``.env`` can carry
    them, and litellm's import-time ``load_dotenv()`` (pulled in transitively by
    some integration tests) injects that ``.env`` into ``os.environ`` mid-run.
    Clearing them here keeps the front-matter/default resolution tests below
    deterministic regardless of the host ``.env``; the ``TestEnvOverrides`` tests
    re-set them explicitly via ``monkeypatch.setenv``.
    """
    monkeypatch.delenv(prompt._PROMPT_PATH_ENV, raising=False)
    monkeypatch.delenv(_PROVIDER_ENV, raising=False)
    monkeypatch.delenv(_MODEL_ENV, raising=False)
    prompt._reset_cache_for_tests()
    yield
    prompt._reset_cache_for_tests()


def _write_router_md(path: Path, front_matter: str, body: str = "You are the router.") -> Path:
    path.write_text(f"---\n{front_matter}---\n{body}\n")
    return path


def _use_override(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv(prompt._PROMPT_PATH_ENV, str(path))
    prompt._reset_cache_for_tests()


class TestFieldInvariants:
    """The fields that other modules rely on are pinned here.

    Changing any of these is a topology-contract change and should be
    a deliberate, reviewed commit — these assertions exist to surface
    it as a test failure rather than a silent edit.
    """

    def test_returns_agent_definition(self) -> None:
        assert isinstance(build_router_definition(), AgentDefinition)

    def test_agent_id_is_router_constant(self) -> None:
        assert build_router_definition().agent_id == ROUTER_AGENT_ID == "_router"

    def test_display_name_is_router(self) -> None:
        assert build_router_definition().display_name == "Router"

    def test_role_is_router(self) -> None:
        assert build_router_definition().role == "router"

    def test_publish_topic_is_routing_decisions(self) -> None:
        """The fan-out consumer subscribes to this topic; the router's
        ReturnCall publishes here via FastStream's @publisher wrapping.
        Without publish_topic set the routing decisions go nowhere."""
        assert build_router_definition().publish_topic == "routing.decisions"

    def test_tools_is_empty(self) -> None:
        """Routers use the ToolOutput pattern, not function tools."""
        assert build_router_definition().tools == ()

    def test_source_path_is_none(self) -> None:
        """``router.md`` is bundled infrastructure, deliberately not exposed
        to the ``/thinking-effort`` frontmatter rewriter."""
        assert build_router_definition().source_path is None

    def test_avatar_url_is_none(self) -> None:
        assert build_router_definition().avatar_url is None

    def test_system_prompt_is_non_empty(self) -> None:
        d = build_router_definition()
        assert d.system_prompt.strip() != ""
        # Sanity: the rendered prompt mentions the dispatch tool (the tool
        # name pydantic-ai's ToolOutput pattern uses).
        assert "dispatch" in d.system_prompt


class TestBundledDefault:
    """The bundled ``router.md`` ships the values copied from the old
    ``router.yml``. ``history_turns`` is omitted from the front matter, so it
    falls through to the in-code default."""

    def test_provider(self) -> None:
        assert build_router_definition().provider == "openai-codex"

    def test_model(self) -> None:
        assert build_router_definition().model == "gpt-5.4-mini"

    def test_thinking_effort(self) -> None:
        assert build_router_definition().thinking_effort == "low"

    def test_history_turns_defaults(self) -> None:
        assert build_router_definition().history_turns == 10


class TestConfigResolution:
    """Front-matter value > in-code default, exercised via an override file."""

    def test_front_matter_supplies_all_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_override(
            monkeypatch,
            _write_router_md(
                tmp_path / "router.md",
                "provider: anthropic\n"
                "model: claude-haiku-4-5\n"
                "thinking_effort: high\n"
                "history_turns: 7\n",
            ),
        )
        d = build_router_definition()
        assert d.provider == "anthropic"
        assert d.model == "claude-haiku-4-5"
        assert d.thinking_effort == "high"
        assert d.history_turns == 7

    def test_omitted_fields_fall_to_code_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A partial front matter resolves per-field, not all-or-nothing."""
        _use_override(
            monkeypatch,
            _write_router_md(tmp_path / "router.md", "provider: anthropic\n"),
        )
        d = build_router_definition()
        assert d.provider == "anthropic"  # from front matter
        assert d.model == "gpt-5-nano"  # code default
        assert d.thinking_effort == "none"  # code default
        assert d.history_turns == 10  # code default

    def test_history_turns_zero_is_respected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``history_turns: 0`` is a valid "disable history" signal; the
        resolver must distinguish 0 from "not set" so a falsy-check bug
        doesn't fall through to the default."""
        _use_override(
            monkeypatch,
            _write_router_md(tmp_path / "router.md", "history_turns: 0\n"),
        )
        assert build_router_definition().history_turns == 0

    def test_body_becomes_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_override(
            monkeypatch,
            _write_router_md(
                tmp_path / "router.md",
                "provider: openai\n",
                body="Custom router instructions.",
            ),
        )
        assert build_router_definition().system_prompt == "Custom router instructions."

    def test_no_front_matter_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An override file with no ``---`` fences is rejected. Silently booting
        on all-default config (with the raw config text leaking into the prompt)
        would be a hard-to-diagnose footgun, so the loader fails loudly."""
        path = tmp_path / "router.md"
        path.write_text("Just a prompt, no front matter.\n")
        _use_override(monkeypatch, path)
        with pytest.raises(ValueError, match="no YAML front matter"):
            build_router_definition()


class TestEnvOverrides:
    """``CALFKIT_ROUTER_PROVIDER`` / ``CALFKIT_ROUTER_MODEL`` let an operator
    retarget the router without replacing ``router.md``, with precedence
    env > front matter > in-code default. ``monkeypatch.setenv`` auto-undoes,
    so none of these leak across tests."""

    def test_provider_env_overrides_bundled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The env value wins over the bundled ``openai-codex`` front matter."""
        monkeypatch.setenv(_PROVIDER_ENV, "anthropic")
        assert build_router_definition().provider == "anthropic"

    def test_model_env_overrides_bundled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_MODEL_ENV, "claude-haiku-4-5")
        assert build_router_definition().model == "claude-haiku-4-5"

    def test_unset_env_falls_back_to_bundled(self) -> None:
        """Regression guard: with neither override set, the bundled ``router.md``
        values apply unchanged — env overrides are purely additive."""
        d = build_router_definition()
        assert d.provider == "openai-codex"
        assert d.model == "gpt-5.4-mini"

    def test_invalid_provider_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An invalid override is still validated by pydantic — the env value
        flows into :class:`AgentDefinition` rather than bypassing validation."""
        monkeypatch.setenv(_PROVIDER_ENV, "nonsense")
        with pytest.raises(ValueError):
            build_router_definition()

    def test_model_env_alone_resolves_independently_of_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting ONLY the model env var must not drag the provider with it.

        Provider and model resolve as two independent ``env > front matter >
        default`` chains, not a coupled pair: with only ``CALFKIT_ROUTER_MODEL``
        set, the model comes from the env while the provider still comes from the
        bundled ``router.md`` front matter (``openai-codex``). A coupled-pair bug
        would either ignore the lone model override or reset the provider.
        """
        monkeypatch.setenv(_MODEL_ENV, "claude-haiku-4-5")
        d = build_router_definition()
        assert d.model == "claude-haiku-4-5"  # from the env override
        assert d.provider == "openai-codex"  # untouched: still the bundled front matter
