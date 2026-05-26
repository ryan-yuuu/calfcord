"""Unit tests for the shared addressable / addressed-to-me gate factories.

The gates read ``ctx.deps.provided_deps["discord"]`` (a serialized
:class:`~calfkit_organization.bridge.wire.WireMessage`) and return ``bool``.
Tests fabricate a minimal :class:`SessionRunContext` directly — no Kafka,
no real envelope flow — because gates are pure predicates over ``ctx``.
"""

from __future__ import annotations

from typing import Any

import pytest
from calfkit.models import State
from calfkit.models.session_context import Deps, SessionRunContext

from calfkit_organization.agents.gates import (
    make_addressable_gate,
    make_addressed_to_me_gate,
)


def _ctx(discord: Any) -> SessionRunContext:
    """Build a minimal SessionRunContext with the given ``discord`` dep value.

    ``discord=None`` simulates a missing dep entirely (the dict key is absent).
    ``discord={...}`` simulates a serialized WireMessage.
    """
    deps_dict: dict[str, Any] = {}
    if discord is not None:
        deps_dict["discord"] = discord
    return SessionRunContext(
        state=State(),
        deps=Deps(correlation_id="corr-test", provided_deps=deps_dict),
    )


def _wire(
    *,
    kind: str = "message",
    slash_target: str | None = None,
    author_agent_id: str | None = None,
    author_is_bot: bool = False,
) -> dict[str, Any]:
    """Build a minimal serialized WireMessage dict for gate testing.

    Only fields the gates read are populated; other fields are omitted.
    """
    return {
        "kind": kind,
        "slash_target": slash_target,
        "author": {
            "agent_id": author_agent_id,
            "is_bot": author_is_bot,
        },
    }


class TestAddressableGate:
    def test_accepts_human_message(self) -> None:
        gate = make_addressable_gate("scheduler")
        assert gate(_ctx(_wire())) is True

    def test_rejects_own_persona(self) -> None:
        """Prevents self-reply loops: the agent's own webhook posts re-enter
        the ingress topic with author.agent_id resolved via display-name."""
        gate = make_addressable_gate("scheduler")
        assert gate(_ctx(_wire(author_agent_id="scheduler"))) is False

    def test_accepts_peer_agent_message(self) -> None:
        """Other agents' messages flow through — they're recognized personas
        (agent_id is set), not anonymous bots."""
        gate = make_addressable_gate("scheduler")
        assert gate(_ctx(_wire(author_agent_id="finance"))) is True

    def test_rejects_unknown_bot(self) -> None:
        """Third-party bots in the guild that aren't registered agents."""
        gate = make_addressable_gate("scheduler")
        assert gate(_ctx(_wire(author_is_bot=True, author_agent_id=None))) is False

    def test_rejects_missing_discord_dep(self) -> None:
        """Events without the bridge's discord dep are not actionable."""
        gate = make_addressable_gate("scheduler")
        assert gate(_ctx(None)) is False

    def test_rejects_non_dict_discord_dep(self) -> None:
        """Defensive: a malformed dep (e.g. string) is rejected, not crashed."""
        gate = make_addressable_gate("scheduler")
        assert gate(_ctx("not a dict")) is False

    def test_name_includes_agent_id(self) -> None:
        """__name__ is used by calfkit's gate logger; make it identifiable."""
        gate = make_addressable_gate("scheduler")
        assert gate.__name__ == "addressable_scheduler"


class TestAddressedToMeGate:
    def test_rejects_ambient_message_from_human(self) -> None:
        """Ambient (kind=message) traffic from humans no longer reaches
        assistants directly. The bridge ingress routes ambient envelopes to
        ``discord.ambient.in`` (the router's exclusive ingress), and the
        router decides which assistants get a synthesized slash. Any raw
        ``kind="message"`` arriving on a channel topic is a topology
        regression and must be rejected — otherwise an ambient that
        somehow bypassed the router would fan out to every co-tenant
        agent, which is precisely the anti-pattern the routing feature
        was introduced to eliminate."""
        gate = make_addressed_to_me_gate("scheduler")
        assert gate(_ctx(_wire(kind="message"))) is False

    def test_rejects_ambient_message_from_peer_agent(self) -> None:
        """Same as above for peer-agent ambient: rejected because
        ``kind != "slash"``. Note the rejection reason has shifted from
        "peer agent author" (the pre-routing semantic) to "non-slash
        envelope" (the new contract). The end result is the same: no
        agent-to-agent reply storms."""
        gate = make_addressed_to_me_gate("scheduler")
        wire = _wire(kind="message", author_agent_id="finance")
        assert gate(_ctx(wire)) is False

    def test_accepts_slash_for_self(self) -> None:
        gate = make_addressed_to_me_gate("scheduler")
        wire = _wire(kind="slash", slash_target="scheduler")
        assert gate(_ctx(wire)) is True

    def test_accepts_slash_from_peer_agent_addressed_to_me(self) -> None:
        """A2A regression check: ``private_chat`` synthesizes a wire with
        ``kind="slash"`` and ``slash_target`` pointing at this agent (see
        ``tools/builtin/private_chat.py``'s ``model_copy(update=...)`` block); the
        author remains the original Discord author (which may be a peer
        agent's persona). This path must continue to be accepted — A2A
        does not depend on the removed ambient branch."""
        gate = make_addressed_to_me_gate("scheduler")
        wire = _wire(
            kind="slash", slash_target="scheduler", author_agent_id="finance"
        )
        assert gate(_ctx(wire)) is True

    def test_accepts_synthesized_slash_from_router_fanout(self) -> None:
        """Router fan-out regression check: when the router selects this
        agent for an ambient, the fan-out @consumer synthesizes a fresh
        ``kind="slash"`` wire with ``slash_target=agent_id`` and the
        bridge's synthesized-in consumer feeds it back through
        ``BridgeIngress.handle()`` onto the channel topic. The wire's
        author is the original human (``agent_id`` unset). This path is
        the primary new acceptance case introduced by the routing-agent
        feature; if it ever stops matching, ambient → router → assistant
        is silently broken."""
        gate = make_addressed_to_me_gate("scheduler")
        wire = _wire(
            kind="slash",
            slash_target="scheduler",
            author_agent_id=None,
            author_is_bot=False,
        )
        assert gate(_ctx(wire)) is True

    def test_rejects_slash_for_another_agent(self) -> None:
        gate = make_addressed_to_me_gate("scheduler")
        wire = _wire(kind="slash", slash_target="finance")
        assert gate(_ctx(wire)) is False

    def test_rejects_slash_with_no_target(self) -> None:
        """Defensive: malformed slash with slash_target=None doesn't match anyone."""
        gate = make_addressed_to_me_gate("scheduler")
        wire = _wire(kind="slash", slash_target=None)
        assert gate(_ctx(wire)) is False

    def test_rejects_missing_discord_dep(self) -> None:
        gate = make_addressed_to_me_gate("scheduler")
        assert gate(_ctx(None)) is False

    def test_rejects_non_dict_discord_dep(self) -> None:
        """Defensive: malformed dep (e.g. string) is rejected, not crashed."""
        gate = make_addressed_to_me_gate("scheduler")
        assert gate(_ctx("not a dict")) is False

    def test_name_includes_agent_id(self) -> None:
        gate = make_addressed_to_me_gate("scheduler")
        assert gate.__name__ == "addressed_to_me_scheduler"


class TestGateComposition:
    """Sanity check: when both gates are registered AND-stacked, the
    intersection of accepts is what we expect for the canonical cases.

    Post-routing-agent contract: only ``kind="slash"`` envelopes with
    matching ``slash_target`` survive both gates. Every ambient case is
    rejected — the routing decision happens upstream at the router."""

    @pytest.mark.parametrize(
        ("kind", "slash_target", "author_agent_id", "author_is_bot", "expected"),
        [
            # Human posts a non-slash message: addressable accepts, but the
            # tightened addressed_to_me rejects — ambient is router-only now.
            ("message", None, None, False, False),
            # Human posts `/scheduler hello`: both accept.
            ("slash", "scheduler", None, False, True),
            # Human posts `/finance hello`: addressable accepts, addressed_to_me rejects.
            ("slash", "finance", None, False, False),
            # Scheduler's own webhook message comes back: addressable rejects.
            ("message", None, "scheduler", False, False),
            # Unknown third-party bot in the channel: addressable rejects.
            ("message", None, None, True, False),
            # Peer agent's ambient (non-addressed) post: addressable accepts,
            # addressed_to_me rejects on ``kind != "slash"``.
            ("message", None, "finance", False, False),
            # Peer agent explicitly @mentions me (or A2A via private_chat):
            # both accept.
            ("slash", "scheduler", "finance", False, True),
            # Router fan-out synthesized slash (human author, kind=slash,
            # slash_target=self): both accept.
            ("slash", "scheduler", None, False, True),
        ],
    )
    def test_and_stacking(
        self,
        kind: str,
        slash_target: str | None,
        author_agent_id: str | None,
        author_is_bot: bool,
        expected: bool,
    ) -> None:
        addressable = make_addressable_gate("scheduler")
        addressed = make_addressed_to_me_gate("scheduler")
        ctx = _ctx(
            _wire(
                kind=kind,
                slash_target=slash_target,
                author_agent_id=author_agent_id,
                author_is_bot=author_is_bot,
            )
        )
        result = addressable(ctx) and addressed(ctx)
        assert result is expected
