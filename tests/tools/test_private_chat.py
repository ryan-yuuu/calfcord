"""Unit tests for the ``private_chat`` A2A tool.

Tests call the bare async function directly with a constructed
:class:`ToolContext`, bypassing calfkit's tool dispatch. The module-level
singletons are populated via ``monkeypatch.setattr`` per-test (so leak is
impossible across tests), and the phonebook arrives via ``ctx.deps`` —
mirroring the bridge ingress, which is the tool's only source of agent
identity.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from calfkit.client import Client
from calfkit.models import ToolContext
from calfkit.models.session_context import Deps

from calfkit_organization.agents.phonebook import PhonebookEntry, phonebook_to_deps
from calfkit_organization.bridge.egress import A2AChannelResolver
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.discord.persona import DiscordPersonaSender
from calfkit_organization.tools import private_chat as pc


def _wire(
    *,
    content: str = "hi",
    kind: str = "slash",
    slash_target: str | None = None,
    channel_id: int = 999,
) -> WireMessage:
    """Build a minimal WireMessage representing the inbound that triggered
    the calling agent. The WireMessage validator requires
    ``slash_target`` iff ``kind == "slash"`` — this helper enforces the
    invariant: ``slash_target`` defaults to ``"alice"`` for slash kind and
    ``None`` for message kind."""
    if kind == "slash" and slash_target is None:
        slash_target = "alice"
    if kind == "message":
        slash_target = None
    return WireMessage(
        event_id="evt-1",
        kind=kind,  # type: ignore[arg-type]
        slash_target=slash_target,
        message_id=42,
        channel_id=channel_id,
        guild_id=10,
        content=content,
        author=WireAuthor(
            discord_user_id=1,
            display_name="ryan",
            is_bot=False,
            is_webhook=False,
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _entry(agent_id: str, *, tools: tuple[str, ...] = ()) -> PhonebookEntry:
    return PhonebookEntry(
        agent_id=agent_id,
        display_name=f"{agent_id.title()} Bot",
        avatar_url=f"https://example.com/{agent_id}.png",
        description="test",
        tools=tools,
    )


# Default phonebook used by ``_ctx``: just alice and bob, no tools. Tests
# that need a different roster construct one inline and pass via ``phonebook=``.
_DEFAULT_PHONEBOOK = [_entry("alice"), _entry("bob")]


def _ctx(
    *,
    caller: str = "alice",
    wire: WireMessage | None = None,
    phonebook: list[PhonebookEntry] | None = None,
) -> ToolContext:
    """Construct a ToolContext mirroring what calfkit's dispatch builds.

    The bridge ingress populates ``deps["phonebook"]`` on every invocation;
    tests do the same so the tool reads the same shape it would in
    production.
    """
    if wire is None:
        wire = _wire()
    if phonebook is None:
        phonebook = _DEFAULT_PHONEBOOK
    return ToolContext(
        deps=Deps(
            correlation_id="corr-1",
            provided_deps={
                "discord": wire.model_dump(mode="json"),
                "phonebook": phonebook_to_deps(phonebook),
            },
        ),
        agent_name=caller,
    )


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject mocks into private_chat's module-level singletons.

    No registry: under the decoupled-deployment model the tool's only
    source of agent identity is the phonebook in ``ctx.deps``. Tests
    that want a different phonebook pass it to ``_ctx``.

    ``monkeypatch.setattr`` restores the originals after the test, so
    one test's ``init`` cannot leak into another's.
    """
    client = MagicMock(spec=Client)
    client.execute_node = AsyncMock()
    persona_sender = MagicMock(spec=DiscordPersonaSender)
    persona_sender.send = AsyncMock()
    resolver = MagicMock(spec=A2AChannelResolver)
    resolver.resolve_or_create = AsyncMock(return_value=12345)

    monkeypatch.setattr(pc, "_client", client)
    monkeypatch.setattr(pc, "_persona_sender", persona_sender)
    monkeypatch.setattr(pc, "_resolver", resolver)
    monkeypatch.setattr(pc, "_timeout_seconds", 30.0)

    return {
        "client": client,
        "persona_sender": persona_sender,
        "resolver": resolver,
    }


def _result(text: str) -> Any:
    """A minimal stand-in for ``NodeResult`` carrying only the fields the
    tool reads. The real type has many more fields irrelevant here."""
    r = MagicMock()
    r.output = text
    r.correlation_id = "tool-corr"
    return r


class TestHappyPath:
    async def test_returns_target_response_text(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "hello bob")
        assert out == "bob's reply"

    async def test_invokes_target_inbox_topic(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "msg")
        call = deps["client"].execute_node.await_args
        assert call.kwargs["topic"] == "agent.bob.in"

    async def test_passes_caller_agent_id_in_deps(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "msg")
        passed_deps = deps["client"].execute_node.await_args.kwargs["deps"]
        assert passed_deps["caller_agent_id"] == "alice"

    async def test_forwarded_wire_overrides_slash_target_and_kind(
        self, deps: dict[str, Any]
    ) -> None:
        """B's existing addressed_to_me gate requires slash_target == B and
        kind == "slash". The tool must mutate both on the forwarded wire.
        Starts from a kind=message inbound (slash_target=None per the
        WireMessage validator) to verify both fields get rewritten."""
        deps["client"].execute_node.return_value = _result("ok")
        inbound = _wire(kind="message", content="orig")
        await pc.private_chat(_ctx(caller="alice", wire=inbound), "bob", "new")
        passed_deps = deps["client"].execute_node.await_args.kwargs["deps"]
        forwarded = passed_deps["discord"]
        assert forwarded["slash_target"] == "bob"
        assert forwarded["kind"] == "slash"

    async def test_forwarded_wire_content_is_a2a_payload(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        inbound = _wire(content="original")
        await pc.private_chat(
            _ctx(caller="alice", wire=inbound), "bob", "the a2a request"
        )
        forwarded = deps["client"].execute_node.await_args.kwargs["deps"]["discord"]
        assert forwarded["content"] == "the a2a request"

    async def test_forwarded_wire_preserves_channel_and_author(
        self, deps: dict[str, Any]
    ) -> None:
        """Channel and original author stay on the forwarded wire — the
        caller_agent_id key carries the new info; everything else is
        unchanged Discord context."""
        deps["client"].execute_node.return_value = _result("ok")
        inbound = _wire(channel_id=777)
        await pc.private_chat(_ctx(caller="alice", wire=inbound), "bob", "x")
        forwarded = deps["client"].execute_node.await_args.kwargs["deps"]["discord"]
        assert forwarded["channel_id"] == 777
        assert forwarded["author"]["display_name"] == "ryan"

    async def test_uses_configured_timeout(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(), "bob", "x")
        assert deps["client"].execute_node.await_args.kwargs["timeout"] == 30.0

    async def test_passes_temp_instructions_for_target(
        self, deps: dict[str, Any]
    ) -> None:
        """When invoking the target, the tool injects the peer-roster
        temp_instructions so the target (if A2A-enabled itself) sees who
        else it can chain-call. Built from the phonebook in deps, so a
        hot-added agent (in a future registry refresh) reaches the next
        invocation immediately."""
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("private_chat",)),
            _entry("carol"),
        ]
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice", phonebook=phonebook), "bob", "x")
        instructions = deps["client"].execute_node.await_args.kwargs["temp_instructions"]
        assert instructions is not None
        assert "carol" in instructions
        assert "bob" not in instructions  # target excluded from its own roster

    async def test_propagates_phonebook_to_target_deps(
        self, deps: dict[str, Any]
    ) -> None:
        """The phonebook must ride along to the target so a chain-calling
        target (B → C) doesn't lose its view of the organization. The
        chain target's roster depends on ``tools``, ``display_name``,
        and ``avatar_url`` per entry — assert the full shape survives,
        not just the ids."""
        phonebook = [
            _entry("alice", tools=("private_chat",)),
            _entry("bob", tools=("private_chat",)),
            _entry("carol"),
        ]
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice", phonebook=phonebook), "bob", "x")
        passed_deps = deps["client"].execute_node.await_args.kwargs["deps"]
        propagated = passed_deps["phonebook"]
        ids = sorted(e["agent_id"] for e in propagated)
        assert ids == ["alice", "bob", "carol"]
        # Find bob in the propagated list and confirm full identity rode along.
        propagated_bob = next(e for e in propagated if e["agent_id"] == "bob")
        assert propagated_bob["tools"] == ["private_chat"]
        assert propagated_bob["display_name"] == "Bob Bot"
        assert propagated_bob["avatar_url"] == "https://example.com/bob.png"

    async def test_resolves_pair_channel_for_caller_and_target(
        self, deps: dict[str, Any]
    ) -> None:
        deps["client"].execute_node.return_value = _result("ok")
        await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        deps["resolver"].resolve_or_create.assert_awaited_once_with("alice", "bob")

    async def test_posts_both_projections(self, deps: dict[str, Any]) -> None:
        deps["client"].execute_node.return_value = _result("bob's reply")
        await pc.private_chat(_ctx(caller="alice"), "bob", "alice asks")
        sent = deps["persona_sender"].send
        assert sent.await_count == 2
        # First call: caller persona + request text. Second: target persona +
        # response text.
        first_persona = sent.await_args_list[0].args[0]
        first_content = sent.await_args_list[0].kwargs["content"]
        second_persona = sent.await_args_list[1].args[0]
        second_content = sent.await_args_list[1].kwargs["content"]
        assert first_persona.name == "Alice Bot"
        assert first_content == "alice asks"
        assert second_persona.name == "Bob Bot"
        assert second_content == "bob's reply"

    async def test_empty_target_response_projects_placeholder(
        self, deps: dict[str, Any]
    ) -> None:
        """Discord rejects empty content; an empty A2A reply still needs an
        audit entry, so the projection substitutes a visible placeholder."""
        deps["client"].execute_node.return_value = _result("")
        await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        second_content = (
            deps["persona_sender"].send.await_args_list[1].kwargs["content"]
        )
        assert second_content == "(empty response)"

    async def test_none_target_response_treated_as_empty(
        self, deps: dict[str, Any]
    ) -> None:
        """``NodeResult.output`` is ``OutputT | None`` per calfkit's type —
        the ``output is not None`` guard at the response site needs its own
        test so a future ``result.output or ""`` refactor (which would
        coerce falsy values differently) doesn't slip through."""
        deps["client"].execute_node.return_value = _result(None)
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert out == ""
        second_content = (
            deps["persona_sender"].send.await_args_list[1].kwargs["content"]
        )
        assert second_content == "(empty response)"


class TestInputErrors:
    async def test_self_target_returns_error_string(
        self, deps: dict[str, Any]
    ) -> None:
        """LLM-recoverable error: returned as a string so the calling LLM
        can adapt rather than aborting the whole turn."""
        out = await pc.private_chat(_ctx(caller="alice"), "alice", "x")
        assert "cannot privately chat with itself" in out
        deps["client"].execute_node.assert_not_called()

    async def test_unknown_target_returns_error_with_known_list(
        self, deps: dict[str, Any]
    ) -> None:
        out = await pc.private_chat(_ctx(caller="alice"), "carol", "x")
        assert "unknown agent" in out
        assert "alice" in out
        assert "bob" in out
        deps["client"].execute_node.assert_not_called()


class TestInfraErrors:
    async def test_not_initialized_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling the tool body without ``init()`` is a runner bug; raise
        so it surfaces in logs rather than degrading silently."""
        monkeypatch.setattr(pc, "_client", None)
        monkeypatch.setattr(pc, "_persona_sender", None)
        monkeypatch.setattr(pc, "_resolver", None)
        with pytest.raises(RuntimeError, match="not initialized"):
            await pc.private_chat(_ctx(), "bob", "x")

    async def test_missing_emitter_node_id_raises(self, deps: dict[str, Any]) -> None:
        """``ctx.agent_name`` should be set from the x-calf-emitter header
        in calfkit dispatch; missing implies a bypass."""
        ctx = _ctx()
        ctx.agent_name = None  # simulate missing emitter
        with pytest.raises(RuntimeError, match="emitter_node_id"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_missing_phonebook_dep_raises(self, deps: dict[str, Any]) -> None:
        """The bridge ingress is contractually required to populate
        ``deps['phonebook']`` on every publish — its absence indicates the
        invocation bypassed the bridge, not an LLM input error."""
        ctx = ToolContext(
            deps=Deps(correlation_id="c", provided_deps={}),
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="deps\\['phonebook'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_missing_discord_dep_raises(self, deps: dict[str, Any]) -> None:
        """Same contract as phonebook: bridge populates ``deps['discord']``
        on every publish."""
        ctx = ToolContext(
            deps=Deps(
                correlation_id="c",
                provided_deps={"phonebook": phonebook_to_deps(_DEFAULT_PHONEBOOK)},
            ),
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="deps\\['discord'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_unknown_caller_raises(self, deps: dict[str, Any]) -> None:
        """If the phonebook doesn't include the caller, persona resolution
        would fall back to nothing — surface this as an infrastructure bug,
        not as an error string the LLM could accidentally suppress."""
        # Phonebook contains bob but not the caller ("ghost").
        phonebook = [_entry("bob")]
        with pytest.raises(RuntimeError, match="not in the phonebook"):
            await pc.private_chat(
                _ctx(caller="ghost", phonebook=phonebook), "bob", "x"
            )

    async def test_malformed_phonebook_wrapped_as_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        """Pydantic ValidationError from a malformed phonebook entry must
        be normalized to RuntimeError so the infra-bug contract holds —
        upstream code distinguishes infra bugs from LLM-recoverable
        errors by exception type, not by string parsing."""
        ctx = ToolContext(
            deps=Deps(
                correlation_id="c",
                provided_deps={
                    "discord": _wire().model_dump(mode="json"),
                    # Schema-invalid entry: missing required fields.
                    "phonebook": [{"agent_id": "alice"}],
                },
            ),
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="malformed deps\\['phonebook'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_non_list_phonebook_wrapped_as_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        """``phonebook_from_deps`` raises a plain ``ValueError`` for a
        non-list payload; the tool must also normalize that to
        RuntimeError so callers don't have to handle two exception
        families for the same infra bug."""
        ctx = ToolContext(
            deps=Deps(
                correlation_id="c",
                provided_deps={
                    "discord": _wire().model_dump(mode="json"),
                    "phonebook": "not a list",
                },
            ),
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="malformed deps\\['phonebook'\\]"):
            await pc.private_chat(ctx, "bob", "x")

    async def test_malformed_wire_wrapped_as_runtime_error(
        self, deps: dict[str, Any]
    ) -> None:
        """Same normalization for a malformed discord wire — a missing
        required field should not leak ``ValidationError`` to the LLM."""
        ctx = ToolContext(
            deps=Deps(
                correlation_id="c",
                provided_deps={
                    "discord": {"only": "garbage"},  # missing every required field
                    "phonebook": phonebook_to_deps(_DEFAULT_PHONEBOOK),
                },
            ),
            agent_name="alice",
        )
        with pytest.raises(RuntimeError, match="malformed deps\\['discord'\\]"):
            await pc.private_chat(ctx, "bob", "x")


class TestProjectionBestEffort:
    async def test_projection_failure_does_not_abort(
        self, deps: dict[str, Any]
    ) -> None:
        """A transient Discord projection error must never lose the A2A call."""
        deps["client"].execute_node.return_value = _result("bob's reply")
        deps["persona_sender"].send = AsyncMock(
            side_effect=discord.DiscordException("transient")
        )
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert out == "bob's reply"

    async def test_projection_retries_once_then_logs_with_correlation(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Each projection post tries twice on persistent Discord failure;
        second-failure log names the channel, caller, target, and (when
        known) correlation_id so an operator can match a gap to a turn.

        The final-failure line is at ERROR severity — permanent audit data
        loss, not a transient blip — so alerting hooks fire."""
        import logging as _logging

        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=discord.DiscordException("persistent")
        )
        with caplog.at_level(_logging.WARNING):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # 2 attempts per projection × 2 projections = 4 send calls.
        assert deps["persona_sender"].send.await_count == 4
        final = [r for r in caplog.records if "accepting audit gap" in r.message]
        assert final, "expected a final-failure log line"
        # Severity pinned: permanent audit loss is ERROR, not WARNING.
        assert all(r.levelno >= _logging.ERROR for r in final)
        joined = " ".join(r.getMessage() for r in final)
        assert "caller=alice" in joined
        assert "target=bob" in joined

    async def test_projection_succeeds_on_retry(self, deps: dict[str, Any]) -> None:
        """First attempt fails, second succeeds: the retry actually works.
        Pins the ``return`` inside the retry loop so a refactor that broke
        the early-return would surface here."""
        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=[
                discord.DiscordException("transient"),  # first projection, attempt 1 fails
                None,  # first projection, attempt 2 succeeds
                None,  # second projection, attempt 1 succeeds
            ]
        )
        await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert deps["persona_sender"].send.await_count == 3

    async def test_non_discord_projection_error_propagates(
        self, deps: dict[str, Any]
    ) -> None:
        """RuntimeError / TypeError from the persona sender indicate
        infrastructure misconfiguration (sender not started, channel id not
        a text channel) — they must NOT be swallowed as "best-effort."""
        deps["client"].execute_node.return_value = _result("ok")
        deps["persona_sender"].send = AsyncMock(
            side_effect=RuntimeError("sender not started")
        )
        with pytest.raises(RuntimeError, match="sender not started"):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")


class TestInit:
    """``init()`` is the only path the runner uses to wire dependencies.
    A regression that swapped parameters (e.g. persona_sender vs resolver)
    would silently break A2A at runtime — pin the bindings."""

    def test_init_binds_each_arg_to_its_singleton(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reset to sentinel state so we observe init's writes.
        monkeypatch.setattr(pc, "_client", None)
        monkeypatch.setattr(pc, "_persona_sender", None)
        monkeypatch.setattr(pc, "_resolver", None)
        monkeypatch.setattr(pc, "_timeout_seconds", -1.0)

        client = MagicMock(spec=Client)
        persona_sender = MagicMock(spec=DiscordPersonaSender)
        resolver = MagicMock(spec=A2AChannelResolver)

        pc.init(
            client=client,
            persona_sender=persona_sender,
            resolver=resolver,
            timeout_seconds=42.0,
        )

        assert pc._client is client
        assert pc._persona_sender is persona_sender
        assert pc._resolver is resolver
        assert pc._timeout_seconds == 42.0


class TestExecuteNodeFailures:
    async def test_timeout_returns_error_string_not_raise(
        self, deps: dict[str, Any]
    ) -> None:
        """``execute_node`` timeout is operational, not LLM-input — but if
        we raise, the tool's ReturnCall never fires and the calling agent's
        own execute also times out (double timeout). Returning a string
        lets the calling LLM see the failure and adapt."""
        deps["client"].execute_node.side_effect = asyncio.TimeoutError()
        out = await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        assert "did not reply" in out
        assert "bob" in out

    async def test_timeout_skips_response_projection(
        self, deps: dict[str, Any]
    ) -> None:
        """On timeout, only the request projection has been posted; the
        response projection must not run (there's no response). Pins that
        the second send call is skipped."""
        deps["client"].execute_node.side_effect = asyncio.TimeoutError()
        await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        # Only the request projection attempted (1 send call).
        assert deps["persona_sender"].send.await_count == 1


class TestResolverFailure:
    async def test_resolver_failure_propagates_and_skips_invocation(
        self, deps: dict[str, Any]
    ) -> None:
        """Channel resolution is intentionally NOT best-effort: without an
        audit channel there's nowhere to project, and the audit invariant
        is part of the design. The error must propagate, and the target
        agent must never be invoked under a half-broken setup."""
        deps["resolver"].resolve_or_create.side_effect = discord.Forbidden(
            MagicMock(status=403), "missing permission"
        )
        with pytest.raises(discord.Forbidden):
            await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        deps["client"].execute_node.assert_not_called()
        deps["persona_sender"].send.assert_not_called()

    async def test_resolver_failure_logs_caller_and_target(
        self,
        deps: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The resolver itself logs only the success path. The
        private_chat layer adds caller/target context on failure so an
        operator looking at the tools log knows which A2A turn was
        affected."""
        import logging as _logging

        deps["resolver"].resolve_or_create.side_effect = discord.Forbidden(
            MagicMock(status=403), "missing permission"
        )
        with caplog.at_level(_logging.ERROR):
            with pytest.raises(discord.Forbidden):
                await pc.private_chat(_ctx(caller="alice"), "bob", "x")
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "caller=alice" in joined
        assert "target=bob" in joined
