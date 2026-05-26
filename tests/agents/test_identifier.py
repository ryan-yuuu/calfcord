"""Pin the leaf module's contract independent of any consumer.

The four duplication sites
(:class:`AgentDefinition.agent_id`, :class:`PhonebookEntry.agent_id`,
:class:`RoutingDecision.agent_id`, the bridge normalizer's mention scanner)
all import from this module, so the regex can't drift between them.
These tests pin the regex itself.
"""

from __future__ import annotations

import pytest

from calfkit_organization.agents.identifier import (
    AGENT_ID_CHARSET,
    AGENT_ID_PATTERN,
)


class TestAgentIdPattern:
    @pytest.mark.parametrize(
        "value",
        ["scribe", "agent-1", "agent_2", "a", "x" * 32, "0", "a-b_c"],
    )
    def test_valid_ids_match(self, value: str) -> None:
        assert AGENT_ID_PATTERN.fullmatch(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "",  # empty
            "Scribe",  # uppercase
            "x" * 33,  # over 32 chars
            "agent.id",  # dot not allowed
            "agent id",  # space not allowed
            "agent!",  # special char
            "@scribe",  # @ not part of charset
        ],
    )
    def test_invalid_ids_reject(self, value: str) -> None:
        assert AGENT_ID_PATTERN.fullmatch(value) is None


class TestAgentIdCharset:
    def test_charset_constant_matches_validator_charset(self) -> None:
        # The normalizer builds its mention regex from this constant.
        # Pinning it guards against silent drift in the leaf module.
        assert AGENT_ID_CHARSET == "a-z0-9_-"
