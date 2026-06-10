"""Unit tests for the RoutingDecision schema.

The schema is what the built-in router agent's LLM populates via
pydantic-ai's ``ToolOutput`` pattern. The fan-out consumer reads it
off ``ConsumerContext.output``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from calfcord.agents.routing import (
    ROUTER_OUTPUT_TOOL_NAME,
    RoutingDecision,
)


class TestRoutingDecision:
    def test_agent_id_none_is_structurally_valid(self) -> None:
        # The schema accepts ``agent_id=None`` as defense-in-depth: the
        # router's system prompt instructs the LLM to ALWAYS pick exactly
        # one agent, but if a misbehaving model emits a tool call with
        # no ``agent_id`` anyway, we want the fan-out consumer's no-op
        # path to handle it rather than trigger a pydantic-ai
        # structured-output retry storm. See
        # :mod:`calfcord.agents.routing` module docstring.
        decision = RoutingDecision(reasoning="defensive empty handling")
        assert decision.agent_id is None
        assert decision.reasoning == "defensive empty handling"

    def test_single_agent(self) -> None:
        decision = RoutingDecision(
            agent_id="scribe",
            reasoning="scribe's description matches the topic",
        )
        assert decision.agent_id == "scribe"

    def test_reasoning_is_required(self) -> None:
        with pytest.raises(ValidationError):
            RoutingDecision(agent_id="scribe")  # type: ignore[call-arg]

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
            decision.agent_id = "scribe"  # type: ignore[misc]


class TestAgentIdValidation:
    """The schema enforces the agent_id regex on the chosen id — same
    constraint the bridge enforces on user-defined agents. Moving
    this to the type boundary catches malformed LLM output at parse
    time, before the fan-out tries to act on it."""

    def test_valid_id_accepted(self) -> None:
        decision = RoutingDecision(
            agent_id="agent-1",
            reasoning="valid",
        )
        assert decision.agent_id == "agent-1"

    def test_underscored_id_accepted(self) -> None:
        decision = RoutingDecision(agent_id="agent_2", reasoning="x")
        assert decision.agent_id == "agent_2"

    def test_uppercase_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agent_id="Scribe", reasoning="x")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agent_id="", reasoning="x")

    def test_special_characters_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agent_id="scribe!", reasoning="x")

    def test_over_32_chars_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must match"):
            RoutingDecision(agent_id="x" * 33, reasoning="x")

    def test_at_32_chars_accepted(self) -> None:
        decision = RoutingDecision(agent_id="x" * 32, reasoning="x")
        assert decision.agent_id == "x" * 32


class TestSingleAgentEnforcement:
    """The schema field is a single scalar ``str | None`` (not a
    list/tuple), so a misbehaving LLM cannot fan out to multiple agents
    even by accident. This pins that contract at the type boundary."""

    def test_list_input_rejected(self) -> None:
        """Passing a list rather than a string fails validation —
        pydantic will not silently coerce ``["scribe", "conan"]`` into
        anything sensible for a ``str`` field."""
        with pytest.raises(ValidationError):
            RoutingDecision(agent_id=["scribe", "conan"], reasoning="x")  # type: ignore[arg-type]


class TestToolNameConstant:
    """``ROUTER_OUTPUT_TOOL_NAME`` ties the schema to the prompt and
    the factory's ``ToolOutput`` wiring. It's intentionally a
    one-line constant so both producers (factory) and the prompt
    template can import it."""

    def test_default_value(self) -> None:
        assert ROUTER_OUTPUT_TOOL_NAME == "dispatch"
