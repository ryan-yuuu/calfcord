"""Routing-decision schema — the router agent's structured output.

The built-in router agent's LLM emits exactly one
``{ROUTER_OUTPUT_TOOL_NAME}(...)`` tool call via pydantic-ai's
:class:`~pydantic_ai.output.ToolOutput` pattern, which terminates the
agent loop without running a tool body. The tool's args are parsed
against this model and surfaced as ``NodeResult.output``. The
router's fan-out consumer (in the ``calfkit-router`` process) reads
the decision from there.

An empty ``agents`` list is the silent-ignore case (small talk, off-topic
asides, none of the listed agents are a natural respondent). Multi-entry
lists fan out to each agent in parallel — order is presentational only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from calfkit_organization.agents.identifier import AGENT_ID_PATTERN

ROUTER_OUTPUT_TOOL_NAME = "dispatch"
"""Name of the pseudo-tool the router's LLM emits via pydantic-ai's
:class:`ToolOutput` pattern. The factory wires
``final_output_type=ToolOutput(RoutingDecision, name=...)`` with this
value, and the system prompt references it by name when instructing
the LLM. Both sites import this constant; renaming the tool is a
one-edit change."""

_AGENTS_MAX_LENGTH = 16
"""Cap on the fan-out width per ambient message. A misbehaving LLM
could otherwise emit a 100+ agent list that we'd fan out to 100+
synthesized publishes. 16 is well above the realistic group size for
a Discord groupchat and large enough that legitimate fan-outs never
hit it."""


class RoutingDecision(BaseModel):
    """Structured output the router emits to indicate fan-out targets.

    ``agents`` is parsed by the router's downstream fan-out consumer.
    ``reasoning`` is operator-side logging only — never posted to
    Discord.

    Validation moves four cross-system invariants to the type
    boundary (rather than the consumer):

    * Each ``agent_id`` must match ``[a-z0-9_-]{1,32}`` — same regex
      the bridge enforces on user-defined agents. An invalid id is a
      malformed LLM output, not a registry lookup miss, and should
      fail at parse time so the fan-out never sees it.
    * ``agents`` is deduplicated preserving first-seen order. A
      misbehaving LLM emitting ``["scribe", "scribe", "conan"]``
      collapses to ``("scribe", "conan")`` — without this, the
      fan-out would publish twice and the user would see a duplicate
      reply from scribe.
    * ``agents`` is bounded to :data:`_AGENTS_MAX_LENGTH` entries.
    * ``reasoning`` is bounded to 1–2000 chars (was the only
      invariant pre-existing — a misbehaving model could emit a
      multi-kilobyte rationale that floods log files).

    The tuple type for ``agents`` (rather than ``list``) is
    independently load-bearing: ``frozen=True`` only freezes
    attribute assignment, not the internal sequence, so a ``list``
    would remain mutable in place.

    Two invariants intentionally stay at the consumer rather than
    here, because both depend on runtime state the schema cannot
    see:

    * Router self-reference (``agents`` containing the router's own
      id) — the fan-out consumer skips this id, since the runtime
      ``router_agent_id`` is closure-bound at consumer construction.
    * Phonebook membership (``agent_id`` exists in the current
      registry) — the fan-out validates each chosen id against the
      ``phonebook`` field of the publisher's
      :class:`~calfkit_organization._compat.invoke.MetadataEnvelope`
      and skips with an ERROR log on miss (catches LLM
      hallucinations and post-publish registry drift). See
      :func:`~calfkit_organization.router.fanout.build_fanout_consumer`.
    """

    model_config = ConfigDict(frozen=True)

    agents: tuple[str, ...] = Field(
        default_factory=tuple,
        max_length=_AGENTS_MAX_LENGTH,
        description=(
            "Agent ids that should respond to this ambient message. "
            "Empty tuple means no one should respond (silent ignore — "
            "appropriate when the message is small talk, an aside, "
            "or none of the available agents are a good match)."
        ),
    )
    reasoning: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Short explanation of the routing choice. Used for "
            "operator-side logging and debugging only — never posted "
            "to Discord."
        ),
    )

    @field_validator("agents")
    @classmethod
    def _validate_agent_ids_and_dedupe(
        cls, v: tuple[str, ...]
    ) -> tuple[str, ...]:
        for agent_id in v:
            if not AGENT_ID_PATTERN.fullmatch(agent_id):
                raise ValueError(
                    f"agent id {agent_id!r} must match "
                    f"[a-z0-9_-]{{1,32}}"
                )
        # First-seen dedupe preserves the LLM's intended order while
        # eliminating accidental repeats.
        seen: set[str] = set()
        out: list[str] = []
        for agent_id in v:
            if agent_id not in seen:
                seen.add(agent_id)
                out.append(agent_id)
        return tuple(out)

    @field_validator("reasoning")
    @classmethod
    def _validate_reasoning_non_whitespace(cls, v: str) -> str:
        # ``Field(min_length=1)`` accepts a single space; the field's
        # documented purpose is operator-side debugging, so a
        # whitespace-only rationale is useless log noise. Mirror the
        # ``AgentDefinition._validate_system_prompt`` shape.
        if not v.strip():
            raise ValueError("reasoning must contain non-whitespace content")
        return v
