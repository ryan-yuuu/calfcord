"""Unit tests for :func:`build_router_temp_instructions`.

The router's roster is different from the peer roster
(:mod:`calfkit_organization.agents.peer_roster`):

* No gating on ``private_chat`` — the router has no tools at all, so
  every agent is a candidate respondent regardless of which tools it
  declares.
* The router itself is excluded from the output so the LLM cannot
  pick its own id.
"""

from __future__ import annotations

import logging

import pytest

from calfkit_organization.agents.phonebook import PhonebookEntry
from calfkit_organization.router.definition import ROUTER_AGENT_ID
from calfkit_organization.router.roster import build_router_temp_instructions


def _entry(
    agent_id: str,
    *,
    description: str = "test description",
    tools: tuple[str, ...] = (),
) -> PhonebookEntry:
    return PhonebookEntry(
        agent_id=agent_id,
        display_name=agent_id.title(),
        description=description,
        tools=tools,
    )


class TestBuildRouterTempInstructions:
    def test_empty_phonebook_returns_none(self) -> None:
        """No agents to route to → nothing meaningful to inject. An
        empty roster string would be worse than ``None`` — it implies
        "there is a roster, but it's empty" and burns tokens for no
        signal."""
        assert build_router_temp_instructions([]) is None

    def test_empty_phonebook_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No eligible respondents is a deployment-misconfiguration
        signal (no assistants registered); WARN connects cause to
        symptom (silent router)."""
        with caplog.at_level(
            logging.WARNING, logger="calfkit_organization.router.roster"
        ):
            build_router_temp_instructions([])
        assert any(
            "no eligible respondents" in r.message for r in caplog.records
        )

    def test_phonebook_with_only_router_returns_none(self) -> None:
        """The router never routes to itself; after filtering, an
        all-router phonebook is empty."""
        phonebook = [_entry(ROUTER_AGENT_ID, description="Internal routing agent")]
        assert build_router_temp_instructions(phonebook) is None

    def test_phonebook_with_only_router_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After filtering the router itself, this is functionally an
        empty roster — same WARN as the truly-empty case."""
        phonebook = [_entry(ROUTER_AGENT_ID, description="Internal routing agent")]
        with caplog.at_level(
            logging.WARNING, logger="calfkit_organization.router.roster"
        ):
            build_router_temp_instructions(phonebook)
        assert any(
            "no eligible respondents" in r.message for r in caplog.records
        )

    def test_excludes_router_from_roster(self) -> None:
        """When the registry includes the router (which Phase 3 wires
        in automatically), the roster builder filters it out."""
        phonebook = [
            _entry(ROUTER_AGENT_ID, description="Internal routing agent"),
            _entry("scribe", description="Note-taking agent."),
        ]
        result = build_router_temp_instructions(phonebook)
        assert result is not None
        assert ROUTER_AGENT_ID not in result
        assert "Internal routing agent" not in result
        assert "scribe" in result

    def test_lists_all_agents_regardless_of_tools(self) -> None:
        """The router has no private_chat tool, so unlike the peer
        roster we do NOT filter on the target's tools (the target is
        the router itself — and we exclude it anyway). Every other
        agent appears regardless of which tools it carries."""
        phonebook = [
            _entry("alice", description="Bot A.", tools=()),
            _entry("bob", description="Bot B.", tools=("calendar",)),
            _entry("carol", description="Bot C.", tools=("private_chat",)),
        ]
        result = build_router_temp_instructions(phonebook)
        assert result is not None
        assert "alice" in result
        assert "bob" in result
        assert "carol" in result

    def test_format_includes_agent_id_and_description(self) -> None:
        """The roster lines follow ``- <agent_id>: <description>`` so
        the LLM can pattern-match on the format described in the
        system prompt."""
        phonebook = [
            _entry("scribe", description="Note-taking agent for the team."),
        ]
        result = build_router_temp_instructions(phonebook)
        assert result is not None
        assert "- scribe: Note-taking agent for the team." in result

    def test_header_mentions_routing(self) -> None:
        """The instruction header gives the LLM context for the
        attached list; the system prompt cross-references it."""
        phonebook = [_entry("scribe")]
        result = build_router_temp_instructions(phonebook)
        assert result is not None
        assert "Available agents" in result

    def test_preserves_phonebook_order(self) -> None:
        """Roster ordering follows the input phonebook so a deterministic
        registry produces a deterministic roster — useful for golden tests
        and operator debugging."""
        phonebook = [
            _entry("delta"),
            _entry("alpha"),
            _entry("charlie"),
        ]
        result = build_router_temp_instructions(phonebook)
        assert result is not None
        delta_pos = result.index("delta")
        alpha_pos = result.index("alpha")
        charlie_pos = result.index("charlie")
        assert delta_pos < alpha_pos < charlie_pos
