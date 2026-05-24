"""Unit tests for the per-invocation peer-roster builder."""

from __future__ import annotations

from calfkit_organization.agents.peer_roster import build_temp_instructions
from calfkit_organization.agents.phonebook import PhonebookEntry


def _entry(
    agent_id: str,
    *,
    description: str = "test",
    tools: tuple[str, ...] = (),
) -> PhonebookEntry:
    return PhonebookEntry(
        agent_id=agent_id,
        display_name=agent_id.title(),
        description=description,
        tools=tools,
    )


class TestBuildTempInstructionsA2A:
    """A2A context (channel=False): roster only, gated on the target
    having the ``private_chat`` tool — the tool's docstring carries
    everything else, and the roster alone is meaningless without the
    tool to call it."""

    def test_returns_none_when_target_lacks_private_chat(self) -> None:
        """No A2A tool → no need to advertise peers; save the tokens."""
        phonebook = [_entry("alice", tools=()), _entry("bob", tools=())]
        assert build_temp_instructions(phonebook, "alice", channel=False) is None

    def test_returns_none_when_target_not_in_phonebook(self) -> None:
        """Unknown target — nothing meaningful to say. Caller will hit
        its own error path elsewhere."""
        phonebook = [_entry("alice", tools=("private_chat",))]
        assert build_temp_instructions(phonebook, "ghost", channel=False) is None

    def test_returns_none_when_no_peers_after_excluding_target(self) -> None:
        """A lone agent with private_chat has no one to call. Still
        return None — an empty roster string would be worse than nothing
        (it implies "there is a roster, it's empty")."""
        phonebook = [_entry("alice", tools=("private_chat",))]
        assert build_temp_instructions(phonebook, "alice", channel=False) is None

    def test_lists_peers_with_descriptions_and_excludes_target(self) -> None:
        phonebook = [
            _entry("alice", description="Scheduler bot.", tools=("private_chat",)),
            _entry("bob", description="Note-taker."),
            _entry("carol", description="Researcher.", tools=("private_chat",)),
        ]
        result = build_temp_instructions(phonebook, "alice", channel=False)
        assert result is not None
        assert "alice" not in result  # excluded as the target
        assert "bob: Note-taker." in result
        assert "carol: Researcher." in result

    def test_intro_line_names_the_tool(self) -> None:
        """The intro labels the roster as the set of ``private_chat``
        targets so the LLM knows the connection between this list and
        the tool available in its schema. Without that link the roster
        is ambiguous (could be channel members, audit recipients, etc.)."""
        phonebook = [_entry("alice", tools=("private_chat",)), _entry("bob")]
        result = build_temp_instructions(phonebook, "alice", channel=False)
        assert result is not None
        assert "private_chat" in result

    def test_peers_with_other_tools_still_appear(self) -> None:
        """A peer's *own* tools don't gate visibility — only the target's
        do. A non-A2A peer is still a valid private_chat target as long
        as it's known, because A2A delivery uses the target's
        agent.{id}.in inbox (no tool needed on the receiving side)."""
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("calendar",)),
        ]
        result = build_temp_instructions(phonebook, "alice", channel=False)
        assert result is not None
        assert "bob" in result

    def test_does_not_include_mention_block(self) -> None:
        """A2A replies never re-enter the channel-normalizer flow, so
        @-mention prose would be dead text. Guarding this explicitly
        because a careless refactor that merged the two paths would
        silently inflate every A2A invocation's prompt by ~600 chars."""
        phonebook = [_entry("alice", tools=("private_chat",)), _entry("bob")]
        result = build_temp_instructions(phonebook, "alice", channel=False)
        assert result is not None
        assert "@<agent_id>" not in result


class TestBuildTempInstructionsChannel:
    """Channel context (channel=True): roster + @-mention rules.
    Tools-independent — the @-mention mechanism lives in the bridge
    normalizer, not in any tool, so every channel-invoked agent
    benefits from knowing it."""

    def test_returns_none_when_target_not_in_phonebook(self) -> None:
        phonebook = [_entry("alice"), _entry("bob")]
        assert build_temp_instructions(phonebook, "ghost", channel=True) is None

    def test_returns_none_when_no_peers(self) -> None:
        """Single-agent deployment — nothing to @-mention and no roster
        to advertise, so the whole block would be vacuous."""
        phonebook = [_entry("alice")]
        assert build_temp_instructions(phonebook, "alice", channel=True) is None

    def test_emitted_for_target_without_private_chat_tool(self) -> None:
        """The whole point of this context: an agent without any A2A
        tooling still gets the @-mention rules because the mechanism
        is bridge-level, not tool-gated."""
        phonebook = [_entry("alice", tools=()), _entry("bob", description="Note-taker.")]
        result = build_temp_instructions(phonebook, "alice", channel=True)
        assert result is not None
        assert "bob: Note-taker." in result
        assert "@<agent_id>" in result

    def test_emitted_for_target_with_private_chat_tool(self) -> None:
        """A2A-tooled agents in channel context get the SAME block as
        tool-less agents — the channel roster + mention rules are
        independent of A2A tooling. (The agent's private_chat tool
        docstring covers that path separately.)"""
        phonebook = [_entry("alice", tools=("private_chat",)), _entry("bob")]
        result = build_temp_instructions(phonebook, "alice", channel=True)
        assert result is not None
        assert "bob" in result
        assert "@<agent_id>" in result

    def test_excludes_target_from_roster(self) -> None:
        phonebook = [_entry("alice"), _entry("bob"), _entry("carol")]
        result = build_temp_instructions(phonebook, "alice", channel=True)
        assert result is not None
        assert "alice" not in result
        assert "bob" in result
        assert "carol" in result

    def test_mention_block_contains_load_bearing_rules(self) -> None:
        """The mention block is what teaches the LLM the feature.
        Spot-check the rules an inattentive edit would be most likely
        to drop: the whitespace-prefix requirement, case-insensitivity,
        the every-mention-validated rule, and the self-mention
        silent-drop warning. Pinned inline so the prose can't drift
        silently away from what the bridge normalizer + addressable
        gate actually enforce."""
        phonebook = [_entry("alice"), _entry("bob")]
        result = build_temp_instructions(phonebook, "alice", channel=True)
        assert result is not None
        assert "directly preceded by whitespace" in result
        assert "case-insensitive" in result
        assert "EVERY `@<name>` token" in result  # all-validated rule
        assert "@-mentioning yourself" in result  # silent-drop warning

    def test_mention_block_warns_against_referential_use(self) -> None:
        """The mention block must teach the LLM that `@<id>` is an
        INVOCATION verb, not a soft reference. Without this, agents
        trained on social-media corpora default to writing `@scribe`
        whenever they NAME the peer in a sentence, which silently
        spawns unintended agent-to-agent loops in shared channels.

        The verb/noun framing, the explicit CRITICAL marker, and at
        least one concrete WRONG/RIGHT example pair are pinned here
        so a future tightening of the prompt cannot silently drop
        the rule that motivated the prompt's existence."""
        phonebook = [_entry("alice"), _entry("bob")]
        result = build_temp_instructions(phonebook, "alice", channel=True)
        assert result is not None
        # The load-bearing framing.
        assert "INVOCATION verb" in result
        assert "NOT a soft reference" in result
        # Both halves of the noun-vs-verb distinction.
        assert "plain name is a noun" in result
        assert "`@name` is a verb" in result
        # Concrete WRONG / RIGHT example scaffolding so the LLM sees
        # contrasting patterns rather than abstract rules alone.
        assert "WRONG" in result
        assert "RIGHT" in result
        # The consequence is named so the LLM can reason about why
        # the rule matters at edge cases not covered by the examples.
        assert "back-and-forth" in result or "unintended invocations" in result

    def test_returns_none_when_target_is_filtered_out_of_phonebook(self) -> None:
        """Defense-in-depth for the router-exclusion invariant. The
        production phonebook (built via
        :func:`~calfkit_organization.agents.phonebook.phonebook_from_registry`)
        filters out the router agent, and the bridge normalizer rejects
        ``@<router_id>`` as an unknown mention — so a router target
        should never reach this helper. But if a future regression
        lets one through, the function must return ``None`` (no
        mention block leaks to the router LLM) rather than silently
        producing instructions without the target's own entry to
        anchor on. Simulated here by passing a target absent from the
        phonebook entirely."""
        phonebook = [_entry("alice"), _entry("bob")]
        # "_router" is the canonical filtered-out target; any id missing
        # from the phonebook exercises the same guard.
        assert build_temp_instructions(phonebook, "_router", channel=True) is None

    def test_does_not_restate_private_chat_tool_docstring(self) -> None:
        """The private_chat tool docstring is delivered to the LLM as
        part of the tool schema — restating ``<thread_id>`` semantics
        here would be duplicate prose. Guarded so a regression that
        re-introduces the old A2A trailing paragraph fails loudly."""
        phonebook = [_entry("alice", tools=("private_chat",)), _entry("bob")]
        result = build_temp_instructions(phonebook, "alice", channel=True)
        assert result is not None
        assert "<thread_id>" not in result
