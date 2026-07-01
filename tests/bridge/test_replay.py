"""Tests for tool-call replay hydration (R-A3).

On a new turn the bridge re-injects an agent's prior structured tool
calls/returns into the reconstructed ``message_history`` so the model sees
the tools it used — not just its final texts. This is bridge-side only: the
agent receives the already-hydrated history.

Coverage:

* :func:`build_message_history` splices an agent reply's persisted delta
  IMMEDIATELY BEFORE that reply's ``ModelResponse`` — and is byte-identical
  when ``hydration=None``. POV-agnostic: every agent record (``is_agent``) is
  eligible; a human record is never hydrated; a leading agent reply (no
  preceding human request) is dropped, not spliced.
* :class:`DiscordHistoryProvider` joins (never DB-scans) the fetched,
  ``/clear``-truncated records against the transcript store, stamps the agent
  name on the replayed responses, and returns the hydrated history. A reply
  with no stored row, a ``/clear``-truncated reply, and the
  :class:`NullTranscriptStore` (failed-open) all degrade to no replay.
* :func:`_truncate_replay_tool_returns` truncates oversized tool returns by
  their model-facing string — ``str``, non-``str`` (structured) and
  ``BuiltinToolReturnPart`` content; short returns are left untouched.
* :func:`_stamp_response_names` stamps the agent name on ModelResponses only,
  leaves ModelRequests untouched, and is non-mutating.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

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

from calfcord.bridge.history import (
    REPLAY_TOOL_RETURN_MAX_CHARS,
    DiscordHistoryProvider,
    HistoryRecord,
    _stamp_response_names,
    _truncate_replay_tool_returns,
    build_message_history,
)
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.transcripts import NullTranscriptStore, TranscriptRow, TranscriptStore
from calfcord.bridge.wire import WireAuthor, WireMessage

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _inert_wire(channel_id: int, *, content: str = "hi") -> WireMessage:
    """A minimal valid WireMessage for MentionRequest fixtures whose code paths
    read only the request's typed fields, never the wire itself."""
    return WireMessage(
        event_id="e1",
        kind="message",
        message_id=1,
        channel_id=channel_id,
        source_channel_id=channel_id,
        guild_id=1,
        content=content,
        author=WireAuthor(discord_user_id=1, display_name="ryan", is_bot=False, is_webhook=False),
        created_at=datetime.now(UTC),
    )


def _record(
    *,
    message_id: int = 1,
    content: str = "hi",
    author_display_name: str = "ryan",
    is_agent: bool = False,
) -> HistoryRecord:
    return HistoryRecord(
        message_id=message_id,
        created_at=datetime.now(UTC),
        content=content,
        author_display_name=author_display_name,
        is_agent=is_agent,
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


def _req(*, message_id: int = 30, source_channel_id: int = 6789) -> MentionRequest:
    """A minimal :class:`MentionRequest`.

    :class:`DiscordHistoryProvider` reads only ``message_id`` (the history-fetch
    anchor passed as ``before=``) and ``source_channel_id`` (the un-flattened
    channel to fetch); the rest is filled with inert placeholders.
    """
    return MentionRequest(
        content="and again?",
        mention_ids=("scribe",),
        author_label="ryan",
        message_id=message_id,
        source_channel_id=source_channel_id,
        channel_id=source_channel_id,
        wire=_inert_wire(source_channel_id, content="and again?"),
        reply_target=object(),
    )


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
# build_message_history — splice semantics (pure function)
# ---------------------------------------------------------------------------


class TestBuildMessageHistoryHydration:
    def test_splices_delta_before_agent_response(self) -> None:
        """The delta lands IMMEDIATELY BEFORE the agent reply's ModelResponse."""
        records = [
            _record(message_id=1, content="what's the weather?", author_display_name="ryan"),
            _record(message_id=2, content="It's 18C.", author_display_name="Scribe", is_agent=True),
        ]
        delta = _tool_delta()
        out = build_message_history(records, hydration={2: delta})

        # ryan's request, then the spliced tool call + return, then the
        # final-answer ModelResponse — in order.
        assert len(out) == 4
        assert isinstance(out[0], ModelRequest)
        assert out[0].parts[0].content == "what's the weather?"
        # Spliced delta (same objects, in order, right before the reply) — the
        # builder splices the given list verbatim (name-stamping is the
        # provider's job, tested in TestDiscordHistoryProvider).
        assert out[1] is delta[0]
        assert out[2] is delta[1]
        # The reply's own final text comes LAST.
        assert isinstance(out[3], ModelResponse)
        assert out[3].parts[0].content == "It's 18C."

    def test_multi_row_hydration_pairs_each_delta_with_its_own_reply(self) -> None:
        """Two distinct agent replies (ids 2 and 4) each carry their OWN
        persisted delta. Each delta must splice IMMEDIATELY BEFORE its own
        reply's ModelResponse — never cross-contaminate the other reply.
        """
        records = [
            _record(message_id=1, content="first question", author_display_name="ryan"),
            _record(message_id=2, content="first answer", author_display_name="Scribe", is_agent=True),
            _record(message_id=3, content="second question", author_display_name="ryan"),
            _record(message_id=4, content="second answer", author_display_name="Scribe", is_agent=True),
        ]
        # Two DISTINCT deltas with different tool names so cross-pairing is
        # detectable by object identity AND by content.
        delta_a = _tool_delta(tool_name="alpha", content="A-result")
        delta_b = _tool_delta(tool_name="beta", content="B-result")
        out = build_message_history(records, hydration={2: delta_a, 4: delta_b})

        # req1, [delta_a call, delta_a return], reply2,
        # req3, [delta_b call, delta_b return], reply4 → 8 entries.
        assert len(out) == 8
        # First turn: ryan's request, then delta_a spliced before reply 2.
        assert isinstance(out[0], ModelRequest)
        assert out[0].parts[0].content == "first question"
        assert out[1] is delta_a[0]
        assert out[2] is delta_a[1]
        assert isinstance(out[3], ModelResponse)
        assert out[3].parts[0].content == "first answer"
        # Second turn: ryan's request, then delta_b spliced before reply 4.
        assert isinstance(out[4], ModelRequest)
        assert out[4].parts[0].content == "second question"
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
        """hydration=None reproduces the no-replay build exactly."""
        records = [
            _record(message_id=1, content="hi", author_display_name="ryan"),
            _record(message_id=2, content="hey", author_display_name="Scribe", is_agent=True),
        ]
        baseline = build_message_history(records)
        explicit_none = build_message_history(records, hydration=None)
        assert len(baseline) == len(explicit_none) == 2
        for a, b in zip(baseline, explicit_none, strict=True):
            assert type(a) is type(b)
            assert a.parts[0].content == b.parts[0].content

    def test_missing_row_leaves_build_unchanged(self) -> None:
        """An agent reply whose message_id is absent from the map is not spliced."""
        records = [
            _record(message_id=1, content="q", author_display_name="ryan"),
            _record(message_id=2, content="a", author_display_name="Scribe", is_agent=True),
        ]
        # Hydration keyed on a DIFFERENT message id (no match).
        out = build_message_history(records, hydration={999: _tool_delta()})
        assert len(out) == 2
        assert isinstance(out[1], ModelResponse)
        assert out[1].parts[0].content == "a"

    def test_human_record_is_never_hydrated(self) -> None:
        """Only agent records (``is_agent``) are eligible for hydration; a human
        record whose message_id happens to be in the map is never spliced (it
        becomes a ModelRequest and never carries tool calls)."""
        records = [
            _record(message_id=1, content="kick off", author_display_name="ryan"),
            _record(message_id=2, content="another human", author_display_name="ryan"),
        ]
        # message_id=2 is a HUMAN record but present in the map.
        out = build_message_history(records, hydration={2: _tool_delta()})
        assert len(out) == 2
        assert all(isinstance(m, ModelRequest) for m in out)
        for m in out:
            for p in m.parts:
                assert not isinstance(p, ToolCallPart | ToolReturnPart)

    def test_all_agent_records_are_eligible_for_hydration(self) -> None:
        """POV-agnostic: there is no self/peer distinction in the builder, so
        EVERY agent record with a delta in the map is hydrated — even one that,
        pre-migration, would have been a 'peer' from some agent's POV. calfkit's
        projection handles per-viewer re-roling downstream.
        """
        records = [
            _record(message_id=1, content="kick off", author_display_name="ryan"),
            _record(message_id=2, content="peer reply", author_display_name="Conan", is_agent=True),
        ]
        delta = _tool_delta()
        out = build_message_history(records, hydration={2: delta})
        # ryan request, spliced delta (2 msgs), Conan's response → 4 entries.
        assert len(out) == 4
        assert isinstance(out[0], ModelRequest)
        assert out[1] is delta[0]
        assert out[2] is delta[1]
        assert isinstance(out[3], ModelResponse)
        assert out[3].parts[0].content == "peer reply"

    def test_leading_agent_reply_is_not_hydrated(self) -> None:
        """An agent reply with NO preceding human request is a leading
        ModelResponse the trailing drop removes. Its delta must NOT splice —
        otherwise the leading tool-call ModelResponse is popped and an orphaned
        tool-return ModelRequest (a ``tool_result`` with no matching
        ``tool_use``) is stranded at the head, which providers 400 on. The
        ``seen_request`` gate prevents the splice.
        """
        records = [
            # Oldest record is the agent's OWN reply — nothing before it.
            _record(message_id=2, content="earlier answer", author_display_name="Scribe", is_agent=True),
            _record(message_id=3, content="follow-up", author_display_name="ryan"),
        ]
        out = build_message_history(records, hydration={2: _tool_delta()})
        # The leading agent reply is dropped entirely; nothing is spliced. What
        # survives is ryan's request — and crucially the head is NOT an orphaned
        # tool-return ModelRequest.
        assert len(out) == 1
        assert isinstance(out[0], ModelRequest)
        assert out[0].parts[0].content == "follow-up"
        for m in out:
            for p in m.parts:
                assert not isinstance(p, ToolCallPart | ToolReturnPart)


# ---------------------------------------------------------------------------
# DiscordHistoryProvider — fetch + join + two-turn replay
# ---------------------------------------------------------------------------


class TestDiscordHistoryProvider:
    """Drives :meth:`DiscordHistoryProvider.message_history` end to end with a
    fake fetcher + transcript store — the ``MentionRequest`` → ``list[
    ModelMessage]`` seam the bridge's MentionHandler calls. No Kafka, no
    Discord, no DB.
    """

    async def test_two_turn_replay_splices_tools(self) -> None:
        """Seed a row for the agent's reply; on the next turn the tool call +
        return are spliced immediately before the agent's final-answer
        response."""
        records = [
            _record(message_id=10, content="weather?", author_display_name="ryan"),
            _record(message_id=20, content="It's 18C.", author_display_name="Scribe", is_agent=True),
        ]
        provider = DiscordHistoryProvider(
            _fake_fetcher(records),
            _fake_store({"20": _transcript_row(final_message_id=20)}),
        )

        message_history = await provider.message_history(_req(message_id=30))

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

    async def test_fetch_is_anchored_on_the_request(self) -> None:
        """The provider fetches ``source_channel_id`` and anchors ``before=`` on
        the request's ``message_id`` (the history-fetch anchor)."""
        fetcher = _fake_fetcher([])
        provider = DiscordHistoryProvider(fetcher, _fake_store({}))

        await provider.message_history(_req(message_id=30, source_channel_id=6789))

        kw = fetcher.fetch.call_args.kwargs
        assert kw["source_channel_id"] == 6789
        assert kw["before_message_id"] == 30

    async def test_stamps_agent_name_on_replayed_responses(self) -> None:
        """A persisted delta has no ``name`` on its responses; the provider
        stamps the agent's name (the record's display name) onto every
        ModelResponse in the spliced delta so calfkit's POV projection
        attributes those tool calls to that agent."""
        records = [
            _record(message_id=10, content="weather?", author_display_name="ryan"),
            _record(message_id=20, content="It's 18C.", author_display_name="Scribe", is_agent=True),
        ]
        provider = DiscordHistoryProvider(
            _fake_fetcher(records),
            _fake_store({"20": _transcript_row(final_message_id=20)}),
        )

        message_history = await provider.message_history(_req(message_id=30))

        # The spliced ModelResponse (the tool-call turn) carries the agent name…
        assert message_history[1].name == "Scribe"
        # …as does the final-answer response (stamped by build_message_history).
        assert message_history[3].name == "Scribe"

    async def test_missing_row_no_splice(self) -> None:
        """An agent reply record with no transcript row → build unchanged."""
        records = [
            _record(message_id=10, content="weather?", author_display_name="ryan"),
            _record(message_id=20, content="It's 18C.", author_display_name="Scribe", is_agent=True),
        ]
        provider = DiscordHistoryProvider(_fake_fetcher(records), _fake_store({}))  # no rows

        message_history = await provider.message_history(_req(message_id=30))

        assert len(message_history) == 2  # just request + final reply, no splice
        assert not any(isinstance(p, ToolCallPart | ToolReturnPart) for m in message_history for p in m.parts)

    async def test_null_store_degrades_to_no_replay(self) -> None:
        """The failed-open :class:`NullTranscriptStore` returns {} from the batch
        join, so no replay happens and nothing crashes — today's behavior."""
        records = [
            _record(message_id=10, content="q", author_display_name="ryan"),
            _record(message_id=20, content="a", author_display_name="Scribe", is_agent=True),
        ]
        provider = DiscordHistoryProvider(_fake_fetcher(records), NullTranscriptStore())

        message_history = await provider.message_history(_req(message_id=30))

        assert len(message_history) == 2

    async def test_clear_truncated_reply_is_never_hydrated(self) -> None:
        """A reply that /clear truncated OUT of the fetched records is absent
        from ``records``, so it is never even queried for hydration.

        The fetcher already applies the /clear boundary; here we model the
        post-truncation record set (the old reply with message_id=20 is gone)
        and assert the store is queried only for the SURVIVING agent reply
        (message_id=40), never the truncated one."""
        # Only the post-clear survivors: a new prompt + a new agent reply. The
        # pre-clear reply (message_id=20) is NOT present.
        records = [
            _record(message_id=30, content="new question", author_display_name="ryan"),
            _record(message_id=40, content="new answer", author_display_name="Scribe", is_agent=True),
        ]
        store = _fake_store(
            {
                "20": _transcript_row(final_message_id=20),  # pre-clear (must NOT be read)
                "40": _transcript_row(final_message_id=40),
            }
        )
        provider = DiscordHistoryProvider(_fake_fetcher(records), store)

        await provider.message_history(_req(message_id=50))

        # The join key list contains only the surviving agent reply's id.
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


# ---------------------------------------------------------------------------
# _stamp_response_names
# ---------------------------------------------------------------------------


class TestStampResponseNames:
    """Stamps the agent name on every ModelResponse in a persisted delta so
    calfkit's POV projection attributes the spliced tool calls to that agent.
    ModelRequests are untouched; the operation is non-mutating.
    """

    def test_stamps_name_on_responses(self) -> None:
        messages = _tool_delta()  # [ModelResponse(...), ModelRequest(...)]
        out = _stamp_response_names(messages, "Scribe")
        assert isinstance(out[0], ModelResponse)
        assert out[0].name == "Scribe"
        # Parts survive the rebuild (the tool call is still there).
        assert any(isinstance(p, ToolCallPart) for p in out[0].parts)

    def test_leaves_requests_as_same_object(self) -> None:
        """A ModelRequest is the SAME object (no copy) — only responses are
        rebuilt."""
        messages = _tool_delta()
        out = _stamp_response_names(messages, "Scribe")
        assert isinstance(out[1], ModelRequest)
        assert out[1] is messages[1]

    def test_non_mutating(self) -> None:
        """The input is not mutated — its ModelResponse keeps its original name;
        only the returned copy is stamped."""
        messages = _tool_delta()
        original_name = messages[0].name
        _stamp_response_names(messages, "Scribe")
        assert messages[0].name == original_name

    def test_already_correctly_named_response_is_same_object(self) -> None:
        """A ModelResponse already stamped with the target name is left as the
        SAME object (the ``m.name != name`` guard skips the rebuild)."""
        already = ModelResponse(parts=[TextPart(content="hi")], name="Scribe")
        out = _stamp_response_names([already], "Scribe")
        assert out[0] is already
