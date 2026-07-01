"""Unit tests for the per-@mention orchestration (spec §5.2).

Drives :class:`MentionHandler` through a ``FakeHandle`` (scripted ``stream()`` +
``result()``) and recording collaborator fakes — no Kafka, no Discord, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from calfkit.client import AgentMessageEvent, RunCompleted, RunFailed, ToolCallEvent, ToolResultEvent
from calfkit.exceptions import NodeFaultError
from calfkit.models.error_report import ErrorReport
from calfkit.models.node_result import InvocationResult
from calfkit.models.payload import TextPart
from calfkit.models.state import State

from calfcord.agents.thinking import build_model_settings_union
from calfcord.bridge.mention_handler import MentionHandler, MentionRequest, ReplyOutcome
from calfcord.bridge.wire import WireAuthor, WireMessage


def _fixable_error() -> Any:
    """A stand-in for a Discord 4xx the agent can fix (build_retry_reminder reads
    only ``.status``/``.code``/``.text``)."""
    return SimpleNamespace(status=400, code=0, text="Must be 2000 or fewer in length.")


# --- fakes -----------------------------------------------------------------
class _FakeHandle:
    def __init__(
        self,
        *,
        steps: tuple[Any, ...] = (),
        result: Any = None,
        fault: Exception | None = None,
        correlation_id: str = "c1",
    ) -> None:
        self._steps = list(steps)
        self._result = result
        self._fault = fault
        self.correlation_id = correlation_id

    async def stream(self) -> Any:
        for s in self._steps:
            yield s
        # The real calfkit stream() is terminal-bearing: it yields the terminal
        # (RunCompleted / RunFailed) as its LAST event. normalize_run_event maps
        # those to None, so the handler drain must skip them (`if step is None:
        # continue`). Yield one here so that skip is exercised at the integration
        # level — a refactor to direct `event.kind` access would crash the drain.
        if self._fault is not None:
            report = getattr(self._fault, "report", None) or ErrorReport(error_type="calf.test.fault")
            yield RunFailed(report=report, correlation_id=self.correlation_id)
        else:
            output = getattr(self._result, "output", "") if self._result is not None else ""
            yield RunCompleted(output=output, correlation_id=self.correlation_id, agent="scribe", _envelope=None)

    async def result(self, *, timeout: float | None = None) -> Any:
        if self._fault is not None:
            raise self._fault
        return self._result


class _FakeGateway:
    def __init__(self, handles: list[_FakeHandle]) -> None:
        self._handles = handles
        self.starts: list[dict[str, Any]] = []

    @property
    def started(self) -> dict[str, Any] | None:
        """The first ``start()`` call's kwargs (the original invocation)."""
        return self.starts[0] if self.starts else None

    async def start(self, prompt: str, **kwargs: Any) -> _FakeHandle:
        self.starts.append({"prompt": prompt, **kwargs})
        # Successive start()s (retries) consume successive handles; the last
        # handle is reused if start() is called more times than handles given.
        return self._handles[min(len(self.starts) - 1, len(self._handles) - 1)]


class _FakeClient:
    def __init__(self, handles: list[_FakeHandle]) -> None:
        self.gw = _FakeGateway(handles)
        self.requested_agent: str | None = None

    def agent(self, name: str) -> _FakeGateway:
        self.requested_agent = name
        return self.gw


class _FakeRoster:
    def __init__(self, online: frozenset[str] | None) -> None:
        self._online = online
        self.refreshes = 0

    async def refresh(self) -> None:
        self.refreshes += 1

    def online(self) -> frozenset[str] | None:
        return self._online


class _FakeHistory:
    def __init__(self) -> None:
        self.calls = 0

    async def message_history(self, req: MentionRequest) -> list[Any]:
        self.calls += 1
        return []


class _FakeOverrides:
    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._m = mapping or {}

    def effort_for(self, agent_id: str) -> str | None:
        return self._m.get(agent_id)


class _FakeA2A:
    def __init__(self) -> None:
        self.projected: list[Any] = []
        self.faults: list[Any] = []

    async def project(self, projection: Any) -> None:
        self.projected.append(projection)

    async def project_fault(self, call: Any) -> None:
        self.faults.append(call)


class _FakeProgress:
    def __init__(self) -> None:
        self.steps: list[Any] = []
        self.finished: list[str] = []

    async def on_step(self, step: Any, req: MentionRequest) -> None:
        self.steps.append(step)

    async def finish(self, correlation_id: str) -> None:
        self.finished.append(correlation_id)


class _FakeReply:
    def __init__(
        self, outcomes: list[ReplyOutcome] | None = None, *, chunked_ok: bool = True
    ) -> None:
        self.replies: list[tuple[Any, str]] = []
        self.chunked: list[tuple[Any, str]] = []
        self.notices: list[str] = []
        # Every correlation_id the handler passed, across retries + chunk fallback.
        # Pins the D-13 guarantee that retries reuse the ORIGINAL run's id (one
        # transcript row), not each retry handle's id.
        self.correlation_ids: list[str] = []
        # Scripted per-post_reply outcomes; defaults to "ok" once exhausted.
        self._outcomes = list(outcomes or [])
        # Whether post_chunked reports a successful delivery (False => fully lost).
        self._chunked_ok = chunked_ok

    async def post_reply(
        self, req: MentionRequest, persona: Any, result: Any, *, initial_len: int, correlation_id: str
    ) -> ReplyOutcome:
        self.replies.append((persona, result.output))
        self.correlation_ids.append(correlation_id)
        return self._outcomes.pop(0) if self._outcomes else ReplyOutcome("ok")

    async def post_chunked(
        self, req: MentionRequest, persona: Any, result: Any, *, initial_len: int, correlation_id: str
    ) -> bool:
        self.chunked.append((persona, result.output))
        self.correlation_ids.append(correlation_id)
        return self._chunked_ok

    async def post_notice(self, req: MentionRequest, text: str) -> None:
        self.notices.append(text)


def _result(output: str, emitter: str) -> InvocationResult:
    return InvocationResult(
        output=output, state=State(message_history=[]), correlation_id="c1", emitter_node_id=emitter
    )


def _agent_msg(text: str, emitter: str = "scribe") -> AgentMessageEvent:
    return AgentMessageEvent(correlation_id="c1", depth=0, frame_id="f", emitter=emitter, parts=[TextPart(text=text)])


def _consult(tcid: str, peer: str, message: str, caller: str = "scribe") -> ToolCallEvent:
    return ToolCallEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter=caller,
        tool_call_id=tcid,
        name="message_agent",
        args={"name": peer, "message": message},
    )


def _consult_reply(tcid: str, peer: str, text: str) -> ToolResultEvent:
    return ToolResultEvent(
        correlation_id="c1",
        depth=1,
        frame_id="f",
        emitter=peer,
        tool_call_id=tcid,
        name=peer,
        parts=[TextPart(text=text)],
        is_error=False,
    )


def _wire(content: str = "hello") -> WireMessage:
    return WireMessage(
        event_id="e1",
        kind="message",
        message_id=1,
        channel_id=10,
        source_channel_id=10,
        guild_id=42,
        content=content,
        author=WireAuthor(discord_user_id=111, display_name="alice", is_bot=False, is_webhook=False),
        created_at=datetime.now(UTC),
    )


def _req(content: str = "hello", mentions: tuple[str, ...] = ("scribe",)) -> MentionRequest:
    return MentionRequest(
        content=content,
        mention_ids=mentions,
        author_label="alice",
        message_id=1,
        source_channel_id=10,
        channel_id=10,
        wire=_wire(content),
        reply_target=object(),
    )


def _make(
    *,
    online: frozenset[str] | None = frozenset({"scribe"}),
    handle: _FakeHandle | None = None,
    handles: list[_FakeHandle] | None = None,
    overrides: dict[str, str] | None = None,
    reply_outcomes: list[ReplyOutcome] | None = None,
    chunked_ok: bool = True,
) -> tuple[MentionHandler, _FakeClient, dict[str, Any]]:
    if handles is None:
        handles = [handle if handle is not None else _FakeHandle(result=_result("done", "scribe"))]
    client = _FakeClient(handles)
    fakes = {
        "a2a": _FakeA2A(),
        "progress": _FakeProgress(),
        "reply": _FakeReply(reply_outcomes, chunked_ok=chunked_ok),
        "history": _FakeHistory(),
    }
    handler = MentionHandler(
        client=client,
        roster=_FakeRoster(online),
        history=fakes["history"],
        overrides=_FakeOverrides(overrides),
        a2a=fakes["a2a"],
        progress=fakes["progress"],
        reply=fakes["reply"],
        memory_deps=lambda: {"memory_prompt": "tmpl"},
    )
    return handler, client, fakes


class TestRouting:
    async def test_happy_path_posts_reply_under_emitter_persona(self) -> None:
        handler, client, fakes = _make()
        await handler.handle(_req(mentions=("scribe",)))
        assert client.requested_agent == "scribe"
        # start carried history, author, the serialized discord wire, memory deps.
        started = client.gw.started
        assert started["author"] == "alice"
        discord_dep = started["deps"]["discord"]
        assert discord_dep["content"] == "hello" and discord_dep["channel_id"] == 10
        assert started["deps"]["memory_prompt"] == "tmpl"
        # one reply, persona named for the emitter, text == result.output.
        assert len(fakes["reply"].replies) == 1
        persona, text = fakes["reply"].replies[0]
        assert persona.name == "scribe" and text == "done"
        assert fakes["progress"].finished == ["c1"]
        assert fakes["reply"].notices == []

    async def test_reply_uses_handoff_emitter_persona_not_target(self) -> None:
        handler, _client, fakes = _make(handle=_FakeHandle(result=_result("handed off answer", "conan")))
        await handler.handle(_req(mentions=("scribe",)))
        persona, _ = fakes["reply"].replies[0]
        assert persona.name == "conan"  # the node that actually replied

    async def test_no_mention_is_unanswered(self) -> None:
        handler, client, fakes = _make()
        await handler.handle(_req(mentions=()))
        assert client.requested_agent is None
        assert fakes["reply"].replies == [] and fakes["reply"].notices == []

    async def test_mentioned_but_none_online_posts_notice(self) -> None:
        handler, client, fakes = _make(online=frozenset({"scribe"}))
        await handler.handle(_req(mentions=("ghost",)))
        assert client.requested_agent is None
        assert fakes["reply"].replies == []
        assert len(fakes["reply"].notices) == 1 and "ghost" in fakes["reply"].notices[0]

    async def test_first_online_mention_wins(self) -> None:
        handler, client, _fakes = _make(online=frozenset({"conan"}))
        await handler.handle(_req(mentions=("ghost", "conan")))
        assert client.requested_agent == "conan"

    async def test_roster_unavailable_fails_fast_with_notice(self) -> None:
        handler, client, fakes = _make(online=None)
        await handler.handle(_req(mentions=("scribe",)))
        assert client.requested_agent is None
        assert len(fakes["reply"].notices) == 1 and "roster" in fakes["reply"].notices[0].lower()


class TestStreamDrain:
    async def test_a2a_consult_goes_to_projector_not_progress(self) -> None:
        steps = (_consult("t1", "conan", "summarize"), _consult_reply("t1", "conan", "the summary"))
        handler, _client, fakes = _make(handle=_FakeHandle(steps=steps, result=_result("done", "scribe")))
        await handler.handle(_req())
        assert len(fakes["a2a"].projected) == 2  # request + reply
        assert fakes["progress"].steps == []

    async def test_plain_agent_message_goes_to_progress(self) -> None:
        handler, _client, fakes = _make(
            handle=_FakeHandle(steps=(_agent_msg("thinking…"),), result=_result("done", "scribe"))
        )
        await handler.handle(_req())
        assert len(fakes["progress"].steps) == 1
        assert fakes["a2a"].projected == []


class TestTerminalErrors:
    async def test_fault_posts_error_and_synthesizes_dangling_a2a_notes(self) -> None:
        # A consult is opened (tool_call) but the peer faults — no reply step;
        # result() raises NodeFaultError, faulting the whole run (D-2).
        handle = _FakeHandle(steps=(_consult("t9", "conan", "x"),), fault=NodeFaultError("peer_fault", message="boom"))
        handler, _client, fakes = _make(handle=handle)
        await handler.handle(_req())
        assert len(fakes["a2a"].projected) == 1  # the request rendered before the fault
        assert len(fakes["a2a"].faults) == 1  # the dangling consult → failure note
        assert fakes["a2a"].faults[0].peer == "conan"
        assert len(fakes["reply"].notices) == 1 and fakes["reply"].replies == []


class TestOverrides:
    async def test_effort_override_applied_as_provider_blind_union(self) -> None:
        handler, client, _fakes = _make(overrides={"scribe": "high"})
        await handler.handle(_req(mentions=("scribe",)))
        assert client.gw.started["model_settings"] == build_model_settings_union("high")

    async def test_no_override_passes_none_model_settings(self) -> None:
        handler, client, _fakes = _make()
        await handler.handle(_req(mentions=("scribe",)))
        assert client.gw.started["model_settings"] is None


@pytest.mark.parametrize("output", ["", "a long reply"])
async def test_output_text_round_trips(output: str) -> None:
    handler, _client, fakes = _make(handle=_FakeHandle(result=_result(output, "scribe")))
    await handler.handle(_req())
    assert fakes["reply"].replies[0][1] == output


class TestRetryWithFeedback:
    async def test_agent_fixable_failure_retries_then_succeeds(self) -> None:
        # First post is rejected (agent-fixable); a second start() (the retry) yields
        # a corrected reply that posts OK.
        h1 = _FakeHandle(result=_result("too long", "scribe"))
        h2 = _FakeHandle(result=_result("concise", "scribe"))
        handler, client, fakes = _make(
            handles=[h1, h2],
            reply_outcomes=[ReplyOutcome("retry", error=_fixable_error(), failed_text="too long"), ReplyOutcome("ok")],
        )
        await handler.handle(_req())
        # original invocation + exactly one retry start()
        assert len(client.gw.starts) == 2
        # the retry carried the corrective reminder as its prompt
        assert "<system-reminder>" in client.gw.starts[1]["prompt"]
        # the retry preserved deps/author/model_settings
        assert client.gw.starts[1]["author"] == "alice"
        assert client.gw.starts[1]["deps"]["memory_prompt"] == "tmpl"
        # two post attempts; the second posted the corrected text; no chunk fallback
        assert [t for _, t in fakes["reply"].replies] == ["too long", "concise"]
        assert fakes["reply"].chunked == []

    async def test_exhausted_retries_fall_back_to_chunked(self) -> None:
        # Every post is agent-fixable → 1 original + MAX_REPLY_RETRY_ATTEMPTS (2)
        # retries = 3 posts, then a chunk-split fallback of the last attempt.
        handler, client, fakes = _make(
            handles=[_FakeHandle(result=_result("x", "scribe"))],
            reply_outcomes=[ReplyOutcome("retry", error=_fixable_error(), failed_text="x")] * 3,
        )
        await handler.handle(_req())
        assert len(fakes["reply"].replies) == 3
        assert len(fakes["reply"].chunked) == 1
        assert len(client.gw.starts) == 3  # original + 2 retries

    async def test_dropped_outcome_does_not_retry_but_notifies(self) -> None:
        handler, client, fakes = _make(reply_outcomes=[ReplyOutcome("dropped")])
        await handler.handle(_req())
        assert len(client.gw.starts) == 1  # infra failure → no retry
        assert fakes["reply"].chunked == []
        # I-2: a dropped reply must surface an operator notice, not ghost the user.
        assert len(fakes["reply"].notices) == 1 and "couldn't post" in fakes["reply"].notices[0].lower()

    async def test_chunk_total_loss_posts_notice(self) -> None:
        # Retries exhausted → chunk-split fallback, and every chunk also fails
        # (post_chunked returns False) → the user gets an operator notice.
        handler, _client, fakes = _make(
            handles=[_FakeHandle(result=_result("x", "scribe"))],
            reply_outcomes=[ReplyOutcome("retry", error=_fixable_error(), failed_text="x")] * 3,
            chunked_ok=False,
        )
        await handler.handle(_req())
        assert len(fakes["reply"].chunked) == 1
        assert len(fakes["reply"].notices) == 1 and "couldn't post" in fakes["reply"].notices[0].lower()

    async def test_fault_logs_full_error_report_at_error(self, caplog: pytest.LogCaptureFixture) -> None:
        # I-1: the fault log must carry the ErrorReport calfkit shipped (error_type,
        # message), not just the origin, and at ERROR.
        handle = _FakeHandle(fault=NodeFaultError("billing.quota_exceeded", message="boom"))
        handler, _client, _fakes = _make(handle=handle)
        with caplog.at_level("ERROR"):
            await handler.handle(_req())
        faults = [r for r in caplog.records if "faulted" in r.message and r.levelname == "ERROR"]
        assert len(faults) == 1
        assert "error_type=billing.quota_exceeded" in faults[0].message
        assert "message=boom" in faults[0].message

    async def test_all_attempts_reuse_original_correlation_id(self) -> None:
        # D-13: retries (and the chunk fallback) must key the transcript on the
        # ORIGINAL run's correlation_id so they UPSERT one row, not orphan a fresh
        # row per attempt. Give the retry handles a DIFFERENT id so a regression to
        # retry_handle.correlation_id would be caught.
        original = _FakeHandle(result=_result("x", "scribe"), correlation_id="c1")
        retry1 = _FakeHandle(result=_result("x", "scribe"), correlation_id="c2")
        retry2 = _FakeHandle(result=_result("x", "scribe"), correlation_id="c2")
        handler, _client, fakes = _make(
            handles=[original, retry1, retry2],
            reply_outcomes=[ReplyOutcome("retry", error=_fixable_error(), failed_text="x")] * 3,
        )
        await handler.handle(_req())
        # 3 post_reply + 1 post_chunked, every one under the original run's id.
        assert fakes["reply"].correlation_ids == ["c1", "c1", "c1", "c1"]

    async def test_retry_reinvocation_fault_posts_notice(self) -> None:
        # The retry re-invocation itself faults → user-facing notice, no crash.
        h1 = _FakeHandle(result=_result("x", "scribe"))
        h2 = _FakeHandle(fault=NodeFaultError("peer_fault", message="boom"))
        handler, _client, fakes = _make(
            handles=[h1, h2],
            reply_outcomes=[ReplyOutcome("retry", error=_fixable_error(), failed_text="x")],
        )
        await handler.handle(_req())
        assert len(fakes["reply"].notices) == 1
        assert fakes["reply"].chunked == []
