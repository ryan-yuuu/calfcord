"""Unit tests for the effort-tier → provider model_settings mapping."""

from __future__ import annotations

import pytest

from calfkit_organization.agents.thinking import build_model_settings


class TestNoneAndUnset:
    def test_effort_none_object_returns_empty_dict(self) -> None:
        """``effort='none'`` is an explicit operator choice — empty dict, not None."""
        assert build_model_settings("anthropic", "none") == {}
        assert build_model_settings("openai", "none") == {}

    def test_unset_effort_returns_none(self) -> None:
        """``effort=None`` means "no operator override configured" — return None."""
        assert build_model_settings("anthropic", None) is None
        assert build_model_settings("openai", None) is None


class TestAnthropicMapping:
    @pytest.mark.parametrize(
        ("effort", "budget"),
        [
            # 1024 is Anthropic's documented floor for ``budget_tokens``
            # when ``type=enabled``; the lightest non-zero reasoning tier.
            ("minimal", 1024),
            ("low", 4000),
            ("medium", 10000),
            ("high", 31999),
            ("xhigh", 48000),
            ("max", 63999),
        ],
    )
    def test_anthropic_budget_ramp(self, effort: str, budget: int) -> None:
        result = build_model_settings("anthropic", effort)  # type: ignore[arg-type]
        assert result == {
            "anthropic_thinking": {"type": "enabled", "budget_tokens": budget}
        }


class TestOpenAIMapping:
    @pytest.mark.parametrize(
        ("effort", "value"),
        [
            # Operator ramp maps 1:1 onto OpenAI's four
            # ``reasoning_effort`` tiers for ``minimal`` through
            # ``high``. ``xhigh`` and ``max`` saturate at OpenAI's
            # top tier (``high``) since the API exposes no higher
            # value. This ramp was shifted up one notch when
            # operator ``minimal`` was added — see the lookup-table
            # comment in :mod:`calfkit_organization.agents.thinking`
            # for the migration rationale.
            ("minimal", "minimal"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("xhigh", "high"),
            ("max", "high"),
        ],
    )
    def test_openai_reasoning_effort_ramp(self, effort: str, value: str) -> None:
        result = build_model_settings("openai", effort)  # type: ignore[arg-type]
        assert result == {"openai_reasoning_effort": value}


class TestUnknownProvider:
    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown provider"):
            build_model_settings("xai", "high")  # type: ignore[arg-type]
