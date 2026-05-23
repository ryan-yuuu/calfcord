"""Unit tests for the RoutingDecision schema.

The schema is what the built-in router agent's LLM populates via
pydantic-ai's ``ToolOutput`` pattern. The fan-out consumer reads it
off ``NodeResult.output``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from calfkit_organization.agents.routing import (
    ROUTER_OUTPUT_TOOL_NAME,
    RoutingDecision,
)


class TestRoutingDecision:
    def test_empty_agents_tuple_is_structurally_valid(self) -> None:
        # The schema accepts an empty tuple as defense-in-depth: the
        # router's system prompt instructs the LLM to ALWAYS pick at
        # least one agent (no silent-ignore policy), but if a
        # misbehaving model emits an empty list anyway, we want the
        # fan-out consumer's no-op path to handle it rather than
        # trigger a pydantic-ai structured-output retry storm.
        # ``min_length=0`` is intentional — see
        # :mod:`calfkit_organization.agents.routing` module docstring.
        decision = RoutingDecision(reasoning="defensive empty handling")
        assert decision.agents == ()
        assert decision.reasoning == "defensive empty handling"

    def test_single_agent(self) -> None:
        decision = RoutingDecision(
            agents=["scribe"],
            reasoning="scribe's description matches the topic",
        )
        # Pydantic coerces list → tuple at validation; we assert tuple.
        assert decision.agents == ("scribe",)
        assert isinstance(decision.agents, tuple)

    def test_multiple_agents(self) -> None:
        decision = RoutingDecision(
            agents=["scribe", "conan"],
            reasoning="topic spans both domains",
        )
        assert decision.agents == ("scribe", "conan")
        assert isinstance(decision.agents, tuple)

    def test_reasoning_is_required(self) -> None:
        with pytest.raises(ValidationError):
            RoutingDecision(agents=["scribe"])  # type: ignore[call-arg]

    def test_reasoning_empty_string_rejected(self) -> None:
        """An empty reasoning string is rejected — the field's
        documented purpose is operator-side debugging, and empty
        reasoning produces useless log lines."""
        with pytest.raises(ValidationError):
            RoutingDecision(reasoning="")

    def test_reasoning_max_length_rejected(self) -> None:
        """A misbehaving LLM emitting a >2000-char rationale is
        rejected at validation. Protects log files from flooding."""
        with pytest.raises(ValidationError):
            RoutingDecision(reasoning="x" * 2001)

    def test_reasoning_at_max_length_accepted(self) -> None:
        decision = RoutingDecision(reasoning="x" * 2000)
        assert len(decision.reasoning) == 2000

    def test_frozen(self) -> None:
        decision = RoutingDecision(reasoning="x")
        with pytest.raises(ValidationError):
            decision.agents = ("scribe",)  # type: ignore[misc]


class TestAgentIdValidation:
    """The schema enforces the agent_id regex on every entry — same
    constraint the bridge enforces on user-defined agents. Moving
    this to the type boundary catches malformed LLM output at parse
    time, before the fan-out tries to act on it."""

    def test_valid_ids_accepted(self) -> None:
        decision = RoutingDecision(
            agents=["scribe", "agent-1", "agent_2", "agt"],
            reasoning="all valid",
        )
        assert decision.agents == ("scribe", "agent-1", "agent_2", "agt")

    def test_uppercase_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agents=["Scribe"], reasoning="x")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agents=[""], reasoning="x")

    def test_special_characters_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agents=["scribe!"], reasoning="x")

    def test_over_32_chars_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agents=["x" * 33], reasoning="x")

    def test_at_32_chars_accepted(self) -> None:
        decision = RoutingDecision(agents=["x" * 32], reasoning="x")
        assert decision.agents == ("x" * 32,)


class TestAgentsDedupe:
    """The schema deduplicates ``agents`` at construction time so the
    fan-out consumer doesn't need a redundant second-line dedupe.
    Order is preserved by first-seen."""

    def test_adjacent_duplicates_collapsed(self) -> None:
        decision = RoutingDecision(
            agents=["scribe", "scribe", "conan"],
            reasoning="x",
        )
        assert decision.agents == ("scribe", "conan")

    def test_separated_duplicates_collapsed(self) -> None:
        decision = RoutingDecision(
            agents=["scribe", "conan", "scribe"],
            reasoning="x",
        )
        assert decision.agents == ("scribe", "conan")

    def test_all_duplicates_collapse_to_one(self) -> None:
        decision = RoutingDecision(
            agents=["scribe", "scribe", "scribe"],
            reasoning="x",
        )
        assert decision.agents == ("scribe",)


class TestAgentsMaxLength:
    """Hard cap on fan-out width protects against a misbehaving LLM
    that returns a 100+ agent list."""

    def test_at_max_length_accepted(self) -> None:
        # 16 is the documented cap.
        ids = [f"agent{i:02d}" for i in range(16)]
        decision = RoutingDecision(agents=ids, reasoning="x")
        assert len(decision.agents) == 16

    def test_over_max_length_rejected(self) -> None:
        ids = [f"agent{i:02d}" for i in range(17)]
        with pytest.raises(ValidationError):
            RoutingDecision(agents=ids, reasoning="x")

    def test_max_length_fires_before_dedupe(self) -> None:
        """Pins the validator ordering: pydantic enforces
        ``Field(max_length=...)`` BEFORE custom field validators
        run, so an input of 20 duplicate ``"scribe"`` entries is
        rejected as ``too_long`` rather than collapsing to a
        single-element tuple.

        This is the deliberate behavior: a 100-element list of all
        duplicates is still pathological LLM output and worth
        rejecting outright as a sanity check, rather than silently
        producing a one-element decision the operator may not
        expect. The dedupe is for "accidental small repeats"; the
        cap is for "runaway list".
        """
        with pytest.raises(ValidationError, match="too_long"):
            RoutingDecision(agents=["scribe"] * 20, reasoning="x")


class TestToolNameConstant:
    """``ROUTER_OUTPUT_TOOL_NAME`` ties the schema to the prompt and
    the factory's ``ToolOutput`` wiring. It's intentionally a
    one-line constant so both producers (factory) and the prompt
    template can import it."""

    def test_default_value(self) -> None:
        assert ROUTER_OUTPUT_TOOL_NAME == "dispatch"
