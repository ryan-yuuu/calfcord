"""Unit tests for the reply poster (re-homed outbox posting; calfkit-012 B3)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

from calfcord.bridge import reply_poster as rp
from calfcord.bridge.mention_handler import MentionRequest
from calfcord.bridge.reply_poster import ReplyPoster
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.discord.messages import SentMessage
from calfcord.discord.persona import Persona


# --- helpers ---------------------------------------------------------------
class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "err"


def _http(status: int, text: str = "rejected") -> discord.HTTPException:
    return discord.HTTPException(_Resp(status), text)


def _wire_dict(*, source_channel_id: int | None = None, channel_id: int = 6789) -> dict[str, Any]:
    return WireMessage(
        event_id="c1",
        kind="message",
        slash_target=None,
        message_id=12345,
        channel_id=channel_id,
        source_channel_id=source_channel_id,
        guild_id=4242,
        content="hello?",
        author=WireAuthor(discord_user_id=111, display_name="alice", is_bot=False, is_webhook=False),
        created_at=datetime.now(UTC),
    ).model_dump(mode="json")


def _req(wire: dict[str, Any] | None = None) -> MentionRequest:
    wire = wire if wire is not None else _wire_dict()
    return MentionRequest(
        content="hello?",
        mention_ids=("scribe",),
        author_label="alice",
        message_id=12345,
        source_channel_id=wire["source_channel_id"] or wire["channel_id"],
        channel_id=wire["channel_id"],
        wire=wire,
        reply_target=_FakeReplyTarget(),
    )


def _result(output: str, *, message_history: list[Any] | None = None, emitter: str = "scribe") -> Any:
    return SimpleNamespace(
        output=output, message_history=message_history or [], emitter_node_id=emitter, correlation_id="c1"
    )


def _tool_history() -> list[Any]:
    """A history whose [1:-1] slice is a single tool call+return (one step block)."""
    return [
        ModelRequest(parts=[TextPart(content="prefix")]),  # channel-history prefix (initial_len=1)
        ModelResponse(parts=[ToolCallPart(tool_name="search", args={"q": "x"}, tool_call_id="t1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="search", content="res", tool_call_id="t1")]),
        ModelResponse(parts=[TextPart(content="final")]),  # trailing final answer (dropped by [:-1])
    ]


class _FakePersonas:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self._next_id = 7000
        self.errors: list[Exception | None] = []  # scripted per-send; None = success

    async def send(
        self,
        persona: Persona,
        channel_id: int,
        content: str,
        *,
        reply_to: Any = None,
        extra_buttons: Any = None,
        thread_id: int | None = None,
    ) -> SentMessage:
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self._next_id += 1
        self.sends.append(
            {
                "persona": persona,
                "channel_id": channel_id,
                "content": content,
                "reply_to": reply_to,
                "extra_buttons": extra_buttons,
                "thread_id": thread_id,
            }
        )
        return SentMessage(id=self._next_id, channel_id=thread_id or channel_id)


class _FakeStore:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.rows: list[Any] = []

    async def write_turn(self, row: Any) -> None:
        self.rows.append(row)


class _FakeReplyTarget:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.fail = False

    async def reply(self, text: str) -> None:
        if self.fail:
            raise _http(403)
        self.replies.append(text)


def _poster(
    personas: _FakePersonas | None = None, store: _FakeStore | None = None
) -> tuple[ReplyPoster, _FakePersonas, _FakeStore]:
    p = personas or _FakePersonas()
    s = store or _FakeStore()
    return ReplyPoster(p, s), p, s  # type: ignore[arg-type]


# --- tests -----------------------------------------------------------------
class TestPostReply:
    async def test_happy_path_posts_and_returns_ok(self) -> None:
        poster, personas, store = _poster()
        out = await poster.post_reply(
            _req(), Persona(name="scribe"), _result("done"), initial_len=0, correlation_id="c1"
        )
        assert out.status == "ok"
        assert len(personas.sends) == 1
        assert personas.sends[0]["persona"].name == "scribe"
        assert personas.sends[0]["content"] == "done"
        assert personas.sends[0]["extra_buttons"] is None  # pure-text turn: no toggle
        assert store.rows == []

    async def test_empty_output_is_ok_without_sending(self) -> None:
        poster, personas, _ = _poster()
        out = await poster.post_reply(
            _req(), Persona(name="scribe"), _result("   "), initial_len=0, correlation_id="c1"
        )
        assert out.status == "ok"
        assert personas.sends == []

    async def test_tool_turn_attaches_toggle_and_writes_transcript(self) -> None:
        poster, personas, store = _poster()
        out = await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result("final", message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert out.status == "ok"
        assert personas.sends[0]["extra_buttons"] is not None  # toggle attached
        assert len(store.rows) == 1
        assert store.rows[0].agent_id == "scribe" and store.rows[0].correlation_id == "c1"

    async def test_pure_text_turn_no_toggle_no_row(self) -> None:
        poster, personas, store = _poster()
        # message_history with no tool slice → empty delta
        hist = [ModelRequest(parts=[TextPart(content="p")]), ModelResponse(parts=[TextPart(content="final")])]
        await poster.post_reply(
            _req(), Persona(name="scribe"), _result("final", message_history=hist), initial_len=1, correlation_id="c1"
        )
        assert personas.sends[0]["extra_buttons"] is None
        assert store.rows == []

    async def test_disabled_store_suppresses_toggle_and_write(self) -> None:
        poster, personas, store = _poster(store=_FakeStore(enabled=False))
        await poster.post_reply(
            _req(),
            Persona(name="scribe"),
            _result("final", message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert personas.sends[0]["extra_buttons"] is None
        assert store.rows == []

    async def test_thread_routing(self) -> None:
        poster, personas, _ = _poster()
        await poster.post_reply(
            _req(_wire_dict(source_channel_id=99999)),
            Persona(name="scribe"),
            _result("done"),
            initial_len=0,
            correlation_id="c1",
        )
        assert personas.sends[0]["thread_id"] == 99999


class TestFailureClassification:
    async def test_agent_fixable_returns_retry(self) -> None:
        personas = _FakePersonas()
        personas.errors = [_http(400, "too long")]
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(_req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1")
        assert out.status == "retry" and out.failed_text == "x" and out.error is not None

    async def test_forbidden_returns_dropped_and_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        personas = _FakePersonas()
        personas.errors = [discord.Forbidden(_Resp(403), "no perms")]
        poster, _, _ = _poster(personas)
        with caplog.at_level("WARNING"):
            out = await poster.post_reply(
                _req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1"
            )
        assert out.status == "dropped"
        # 403 (missing Manage Webhooks) is an operator-actionable misconfiguration → ERROR.
        drops = [r for r in caplog.records if "dropping" in r.message]
        assert len(drops) == 1 and drops[0].levelname == "ERROR"

    async def test_rate_limited_returns_dropped_and_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        personas = _FakePersonas()
        personas.errors = [discord.RateLimited(1.0)]
        poster, _, _ = _poster(personas)
        with caplog.at_level("WARNING"):
            out = await poster.post_reply(
                _req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1"
            )
        assert out.status == "dropped"
        # Rate-limit is transient noise → WARNING, not ERROR.
        drops = [r for r in caplog.records if "dropping" in r.message]
        assert len(drops) == 1 and drops[0].levelname == "WARNING"

    async def test_non_discord_sender_error_returns_dropped(self) -> None:
        personas = _FakePersonas()
        personas.errors = [TypeError("not a text channel")]
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(_req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1")
        assert out.status == "dropped"

    async def test_5xx_once_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rp, "_SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        personas = _FakePersonas()
        personas.errors = [discord.DiscordServerError(_Resp(503), "down"), None]  # fail then succeed
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(_req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1")
        assert out.status == "ok"
        assert len(personas.sends) == 1  # the successful retry

    async def test_persistent_5xx_is_dropped_as_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rp, "_SERVER_ERROR_RETRY_DELAY_SECONDS", 0)
        personas = _FakePersonas()
        personas.errors = [
            discord.DiscordServerError(_Resp(503), "down"),
            discord.DiscordServerError(_Resp(503), "down"),
        ]
        poster, _, _ = _poster(personas)
        out = await poster.post_reply(_req(), Persona(name="scribe"), _result("x"), initial_len=0, correlation_id="c1")
        assert out.status == "dropped"


class TestPostChunked:
    async def test_long_reply_splits_first_chunk_has_toggle_and_row(self) -> None:
        poster, personas, store = _poster()
        big = "x" * 4500
        posted = await poster.post_chunked(
            _req(),
            Persona(name="scribe"),
            _result(big, message_history=_tool_history()),
            initial_len=1,
            correlation_id="c1",
        )
        assert posted is True  # at least one chunk delivered
        assert len(personas.sends) >= 3  # >2 chunks
        assert personas.sends[0]["extra_buttons"] is not None  # toggle on first chunk only
        assert all(s["extra_buttons"] is None for s in personas.sends[1:])
        assert personas.sends[0]["reply_to"] is not None  # anchor on first chunk only
        assert all(s["reply_to"] is None for s in personas.sends[1:])
        assert len(store.rows) == 1

    async def test_all_chunks_fail_returns_false(self) -> None:
        personas = _FakePersonas()
        personas.errors = [_http(403)] * 10
        poster, _, store = _poster(personas)
        # must not raise even though every chunk fails, and must signal total loss
        # (False) so the handler surfaces an operator notice.
        posted = await poster.post_chunked(
            _req(), Persona(name="scribe"), _result("x" * 4500), initial_len=0, correlation_id="c1"
        )
        assert posted is False
        assert store.rows == []


class TestPostNotice:
    async def test_notice_replies_to_trigger(self) -> None:
        poster, _, _ = _poster()
        req = _req()
        await poster.post_notice(req, "no agent online")
        assert req.reply_target.replies == ["no agent online"]

    async def test_notice_swallows_failure(self) -> None:
        poster, _, _ = _poster()
        req = _req()
        req.reply_target.fail = True
        await poster.post_notice(req, "boom")  # must not raise
        assert req.reply_target.replies == []
