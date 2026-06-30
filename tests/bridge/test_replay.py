"""Tests for tool-call replay hydration (Phase 5).

On a new turn the bridge re-injects an agent's prior structured tool
calls/returns into its reconstructed ``message_history`` so the model
sees the tools it used — not just its final texts. This is bridge-side
only: the agent receives the already-hydrated history over Kafka.

Coverage (see
``docs/design/step-transcripts-and-live-streaming-plan.md`` §4, §7.6):

* :func:`project_history` splices a self-reply's persisted delta IMMEDIATELY
  BEFORE that reply's ``ModelResponse`` — and is byte-identical when
  ``hydration=None``.
* :meth:`BridgeIngress._build_slash_message_history` joins (never DB-scans)
  the fetched, ``/clear``-truncated records against the store, and the
  hydrated list it returns is exactly what the ``PendingEntry`` snapshots
  as ``initial_message_history_length`` (the consistency invariant).
* The router (``self_agent_id=None``) never sees tool calls.
* A self reply with no stored row, and a reply ``/clear`` truncated out of
  the records, are both never spliced.
* Oversized tool returns are truncated by their model-facing string —
  covering ``str``, non-``str`` (structured) and ``BuiltinToolReturnPart``
  content; short returns are left untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit._vendor.pydantic_ai.messages import (
    BuiltinToolReturnPart,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from calfcord.agents.definition import AgentDefinition
from calfcord.bridge.history import HistoryRecord, project_history
from calfcord.bridge.ingress import (
    REPLAY_TOOL_RETURN_MAX_CHARS,
    BridgeIngress,
    _truncate_replay_tool_returns,
)
from calfcord.bridge.pending_wires import PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.transcripts import TranscriptRow, TranscriptStore
from calfcord.bridge.wire import WireAuthor, WireMessage

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _record(
    *,
    message_id: int = 1,
    content: str = "hi",
    author_display_name: str = "ryan",
    author_agent_id: str | None = None,
) -> HistoryRecord:
    return HistoryRecord(
        message_id=message_id,
        created_at=datetime.now(UTC),
        content=content,
        author_display_name=author_display_name,
        author_agent_id=author_agent_id,
    )


def _tool_delta(*, tool_name: str = "weather", content: str = "18C") -> list[ModelMessage]:
    """A two-message structured slice: one tool call + its return."""
    return [
        ModelResponse(
            parts=[
                TextPart(content="checking…"),
                ToolCallPart(tool_name=tool_name, args={"q": "Tokyo"}, tool_call_id="t1"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name=tool_name, content=content, tool_call_id="t1")]),
    ]


def _delta_json(delta: list[ModelMessage] | None = None) -> str:
    return ModelMessagesTypeAdapter.dump_json(delta or _tool_delta()).decode()


def _transcript_row(*, final_message_id: int, delta: list[ModelMessage] | None = None) -> TranscriptRow:
    return TranscriptRow(
        correlation_id=f"corr-{final_message_id}",
        conversation_key="6789",
        agent_id="scribe",
        final_message_id=str(final_message_id),
        delta_json=_delta_json(delta),
        created_at=1700000000,
    )


def _slash_wire(
    *,
    event_id: str = "evt-1",
    slash_target: str = "scribe",
    channel_id: int = 6789,
    source_channel_id: int | None = None,
    message_id: int = 99999,
) -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind="slash",
        slash_target=slash_target,
        message_id=message_id,
        channel_id=channel_id,
        source_channel_id=source_channel_id,
        guild_id=4242,
        content="and again?",
        author=WireAuthor(
            discord_user_id=111,
            display_name="ryan",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
            is_human_owner=True,
        ),
        created_at=datetime.now(UTC),
    )


def _registry(scribe_history_turns: int = 30) -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scribe",
                display_name="Scribe",
                description="Notes.",
                provider="openai",
                history_turns=scribe_history_turns,
                system_prompt="OpenAI scribe.",
            ),
        ]
    )


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    # calfkit 0.10.0: ``Client.send`` is fire-and-forget and returns the
    # correlation_id (a ``str``), not an awaitable reply handle.
    c.send = AsyncMock(side_effect=lambda *_a, **_kw: "corr-id")
    return c


@pytest.fixture
def pending_wires() -> PendingWires:
    return PendingWires()


def _fake_fetcher(records: list[HistoryRecord]) -> MagicMock:
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=lambda **_kw: list(records))
    return fetcher


def _fake_store(rows: dict[str, TranscriptRow]) -> MagicMock:
    """A store stub exposing the one batch method replay calls."""
    store = MagicMock(spec=TranscriptStore)
    store.get_by_final_message_ids = AsyncMock(side_effect=lambda ids: {i: rows[i] for i in ids if i in rows})
    return store


# ---------------------------------------------------------------------------
# project_history — splice semantics (pure function)
# ---------------------------------------------------------------------------


class TestProjectHistoryHydration:
    def test_splices_delta_before_self_response(self) -> None:
        """The delta lands IMMEDIATELY BEFORE the self reply's ModelResponse."""
        records = [
            _record(message_id=1, content="what's the weather?", author_display_name="ryan"),
            _record(
                message_id=2,
                content="It's 18C.",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        delta = _tool_delta()
        out = project_history(records, self_agent_id="scribe", hydration={2: delta})

        # ryan's request, then the spliced tool call + return, then the
        # final-answer ModelResponse — in order.
        assert len(out) == 4
        assert isinstance(out[0], ModelRequest)
        assert out[0].parts[0].content == "<ryan> what's the weather?"
        # Spliced delta (same objects, in order, right before the reply).
        assert out[1] is delta[0]
        assert out[2] is delta[1]
        # The reply's own final text comes LAST.
        assert isinstance(out[3], ModelResponse)
        assert out[3].parts[0].content == "It's 18C."

    def test_multi_row_hydration_pairs_each_delta_with_its_own_reply(self) -> None:
        """Two distinct self replies (ids 2 and 4) each carry their OWN
        persisted delta. Each delta must splice IMMEDIATELY BEFORE its own
        reply's ModelResponse — never cross-contaminate the other reply.
        """
        records = [
            _record(message_id=1, content="first question", author_display_name="ryan"),
            _record(
                message_id=2,
                content="first answer",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(message_id=3, content="second question", author_display_name="ryan"),
            _record(
                message_id=4,
                content="second answer",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        # Two DISTINCT deltas with different tool names so cross-pairing is
        # detectable by object identity AND by content.
        delta_a = _tool_delta(tool_name="alpha", content="A-result")
        delta_b = _tool_delta(tool_name="beta", content="B-result")
        out = project_history(
            records,
            self_agent_id="scribe",
            hydration={2: delta_a, 4: delta_b},
        )

        # req1, [delta_a call, delta_a return], reply2,
        # req3, [delta_b call, delta_b return], reply4 → 8 entries.
        assert len(out) == 8
        # First turn: ryan's request, then delta_a spliced before reply 2.
        assert isinstance(out[0], ModelRequest)
        assert out[0].parts[0].content == "<ryan> first question"
        assert out[1] is delta_a[0]
        assert out[2] is delta_a[1]
        assert isinstance(out[3], ModelResponse)
        assert out[3].parts[0].content == "first answer"
        # Second turn: ryan's request, then delta_b spliced before reply 4.
        assert isinstance(out[4], ModelRequest)
        assert out[4].parts[0].content == "<ryan> second question"
        assert out[5] is delta_b[0]
        assert out[6] is delta_b[1]
        assert isinstance(out[7], ModelResponse)
        assert out[7].parts[0].content == "second answer"
        # No cross-contamination: delta_a's objects never appear in the
        # second turn's slice, and delta_b's never in the first turn's.
        assert delta_b[0] not in out[1:3]
        assert delta_b[1] not in out[1:3]
        assert delta_a[0] not in out[5:7]
        assert delta_a[1] not in out[5:7]

    def test_none_hydration_is_byte_identical(self) -> None:
        """hydration=None reproduces the no-replay projection exactly."""
        records = [
            _record(message_id=1, content="hi", author_display_name="ryan"),
            _record(
                message_id=2,
                content="hey",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        baseline = project_history(records, self_agent_id="scribe")
        explicit_none = project_history(records, self_agent_id="scribe", hydration=None)
        assert len(baseline) == len(explicit_none) == 2
        for a, b in zip(baseline, explicit_none, strict=True):
            assert type(a) is type(b)
            assert a.parts[0].content == b.parts[0].content

    def test_missing_row_leaves_projection_unchanged(self) -> None:
        """A self reply whose message_id is absent from the map is not spliced."""
        records = [
            _record(message_id=1, content="q", author_display_name="ryan"),
            _record(
                message_id=2,
                content="a",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        # Hydration keyed on a DIFFERENT message id (no match).
        out = project_history(records, self_agent_id="scribe", hydration={999: _tool_delta()})
        assert len(out) == 2
        assert isinstance(out[1], ModelResponse)
        assert out[1].parts[0].content == "a"

    def test_router_pov_ignores_hydration(self) -> None:
        """self_agent_id=None never self-classifies, so nothing is spliced."""
        records = [
            _record(
                message_id=2,
                content="prior reply",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(message_id=3, content="hi", author_display_name="ryan"),
        ]
        out = project_history(records, self_agent_id=None, hydration={2: _tool_delta()})
        # Every entry is a ModelRequest; no ToolCallPart/ToolReturnPart anywhere.
        assert all(isinstance(m, ModelRequest) for m in out)
        for m in out:
            for p in m.parts:
                assert not isinstance(p, (ToolCallPart, ToolReturnPart))

    def test_only_self_records_are_hydrated(self) -> None:
        """A peer's reply id present in the map must NOT be spliced (peer is
        projected as a ModelRequest, never a self ModelResponse)."""
        records = [
            _record(message_id=1, content="kick off", author_display_name="ryan"),
            _record(
                message_id=2,
                content="peer reply",
                author_display_name="Conan",
                author_agent_id="conan",
            ),
        ]
        # message_id=2 is in the map but belongs to a PEER from scribe's POV.
        out = project_history(records, self_agent_id="scribe", hydration={2: _tool_delta()})
        assert len(out) == 2
        assert all(isinstance(m, ModelRequest) for m in out)

    def test_leading_self_reply_is_not_hydrated(self) -> None:
        """A self reply with NO preceding user request is a leading
        ModelResponse the trailing drop removes. Its delta must NOT splice —
        otherwise the leading tool-call ModelResponse is popped and an
        orphaned tool-return ModelRequest (a ``tool_result`` with no matching
        ``tool_use``) is stranded at the head, which providers 400 on.
        """
        records = [
            # Oldest record is the agent's OWN reply — nothing before it.
            _record(
                message_id=2,
                content="earlier answer",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
            _record(message_id=3, content="follow-up", author_display_name="ryan"),
        ]
        out = project_history(records, self_agent_id="scribe", hydration={2: _tool_delta()})
        # The leading self reply is dropped entirely; nothing is spliced.
        # What survives is ryan's request — and crucially the head is NOT an
        # orphaned tool-return ModelRequest.
        assert len(out) == 1
        assert isinstance(out[0], ModelRequest)
        assert out[0].parts[0].content == "<ryan> follow-up"
        for m in out:
            for p in m.parts:
                assert not isinstance(p, (ToolCallPart, ToolReturnPart))


# ---------------------------------------------------------------------------
# _build_slash_message_history — join + two-turn replay + consistency
# ---------------------------------------------------------------------------


class TestSlashReplay:
    async def test_two_turn_replay_splices_tools(self, client: MagicMock, pending_wires: PendingWires) -> None:
        """Seed a row for agent A's reply; on the next turn the tool call +
        return are spliced immediately before A's final-answer response."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        records = [
            _record(message_id=10, content="weather?", author_display_name="ryan"),
            _record(
                message_id=20,
                content="It's 18C.",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        ingress.set_fetcher(_fake_fetcher(records))
        ingress.set_transcript_store(_fake_store({"20": _transcript_row(final_message_id=20)}))

        await ingress.handle(_slash_wire(slash_target="scribe", message_id=30))

        kw = client.send.call_args.kwargs
        # Slash sends address their reply at the Discord outbox; no output_type.
        assert kw["reply_to"] == "discord.outbox"
        assert "output_type" not in kw
        message_history = kw["message_history"]
        # request, ModelResponse(tool call), ModelRequest(tool return), final reply.
        assert len(message_history) == 4
        assert isinstance(message_history[0], ModelRequest)
        # Spliced tool call.
        assert isinstance(message_history[1], ModelResponse)
        assert any(isinstance(p, ToolCallPart) for p in message_history[1].parts)
        # Spliced tool return.
        assert isinstance(message_history[2], ModelRequest)
        assert any(isinstance(p, ToolReturnPart) for p in message_history[2].parts)
        # Final answer text last.
        assert isinstance(message_history[3], ModelResponse)
        assert message_history[3].parts[0].content == "It's 18C."

    async def test_consistency_invariant_initial_length(self, client: MagicMock, pending_wires: PendingWires) -> None:
        """MANDATORY: the PendingEntry's initial_message_history_length equals
        len(message_history) after a HYDRATED slash build — the steps cursor
        and the snapshot must agree on the now-longer list."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        records = [
            _record(message_id=10, content="weather?", author_display_name="ryan"),
            _record(
                message_id=20,
                content="It's 18C.",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        ingress.set_fetcher(_fake_fetcher(records))
        ingress.set_transcript_store(_fake_store({"20": _transcript_row(final_message_id=20)}))

        wire = _slash_wire(event_id="evt-consist", slash_target="scribe", message_id=30)
        await ingress.handle(wire)

        message_history = client.send.call_args.kwargs["message_history"]
        entry = pending_wires.get("evt-consist")
        assert entry is not None
        assert entry.initial_message_history_length == len(message_history)
        # And it's the hydrated (longer) length, not the bare 2-record one.
        assert entry.initial_message_history_length == 4
        assert len(entry.message_history) == 4

    async def test_missing_row_no_splice(self, client: MagicMock, pending_wires: PendingWires) -> None:
        """A self reply record with no transcript row → projection unchanged."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        records = [
            _record(message_id=10, content="weather?", author_display_name="ryan"),
            _record(
                message_id=20,
                content="It's 18C.",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        ingress.set_fetcher(_fake_fetcher(records))
        ingress.set_transcript_store(_fake_store({}))  # no rows

        await ingress.handle(_slash_wire(slash_target="scribe", message_id=30))

        message_history = client.send.call_args.kwargs["message_history"]
        assert len(message_history) == 2  # just request + final reply, no splice
        assert not any(isinstance(p, (ToolCallPart, ToolReturnPart)) for m in message_history for p in m.parts)

    async def test_store_unset_degrades_to_no_replay(self, client: MagicMock, pending_wires: PendingWires) -> None:
        """Pre-ready window: no store injected → today's behavior, no crash."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        records = [
            _record(message_id=10, content="q", author_display_name="ryan"),
            _record(
                message_id=20,
                content="a",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        ingress.set_fetcher(_fake_fetcher(records))
        # Deliberately NOT calling set_transcript_store.

        await ingress.handle(_slash_wire(slash_target="scribe", message_id=30))

        message_history = client.send.call_args.kwargs["message_history"]
        assert len(message_history) == 2

    async def test_clear_truncated_reply_is_never_hydrated(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """A reply that /clear truncated OUT of the fetched records is absent
        from ``records``, so it is never even queried for hydration.

        The fetcher already applies the /clear boundary; here we model the
        post-truncation record set (the old reply with message_id=20 is gone)
        and assert the store is queried only for the SURVIVING self reply
        (message_id=40), never the truncated one."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        # Only the post-clear survivors: a new prompt + a new self reply.
        # The pre-clear reply (message_id=20) is NOT present.
        records = [
            _record(message_id=30, content="new question", author_display_name="ryan"),
            _record(
                message_id=40,
                content="new answer",
                author_display_name="Scribe",
                author_agent_id="scribe",
            ),
        ]
        store = _fake_store(
            {
                "20": _transcript_row(final_message_id=20),  # pre-clear (must NOT be read)
                "40": _transcript_row(final_message_id=40),
            }
        )
        ingress.set_fetcher(_fake_fetcher(records))
        ingress.set_transcript_store(store)

        await ingress.handle(_slash_wire(slash_target="scribe", message_id=50))

        # The join key list contains only the surviving self reply's id.
        queried_ids = store.get_by_final_message_ids.call_args.args[0]
        assert queried_ids == ["40"]
        assert "20" not in queried_ids


# ---------------------------------------------------------------------------
# _truncate_replay_tool_returns
# ---------------------------------------------------------------------------


class TestTruncateReplayToolReturns:
    def test_oversized_str_return_is_truncated(self) -> None:
        big = "x" * (REPLAY_TOOL_RETURN_MAX_CHARS + 500)
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content=big, tool_call_id="t1")])]
        out = _truncate_replay_tool_returns(delta)
        content = out[0].parts[0].content
        assert len(content) <= REPLAY_TOOL_RETURN_MAX_CHARS
        assert content.endswith("…(truncated)")
        # Original delta is untouched (immutable copy).
        assert delta[0].parts[0].content == big

    def test_short_str_return_untouched(self) -> None:
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="18C", tool_call_id="t1")])]
        out = _truncate_replay_tool_returns(delta)
        # Same object (no copy when nothing needs trimming).
        assert out[0] is delta[0]
        assert out[0].parts[0].content == "18C"

    def test_oversized_non_str_return_is_truncated(self) -> None:
        """A structured (dict) tool return whose JSON-serialized form exceeds
        the cap IS truncated — the model only ever sees
        ``model_response_str()``, so swapping the dict for the truncated str
        is lossless from the model's POV. (The old ``str``-only check missed
        this entirely.)"""
        payload = {"temp": 18, "unit": "C", "blob": "y" * (REPLAY_TOOL_RETURN_MAX_CHARS + 100)}
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content=payload, tool_call_id="t1")])]
        out = _truncate_replay_tool_returns(delta)
        content = out[0].parts[0].content
        # Content is now a TRUNCATED STR (the JSON render, capped), not the dict.
        assert isinstance(content, str)
        assert len(content) <= REPLAY_TOOL_RETURN_MAX_CHARS
        assert content.endswith("…(truncated)")
        # Original delta is untouched (immutable copy; the dict is preserved).
        assert delta[0].parts[0].content == payload

    def test_small_non_str_return_untouched(self) -> None:
        """A structured tool return whose serialized form is well under the
        cap is left entirely alone (same object, dict content preserved)."""
        payload = {"temp": 18, "unit": "C"}
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content=payload, tool_call_id="t1")])]
        out = _truncate_replay_tool_returns(delta)
        assert out[0] is delta[0]
        assert out[0].parts[0].content == payload

    def test_oversized_builtin_tool_return_is_truncated(self) -> None:
        """A ``BuiltinToolReturnPart`` (also a ``BaseToolReturnPart``) is
        truncated by the same model-facing-string rule — the old check only
        matched plain ``ToolReturnPart`` and missed this subtype."""
        big = "b" * (REPLAY_TOOL_RETURN_MAX_CHARS + 200)
        delta = [ModelRequest(parts=[BuiltinToolReturnPart(tool_name="web", content=big, tool_call_id="t1")])]
        out = _truncate_replay_tool_returns(delta)
        part = out[0].parts[0]
        # Same subtype is preserved (dataclasses.replace keeps the class).
        assert isinstance(part, BuiltinToolReturnPart)
        assert len(part.content) <= REPLAY_TOOL_RETURN_MAX_CHARS
        assert part.content.endswith("…(truncated)")

    def test_mixed_parts_only_oversized_str_trimmed(self) -> None:
        big = "z" * (REPLAY_TOOL_RETURN_MAX_CHARS + 10)
        delta = [
            ModelResponse(
                parts=[
                    TextPart(content="x" * (REPLAY_TOOL_RETURN_MAX_CHARS + 99)),
                    ToolCallPart(tool_name="t", args={"a": 1}, tool_call_id="t1"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="t", content=big, tool_call_id="t1"),
                    UserPromptPart(content="ambient note"),
                ]
            ),
        ]
        out = _truncate_replay_tool_returns(delta)
        # ModelResponse (no oversized tool return) is the SAME object.
        assert out[0] is delta[0]
        # The TextPart, though long, is NOT trimmed (only tool returns are).
        assert len(out[0].parts[0].content) == REPLAY_TOOL_RETURN_MAX_CHARS + 99
        # Only the oversized tool return in the ModelRequest is trimmed; the
        # sibling UserPromptPart is preserved untouched.
        assert len(out[1].parts[0].content) <= REPLAY_TOOL_RETURN_MAX_CHARS
        assert out[1].parts[1].content == "ambient note"

    def test_empty_input(self) -> None:
        assert _truncate_replay_tool_returns([]) == []
