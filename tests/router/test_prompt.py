"""Tests pinning the router system prompt to its schema and tool name.

The router's :data:`SYSTEM_PROMPT` references the LLM-facing tool name
(:data:`ROUTER_OUTPUT_TOOL_NAME`) and the
:class:`RoutingDecision` schema field names. The prompt module already
asserts the field names match at import time
(:mod:`calfkit_organization.router.prompt` lines 36-43), which catches
the most egregious mismatch — a typo in the prompt's hardcoded literal
versus the schema's field name. These tests cover the symmetric
direction (schema rename without prompt update) and pin one specific
wording change that fixed a misleading rule-5 phrase.

A failure here means one of:

* Someone renamed :data:`ROUTER_OUTPUT_TOOL_NAME` without updating the
  prompt to interpolate the new value.
* Someone renamed a :class:`RoutingDecision` field (e.g. ``agents`` →
  ``respondents``) without updating the prompt to instruct the LLM
  using the new name. The LLM would still emit a tool call but with
  wrong argument names; pydantic-ai's parser would reject it and the
  router would silently fail to fan out.
* Someone reinstated the rewritten "silently dropped downstream" rule-5
  language. That phrasing implied the bridge had a per-message drop
  step which it does not — the actual failure mode is no fan-out, so
  no synthesized invocation, so no agent reply (the message goes
  unanswered, not "dropped").
"""

from __future__ import annotations

from calfkit_organization.agents.routing import (
    ROUTER_OUTPUT_TOOL_NAME,
    RoutingDecision,
)
from calfkit_organization.router.prompt import SYSTEM_PROMPT


class TestSystemPromptCoupling:
    def test_prompt_contains_tool_name_literal(self) -> None:
        """The prompt instructs the LLM to call the structured-output
        tool by name. Pin the literal value (not the constant
        reference) so a rename of :data:`ROUTER_OUTPUT_TOOL_NAME`
        without updating the prompt's instruction text is caught here.

        The prompt module interpolates the constant via f-string at
        module load, so a rename without a coordinated update would
        propagate — this test serves as a contract anchor in case the
        f-string is ever inlined as a literal."""
        assert ROUTER_OUTPUT_TOOL_NAME in SYSTEM_PROMPT, (
            f"prompt missing tool name {ROUTER_OUTPUT_TOOL_NAME!r}; "
            f"if ROUTER_OUTPUT_TOOL_NAME was renamed, update "
            f"router/prompt.py to interpolate the new value"
        )
        # The current canonical value — kept in sync with
        # agents/routing.py. A change here forces a coordinated review
        # of the prompt text (the LLM's instructions reference this
        # string by name).
        assert ROUTER_OUTPUT_TOOL_NAME == "dispatch"

    def test_prompt_contains_every_routing_decision_field(self) -> None:
        """Every :class:`RoutingDecision` field name must appear in the
        prompt so the LLM is instructed to populate it.

        Without this check, renaming ``agents`` → ``respondents`` on
        the schema would only fail the import-time assertion if the
        prompt module's ``_AGENTS_FIELD`` constant was also updated;
        if a contributor renamed the schema field AND the prompt
        constant but forgot to update the prompt text's prose (which
        currently uses backticked field names like ``agents``), the
        LLM would receive instructions referencing a nonexistent
        field name. This test catches that schema-prompt drift."""
        missing = [
            field_name
            for field_name in RoutingDecision.model_fields
            if field_name not in SYSTEM_PROMPT
        ]
        assert not missing, (
            f"RoutingDecision fields {missing!r} are not referenced in "
            f"SYSTEM_PROMPT; rename or remove the field in the schema "
            f"and update router/prompt.py to match"
        )

    def test_prompt_does_not_contain_misleading_drop_phrase(self) -> None:
        """Rule 5 was rewritten to remove the misleading "silently
        dropped downstream" phrase.

        Earlier prompt text implied the bridge actively dropped
        messages targeting unknown agents. That's not what happens —
        :func:`router.fanout` publishes a synthesized wire to
        ``bridge.synthesized.in`` regardless of whether the target
        exists, the bridge republishes it to the channel topic, and
        no assistant accepts it (because no agent's
        ``addressed_to_me_gate`` matches the unknown ``slash_target``).
        The end result is the same — no reply — but the failure mode
        is "no agent picks it up", not "the message is dropped".

        The rewritten language ("the targeted agent will not exist and
        the message will go unanswered") is more accurate and helps
        the LLM understand the consequence: pick from the roster, not
        invent ids."""
        assert "silently dropped downstream" not in SYSTEM_PROMPT, (
            "rule 5 used to say invalid agent ids would be 'silently "
            "dropped downstream' — that was rewritten to 'the targeted "
            "agent will not exist and the message will go unanswered' "
            "because the bridge does NOT drop synthesized wires; the "
            "agents simply don't pick them up. Restore the rewording."
        )
        # Positive anchor for the corrected wording — a future
        # rewrite that loses this guidance should fail this test
        # rather than silently regress the LLM's understanding of
        # rule 5.
        assert "go unanswered" in SYSTEM_PROMPT, (
            "rule 5 should describe the failure consequence as the "
            "message going unanswered; the current wording 'the "
            "targeted agent will not exist and the message will go "
            "unanswered' was lost in a later edit"
        )
