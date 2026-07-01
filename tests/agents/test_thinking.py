"""Unit tests for the effort-tier → provider model_settings mapping."""

from __future__ import annotations

import pytest

from calfcord.agents.thinking import build_model_settings, build_model_settings_union


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
            # comment in :mod:`calfcord.agents.thinking`
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


class TestOpenAICodexMapping:
    """The ``openai-codex`` provider routes through the same OpenAI Responses
    API as ``openai``, so it accepts the same ``reasoning_effort`` setting and
    uses the same operator → backend ramp. This test pins that equivalence so
    a future Codex-specific override (e.g. saturating earlier because of
    subscription-tier rate limits) is a deliberate change, not a silent drift.
    """

    @pytest.mark.parametrize(
        ("effort", "value"),
        [
            ("minimal", "minimal"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("xhigh", "high"),
            ("max", "high"),
        ],
    )
    def test_codex_reasoning_effort_matches_openai(self, effort: str, value: str) -> None:
        result = build_model_settings("openai-codex", effort)  # type: ignore[arg-type]
        assert result == {"openai_reasoning_effort": value}


class TestUnknownProvider:
    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown provider"):
            build_model_settings("xai", "high")  # type: ignore[arg-type]


class TestBuildModelSettingsUnion:
    """The provider-blind C11 override (R-A1): the bridge no longer knows an
    agent's provider, so it emits BOTH provider keys at once — each model client
    reads only its own (``.get()``) and ignores the foreign one."""

    def test_unset_returns_none(self) -> None:
        assert build_model_settings_union(None) is None

    def test_none_tier_returns_empty_dict(self) -> None:
        assert build_model_settings_union("none") == {}

    @pytest.mark.parametrize(
        ("effort", "budget", "reasoning"),
        [
            ("minimal", 1024, "minimal"),
            ("low", 4000, "low"),
            ("medium", 10000, "medium"),
            ("high", 31999, "high"),
            ("xhigh", 48000, "high"),
            ("max", 63999, "high"),
        ],
    )
    def test_emits_both_provider_keys(self, effort: str, budget: int, reasoning: str) -> None:
        assert build_model_settings_union(effort) == {  # type: ignore[arg-type]
            "anthropic_thinking": {"type": "enabled", "budget_tokens": budget},
            "openai_reasoning_effort": reasoning,
        }

    @pytest.mark.parametrize("bad", ["ultra", "ludicrous", "HIGH", ""])
    def test_unknown_tier_degrades_to_empty_dict(self, bad: str, caplog: pytest.LogCaptureFixture) -> None:
        """A stale/garbage tier (this reads a raw ``str`` straight off the SQLite
        overrides map on the per-turn hot path) must degrade to ``{}`` — NOT raise.
        A raise here would dark-out the agent on EVERY @mention until the DB is
        hand-edited. Warns so the bad tier is diagnosable."""
        with caplog.at_level("WARNING"):
            assert build_model_settings_union(bad) == {}  # type: ignore[arg-type]
        assert any("unknown effort tier" in r.message for r in caplog.records)
