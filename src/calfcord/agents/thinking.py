"""Map operator-facing thinking effort tiers to provider-specific model settings.

Two consumers share this mapper:

* :class:`calfcord.agents.factory.AgentFactory` — bakes the
  declared ``thinking_effort`` from ``agents/<name>.md`` into the calfkit
  ``Agent`` constructor at agent boot (tier 2). This is the effort that
  governs runs the bridge doesn't intercept — native A2A peer consults and
  handoffs.
* :class:`calfcord.bridge.mention_handler.MentionHandler` — forwards
  the same effort as a per-call override (tier 3) on ``@<agent_id>``
  mentions, so a runtime ``/thinking-effort`` change takes effect on the
  next message without restarting the agent process.

The Anthropic ``budget_tokens`` ramp anchors its ``low`` / ``medium`` /
``high`` values (4000 / 10000 / 31999) to the same budgets Claude Code's
``think`` / ``megathink`` / ``ultrathink`` keywords trigger; see the PR
plan for sources. ``minimal`` uses Anthropic's documented floor of
``1024`` budget tokens — the lowest value the API accepts when
``type=enabled``. OpenAI's ``reasoning_effort`` exposes ``minimal`` /
``low`` / ``medium`` / ``high``; the operator ramp uses all four
distinct values with ``xhigh`` and ``max`` saturating at ``high``. See
the per-provider tables below for exact values.

Per-call override scope
-----------------------
Per-call (tier 3) overrides apply to every message the bridge answers.
The bridge only answers ``@<agent_id>`` mentions (ambient, un-mentioned
channel messages are no longer answered under the 0.12 caller surface), so
it always knows the target agent ahead of time and applies the current
override when one is set (no override → the run uses the boot-baked tier-2).
Runs the bridge doesn't intercept — native A2A peer consults and handoffs —
fall back to the tier-2 effort baked in at agent boot, so those pick up a
``/thinking-effort`` change only after the agent process restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from calfcord.agents.definition import Provider, ThinkingEffort

logger = logging.getLogger(__name__)

_ANTHROPIC_BUDGET_TOKENS: dict[ThinkingEffort, int] = {
    "minimal": 1024,
    "low": 4000,
    "medium": 10000,
    "high": 31999,
    "xhigh": 48000,
    "max": 63999,
}

# Operator ramp → OpenAI ``reasoning_effort``. Shifted up one step from
# the original 5-tier mapping when ``minimal`` was added: previously
# operator ``low`` mapped to OpenAI ``"minimal"``; now operator
# ``minimal`` does, and operator ``low`` / ``medium`` / ``high`` step
# through OpenAI ``"low"`` / ``"medium"`` / ``"high"`` distinctly.
# ``xhigh`` and ``max`` still saturate at OpenAI ``"high"`` (the API's
# top tier). This means existing OpenAI agents that previously declared
# ``thinking_effort: low|medium|high`` now run with one notch more
# reasoning effort on next restart — a deliberate one-time bump
# documented in the commit that added ``minimal``.
_OPENAI_REASONING_EFFORT: dict[ThinkingEffort, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def build_model_settings(
    provider: Provider,
    effort: ThinkingEffort | None,
) -> dict[str, Any] | None:
    """Build a calfkit ``model_settings`` dict for the given tier.

    Returns:
        - ``None`` when ``effort is None`` (no operator override configured;
          calfkit treats this as "use whatever was passed down the chain").
        - ``{}`` when ``effort == "none"`` (operator explicitly asked for
          no extra overrides; calfkit treats an empty dict the same as
          ``None`` on the merge path).
        - A provider-specific dict for all other tiers.

    Defensive: a typed-input effort that doesn't appear in the per-provider
    mapping table (e.g. a future tier name that slipped past pydantic via
    a hand-edited file) is logged and degrades to ``{}`` rather than
    raising. Unknown ``provider`` values raise :class:`ValueError` — those
    are a config-level bug that should fail fast.
    """
    if effort is None:
        return None
    if effort == "none":
        return {}

    if provider == "anthropic":
        budget = _ANTHROPIC_BUDGET_TOKENS.get(effort)
        if budget is None:
            logger.warning(
                "unknown anthropic effort tier %r; degrading to no override",
                effort,
            )
            return {}
        return {"anthropic_thinking": {"type": "enabled", "budget_tokens": budget}}

    if provider in ("openai", "openai-codex"):
        # ``openai-codex`` is the ChatGPT-subscription backend; it speaks the
        # same OpenAI Responses API as ``openai`` and accepts the same
        # ``reasoning_effort`` setting, so the effort ramp is identical.
        value = _OPENAI_REASONING_EFFORT.get(effort)
        if value is None:
            logger.warning(
                "unknown openai effort tier %r; degrading to no override",
                effort,
            )
            return {}
        return {"openai_reasoning_effort": value}

    raise ValueError(
        f"unknown provider {provider!r}; expected 'anthropic', 'openai', or 'openai-codex'"
    )


def build_model_settings_union(effort: ThinkingEffort | None) -> dict[str, Any] | None:
    """Build a **provider-blind** ``model_settings`` override for ``effort``.

    The bridge no longer knows an agent's provider (the registry that carried it
    is gone), and calfkit forwards ``model_settings`` raw with provider-specific
    keys. So the C11 per-call override emits BOTH keys at once —
    ``anthropic_thinking`` (a dict) and ``openai_reasoning_effort`` (a string):
    whichever model the target agent runs reads only its own key (pydantic-ai
    model clients ``.get()`` their key and ignore the foreign one), so one union
    dict is correct regardless of provider. See R-A1.

    Returns ``None`` for ``effort is None`` (no override) and ``{}`` for
    ``"none"`` (explicit no-override), mirroring :func:`build_model_settings`.
    """
    if effort is None:
        return None
    if effort == "none":
        return {}
    settings: dict[str, Any] = {}
    budget = _ANTHROPIC_BUDGET_TOKENS.get(effort)
    if budget is not None:
        settings["anthropic_thinking"] = {"type": "enabled", "budget_tokens": budget}
    reasoning = _OPENAI_REASONING_EFFORT.get(effort)
    if reasoning is not None:
        settings["openai_reasoning_effort"] = reasoning
    if not settings:
        logger.warning("unknown effort tier %r; degrading to no override", effort)
        return {}
    return settings
