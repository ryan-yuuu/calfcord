"""Unit tests for :class:`RouterConfig` — the ``router.md`` front-matter schema.

:class:`RouterConfig` validates the YAML front matter of ``router.md`` (the
loader, :func:`calfkit_organization.router.prompt.load_router_md`, is tested in
``test_prompt.py``). The schema is strict by design:

1. ``extra="forbid"`` rejects unknown keys (typos like ``provder:``) and
   reserved router-identity fields (``system_prompt:``, ``role:``, ...).
2. The ``Provider`` / ``ThinkingEffort`` literals reject invalid enum values.
3. ``history_turns`` is bounded to ``0..100``.
4. Every field is optional — an omitted field stays ``None`` so the caller can
   fall through to the in-code default.

The model is also ``frozen`` so a parsed config can't be mutated in place.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from calfkit_organization.router.config import RouterConfig


class TestRouterConfigSchema:
    def test_all_fields_set(self) -> None:
        config = RouterConfig(
            provider="openai-codex",
            model="gpt-5.3-codex",
            thinking_effort="minimal",
            history_turns=25,
        )
        assert config.provider == "openai-codex"
        assert config.model == "gpt-5.3-codex"
        assert config.thinking_effort == "minimal"
        assert config.history_turns == 25

    def test_all_fields_optional(self) -> None:
        """Missing keys default to None — the caller falls back to code."""
        config = RouterConfig()
        assert config.provider is None
        assert config.model is None
        assert config.thinking_effort is None
        assert config.history_turns is None

    def test_unknown_field_rejected(self) -> None:
        """A typo (``provder:`` for ``provider:``) surfaces at boot rather
        than silently using the wrong default."""
        with pytest.raises(ValidationError, match="provder"):
            RouterConfig(provder="openai")  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "reserved_key",
        ["name", "display_name", "description", "role",
         "publish_topic", "tools", "system_prompt", "avatar_url"],
    )
    def test_reserved_field_rejected(self, reserved_key: str) -> None:
        """Router identity fields are project infrastructure, not
        operator-tunable. ``extra="forbid"`` makes any such key an error."""
        with pytest.raises(ValidationError, match=reserved_key):
            RouterConfig(**{reserved_key: "something"})

    def test_invalid_provider_rejected(self) -> None:
        with pytest.raises(ValidationError, match="provider"):
            RouterConfig(provider="cohere")  # type: ignore[arg-type]

    def test_invalid_thinking_effort_rejected(self) -> None:
        with pytest.raises(ValidationError, match="thinking_effort"):
            RouterConfig(thinking_effort="extreme")  # type: ignore[arg-type]

    def test_empty_model_rejected(self) -> None:
        """An empty ``model: ""`` must fail at the boundary rather than be
        silently swallowed by the ``config.model or _DEFAULT_MODEL`` fallback."""
        with pytest.raises(ValidationError, match="model"):
            RouterConfig(model="")

    def test_history_turns_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError, match="history_turns"):
            RouterConfig(history_turns=999)

    def test_history_turns_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="history_turns"):
            RouterConfig(history_turns=-1)

    def test_history_turns_zero_accepted(self) -> None:
        """0 disables router history; valid per the ge=0 bound."""
        assert RouterConfig(history_turns=0).history_turns == 0

    def test_frozen(self) -> None:
        """A parsed config is immutable — no in-place mutation."""
        config = RouterConfig(provider="openai")
        with pytest.raises(ValidationError, match="frozen"):
            config.provider = "anthropic"  # type: ignore[misc]
