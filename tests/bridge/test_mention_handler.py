"""Unit tests for the per-@mention orchestration (spec §5.2).

Drives :class:`MentionHandler` through a ``FakeHandle`` (scripted ``stream()`` +
``result()``) and recording collaborator fakes — no Kafka, no Discord, no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from calfkit.client import AgentMessageEvent, ToolCallEvent, ToolResultEvent
from calfkit.exceptions import NodeFaultError
from calfkit.models.node_result import InvocationResult
from calfkit.models.payload import TextPart
from calfkit.models.state import State

from calfcord.agents.thinking import build_model_settings_union
from calfcord.bridge.mention_handler import MentionHandler, MentionRequest


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

    async def result(self, *, timeout: float | None = None) -> Any:
        if self._fault is not None:
            raise self._fault
        return self._result


class _FakeGateway:
    def __init__(self, handle: _FakeHandle) -> None:
        self._handle = handle
        self.started: dict[str, Any] | None = None

    async def start(self, prompt: str, **kwargs: Any) -> _FakeHandle:
        self.started = {"prompt": prompt, **kwargs}
        return self._handle


class _FakeClient:
    def __init__(self, handle: _FakeHandle) -> None:
        self.gw = _FakeGateway(handle)
        self.requested_agent: str | None = None

    def agent(self, name: str) -> _FakeGateway:
        self.requested_agent = name
        return self.gw


class _FakeRoster:
    def __init__(self, online: frozenset[str] | None) -> None:
        self._online = online

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
    def __init__(self) -> None:
        self.replies: list[tuple[Any, str]] = []
        self.notices: list[str] = []

    async def post_reply(self, req: MentionRequest, persona: Any, text: str) -> None:
        self.replies.append((persona, text))

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


def _req(content: str = "hello", mentions: tuple[str, ...] = ("scribe",)) -> MentionRequest:
    return MentionRequest(
        content=content,
        mention_ids=mentions,
        author_label="alice",
        source_channel_id=10,
        channel_id=10,
        wire={"channel_id": 10},
        reply_target=object(),
    )


def _make(
    *,
    online: frozenset[str] | None = frozenset({"scribe"}),
    handle: _FakeHandle | None = None,
    overrides: dict[str, str] | None = None,
) -> tuple[MentionHandler, _FakeClient, dict[str, Any]]:
    h = handle if handle is not None else _FakeHandle(result=_result("done", "scribe"))
    client = _FakeClient(h)
    fakes = {"a2a": _FakeA2A(), "progress": _FakeProgress(), "reply": _FakeReply(), "history": _FakeHistory()}
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
        # start carried history, author, the discord wire, memory deps.
        started = client.gw.started
        assert started["author"] == "alice"
        assert started["deps"]["discord"] == {"channel_id": 10}
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
