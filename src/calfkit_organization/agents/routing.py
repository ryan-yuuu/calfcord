"""Routing-decision schema — the router agent's structured output.

The built-in router agent's LLM emits exactly one
``{ROUTER_OUTPUT_TOOL_NAME}(...)`` tool call via pydantic-ai's
:class:`~pydantic_ai.output.ToolOutput` pattern, which terminates the
agent loop without running a tool body. The tool's args are parsed
against this model and surfaced as ``NodeResult.output``. The
router's fan-out consumer (in the ``calfkit-router`` process) reads
the decision from there.

The router's system prompt instructs the LLM to pick exactly one agent —
the addressee of the ambient message. Every ambient message in the
groupchat goes to a single respondent; cross-agent collaboration is
handled out-of-band by that respondent via its ``private_chat`` tool,
not by fan-out from the router.

The schema does NOT structurally require ``agent_id``: a misbehaving
LLM that emits a tool call with no ``agent_id`` (or with ``None``)
falls through to the fan-out consumer's defensive no-op path (logs
and skips) rather than triggering pydantic-ai structured-output
validation retries in production. The field's optionality is
defense-in-depth; the system prompt is the always-route policy's
enforcement surface.
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


class RoutingDecision(BaseModel):
    """Structured output the router emits to indicate the addressee.

    ``agent_id`` is parsed by the router's downstream fan-out consumer,
    which synthesizes exactly one slash wire targeting that agent.
    ``reasoning`` is operator-side logging only — never posted to
    Discord.

    Validation moves three cross-system invariants to the type
    boundary (rather than the consumer):

    * ``agent_id`` (when non-None) must match ``[a-z0-9_-]{1,32}`` —
      same regex the bridge enforces on user-defined agents. An
      invalid id is a malformed LLM output, not a registry lookup
      miss, and should fail at parse time so the fan-out never sees
      it.
    * ``agent_id`` is a single string (not a list/tuple) so a
      misbehaving LLM cannot fan out to multiple agents even by
      accident — the schema enforces "exactly one addressee" at the
      type boundary.
    * ``reasoning`` is bounded to 1-2000 chars (a misbehaving model
      could otherwise emit a multi-kilobyte rationale that floods
      log files).

    Two invariants intentionally stay at the consumer rather than
    here, because both depend on runtime state the schema cannot
    see:

    * Router self-reference (``agent_id`` equal to the router's own
      id) — the fan-out consumer skips this id, since the runtime
      ``router_agent_id`` is closure-bound at consumer construction.
    * Phonebook membership (``agent_id`` exists in the current
      registry) — the fan-out validates the chosen id against the
      ``phonebook`` field of the publisher's
      :class:`~calfkit_organization._compat.invoke.MetadataEnvelope`
      and skips with an ERROR log on miss (catches LLM
      hallucinations and post-publish registry drift). See
      :func:`~calfkit_organization.router.fanout.build_fanout_consumer`.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str | None = Field(
        default=None,
        description=(
            "The agent_id of the single agent the user is addressing. "
            "Pick exactly one — the addressee. If that agent needs "
            "input from peers, it can pull them in out-of-band via "
            "its ``private_chat`` tool; the router does NOT fan out "
            "to multiple agents. When no agent is a strong match, "
            "pick the agent whose persona best fits the social "
            "register of the message rather than returning None."
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

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not AGENT_ID_PATTERN.fullmatch(v):
            raise ValueError(f"agent_id {v!r} must match [a-z0-9_-]{{1,32}}")
        return v

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
