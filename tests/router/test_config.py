"""Unit tests for :func:`load_router_config`.

The loader has three layers of behavior:

1. Discovery — default path vs. ``CALFKIT_ROUTER_CONFIG_PATH``, and the
   asymmetric "missing is OK only at the default path" rule.
2. Parsing — empty / malformed / non-mapping content must produce a
   ``ValueError`` whose message names the offending file.
3. Schema — pydantic ``extra="forbid"`` rejects unknown keys (typos
   like ``provder:`` and reserved fields like ``slash:`` /
   ``system_prompt:``), and field validators reject invalid enum
   values + out-of-range ints.

Each class targets one layer to keep failure diagnoses scoped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calfkit_organization.router.config import (
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    RouterConfig,
    load_router_config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test from a clean tmp_path with no env override.

    Without ``chdir`` a developer with a ``router.yml`` in their actual
    CWD would see tests pass/fail depending on filesystem state. We pin
    CWD to ``tmp_path`` so each test starts from "no router.yml exists
    at the default path."
    """
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    monkeypatch.chdir(tmp_path)


class TestDiscovery:
    """Default-path-missing is silent; explicit-path-missing is fatal."""

    def test_default_path_missing_returns_none(self) -> None:
        """Backward compat: deploys with no router.yml at CWD get
        env-var-only behavior, no exception."""
        assert load_router_config() is None

    def test_explicit_path_missing_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An operator who sets CALFKIT_ROUTER_CONFIG_PATH is opting in;
        a typo'd path must fail loudly rather than silently using
        defaults."""
        monkeypatch.setenv(CONFIG_PATH_ENV, str(tmp_path / "nope.yml"))
        with pytest.raises(FileNotFoundError, match="no such file exists"):
            load_router_config()

    def test_default_path_resolves_against_cwd(self, tmp_path: Path) -> None:
        """When the file is at ``./router.yml`` relative to CWD, the
        default-path lookup picks it up without any env-var plumbing."""
        (tmp_path / DEFAULT_CONFIG_PATH).write_text("provider: openai\n")
        config = load_router_config()
        assert config is not None
        assert config.provider == "openai"

    def test_explicit_path_loads_from_arbitrary_location(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The override path can point anywhere on disk."""
        elsewhere = tmp_path / "elsewhere" / "router-config.yml"
        elsewhere.parent.mkdir()
        elsewhere.write_text("model: claude-haiku-4-5\n")
        monkeypatch.setenv(CONFIG_PATH_ENV, str(elsewhere))
        config = load_router_config()
        assert config is not None
        assert config.model == "claude-haiku-4-5"

    def test_directory_at_default_path_treated_as_absent(
        self, tmp_path: Path
    ) -> None:
        """Docker's bind-mount of a missing host file creates a
        *directory* at the container path. The loader's ``is_file()``
        check degrades that to "no config present" instead of an
        ``IsADirectoryError`` from ``read_text``."""
        (tmp_path / DEFAULT_CONFIG_PATH).mkdir()
        assert load_router_config() is None


class TestParsing:
    """File-level parse errors include the path in their message."""

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        """Empty file is almost always a half-finished edit; failing
        loudly catches it rather than masking with all-defaults."""
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_router_config()

    def test_whitespace_only_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("   \n\n  \n")
        with pytest.raises(ValueError, match="empty"):
            load_router_config()

    def test_malformed_yaml_raises_with_path(self, tmp_path: Path) -> None:
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("provider: openai\n  : : invalid\n")
        with pytest.raises(ValueError, match="malformed YAML") as exc_info:
            load_router_config()
        # Path is included so the error is self-describing in container logs.
        assert str(path) in str(exc_info.value)

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        """A list / scalar / null at the top level can't be unpacked
        into RouterConfig kwargs."""
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("- provider: openai\n")
        with pytest.raises(ValueError, match="must be a mapping"):
            load_router_config()


class TestSchema:
    """Pydantic validation pins the schema invariants."""

    def test_all_fields_set(self, tmp_path: Path) -> None:
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text(
            "provider: openai-codex\n"
            "model: gpt-5.3-codex\n"
            "thinking_effort: minimal\n"
            "history_turns: 25\n"
        )
        config = load_router_config()
        assert config == RouterConfig(
            provider="openai-codex",
            model="gpt-5.3-codex",
            thinking_effort="minimal",
            history_turns=25,
        )

    def test_partial_fields(self, tmp_path: Path) -> None:
        """Missing keys default to None — caller falls back to env/code."""
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("provider: anthropic\n")
        config = load_router_config()
        assert config is not None
        assert config.provider == "anthropic"
        assert config.model is None
        assert config.thinking_effort is None
        assert config.history_turns is None

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        """A typo (``provder:`` for ``provider:``) surfaces at boot
        rather than silently using the wrong default."""
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("provder: openai\n")
        with pytest.raises(ValueError, match="invalid router config"):
            load_router_config()

    @pytest.mark.parametrize(
        "reserved_key",
        ["name", "slash", "display_name", "description", "role",
         "publish_topic", "tools", "system_prompt", "avatar_url"],
    )
    def test_reserved_field_rejected(
        self, tmp_path: Path, reserved_key: str
    ) -> None:
        """Router identity fields are project infrastructure, not
        operator-tunable. ``extra="forbid"`` makes any such field a
        boot-time error."""
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text(f"{reserved_key}: something\n")
        with pytest.raises(ValueError, match="invalid router config"):
            load_router_config()

    def test_invalid_provider_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("provider: cohere\n")
        with pytest.raises(ValueError, match="invalid router config"):
            load_router_config()

    def test_invalid_thinking_effort_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("thinking_effort: extreme\n")
        with pytest.raises(ValueError, match="invalid router config"):
            load_router_config()

    def test_history_turns_out_of_range(self, tmp_path: Path) -> None:
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("history_turns: 999\n")
        with pytest.raises(ValueError, match="invalid router config"):
            load_router_config()

    def test_history_turns_negative(self, tmp_path: Path) -> None:
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("history_turns: -1\n")
        with pytest.raises(ValueError, match="invalid router config"):
            load_router_config()

    def test_history_turns_zero_accepted(self, tmp_path: Path) -> None:
        """0 disables router history; valid per the ge=0 bound."""
        path = tmp_path / DEFAULT_CONFIG_PATH
        path.write_text("history_turns: 0\n")
        config = load_router_config()
        assert config is not None
        assert config.history_turns == 0
