"""Unit tests for ``BridgeIngress.handle``.

Mocks the calfkit ``Client.send`` and asserts the bridge
publishes the right envelope and maintains the ``PendingWires`` map.
No Kafka, no Discord, no LLM.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.agents.memory import MEMORY_PROMPT_DEPS_KEY
from calfcord.agents.memory import _reset_cache_for_tests as _reset_memory_cache
from calfcord.bridge.ingress import BridgeIngress
from calfcord.bridge.pending_wires import PendingWires
from calfcord.bridge.registry import AgentRegistry
from calfcord.bridge.wire import WireAuthor, WireMessage
from calfcord.topics import DISCORD_OUTBOX_TOPIC


def _wire(
    *,
    event_id: str = "evt-1",
    slash_target: str | None = "scheduler",
    kind: str = "slash",
) -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind=kind,  # type: ignore[arg-type]
        slash_target=slash_target,
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content="book me a haircut",
        author=WireAuthor(
            discord_user_id=111,
            display_name="alice",
            is_bot=False,
            is_webhook=False,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
            is_human_owner=True,
        ),
        created_at=datetime.now(UTC),
    )


def _registry(
    *,
    scheduler_effort: str | None = None,
    scribe_effort: str | None = None,
) -> AgentRegistry:
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar.",
                avatar_url="https://example.com/aksel.png",
                provider="anthropic",
                thinking_effort=scheduler_effort,  # type: ignore[arg-type]
                system_prompt="Anthropic scheduler.",
            ),
            AgentDefinition(
                agent_id="scribe",
                display_name="Scribe",
                description="Notes.",
                avatar_url="https://example.com/scribe.png",
                provider="openai",
                thinking_effort=scribe_effort,  # type: ignore[arg-type]
                system_prompt="OpenAI scribe.",
            ),
        ]
    )


@pytest.fixture
def client() -> MagicMock:
    """Fake calfkit Client covering both ingress paths.

    The slash branch publishes to a channel topic (naming
    ``discord.outbox`` as ``reply_to``) and the ambient branch to the
    router's ambient ingress (``reply_to=None``) — both via
    ``client.send``. ``send`` is fire-and-forget: it registers no reply
    future and returns the ``str`` correlation_id, so the mock returns a
    plain string rather than a handle.
    """
    c = MagicMock()
    c.send = AsyncMock(return_value="corr-id")
    return c


@pytest.fixture
def pending_wires() -> PendingWires:
    return PendingWires()


class TestPublish:
    async def test_invokes_with_in_suffix_topic_and_deps(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())

        kwargs = client.send.call_args.kwargs
        assert kwargs["topic"] == "discord.channel.6789.in"
        assert kwargs["correlation_id"] == "evt-1"
        # The agent's terminal reply lands on the outbox topic.
        assert kwargs["reply_to"] == DISCORD_OUTBOX_TOPIC
        # Full wire round-trips as a dep so the agent's gates can inspect it.
        assert kwargs["deps"]["discord"]["channel_id"] == 6789
        assert kwargs["deps"]["discord"]["slash_target"] == "scheduler"

    async def test_ambient_message_is_dropped_without_publishing(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Ambient (non-@mention) messages go unanswered once the router is
        removed (C2): the bridge accepts a ``kind="message"`` wire but
        publishes nothing — there is no agent to route it to."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(slash_target=None, kind="message"))
        client.send.assert_not_awaited()

    async def test_includes_phonebook_in_deps(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Every invocation carries a phonebook snapshot so decoupled
        deployments (e.g. the tools runner) can resolve personas, build
        peer rosters, and validate targets without local file access."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())

        phonebook = client.send.call_args.kwargs["deps"]["phonebook"]
        assert isinstance(phonebook, list)
        ids = sorted(e["agent_id"] for e in phonebook)
        assert ids == ["scheduler", "scribe"]
        # Each entry carries the fields downstream needs.
        scribe = next(e for e in phonebook if e["agent_id"] == "scribe")
        assert scribe["display_name"] == "Scribe"
        assert "description" in scribe

    async def test_records_wire_in_pending_wires_before_send(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Consumer might fire before send returns — wire must be visible already."""
        observed: dict[str, WireMessage | None] = {}

        async def _capture(*_args: Any, **kwargs: Any) -> str:
            entry = pending_wires.get(kwargs["correlation_id"])
            observed["wire"] = entry.wire if entry is not None else None
            return kwargs["correlation_id"]

        client.send.side_effect = _capture
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire()
        await ingress.handle(wire)

        assert observed["wire"] is wire

    async def test_pops_wire_when_send_raises(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        client.send.side_effect = RuntimeError("kafka down")
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire()

        with pytest.raises(RuntimeError):
            await ingress.handle(wire)

        assert pending_wires.get(wire.event_id) is None

    async def test_publishes_via_send_with_outbox_reply_to(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """The slash branch uses fire-and-forget ``Client.send`` — which
        registers no reply future (nothing to cancel, nothing to leak) —
        and names ``discord.outbox`` as the ``reply_to`` return address so
        the agent's terminal reply lands on the bridge's outbox consumer."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())

        client.send.assert_awaited_once()
        assert client.send.await_args.kwargs["reply_to"] == DISCORD_OUTBOX_TOPIC


class TestModelSettings:
    """Per-call model_settings injection driven by registry state + provider mapping."""

    @pytest.mark.parametrize(
        ("effort", "expected"),
        [
            ("low", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 4000}}),
            ("medium", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 10000}}),
            ("high", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}}),
            ("xhigh", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 48000}}),
            ("max", {"anthropic_thinking": {"type": "enabled", "budget_tokens": 63999}}),
        ],
    )
    async def test_anthropic_target_thinking_dict_per_tier(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        effort: str,
        expected: dict,
    ) -> None:
        ingress = BridgeIngress(
            client, _registry(scheduler_effort=effort), pending_wires
        )
        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.send.call_args.kwargs["model_settings"] == expected

    @pytest.mark.parametrize(
        ("effort", "expected_value"),
        [
            # Matches the operator → OpenAI mapping in
            # :mod:`calfcord.agents.thinking`. The ramp
            # was shifted up one notch when ``minimal`` was added —
            # the lookup-table comment in that module documents the
            # one-time behavior bump for existing OpenAI agents.
            ("minimal", "minimal"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("xhigh", "high"),
            ("max", "high"),  # OpenAI saturates at high.
        ],
    )
    async def test_openai_target_reasoning_effort_per_tier(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        effort: str,
        expected_value: str,
    ) -> None:
        ingress = BridgeIngress(
            client, _registry(scribe_effort=effort), pending_wires
        )
        await ingress.handle(_wire(slash_target="scribe"))
        assert client.send.call_args.kwargs["model_settings"] == {
            "openai_reasoning_effort": expected_value
        }

    async def test_effort_none_passes_empty_dict(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Operator-disabled thinking is an explicit empty dict, not None."""
        ingress = BridgeIngress(
            client, _registry(scheduler_effort="none"), pending_wires
        )
        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.send.call_args.kwargs["model_settings"] == {}

    async def test_no_effort_in_definition_passes_none(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.send.call_args.kwargs["model_settings"] is None

    async def test_target_missing_from_registry_passes_none(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        with caplog.at_level(logging.ERROR):
            await ingress.handle(_wire(slash_target="ghost"))
        assert client.send.call_args.kwargs["model_settings"] is None
        assert any("missing from registry" in r.message for r in caplog.records)

    async def test_provider_resolution_failure_degrades_to_no_override(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A runtime env-var typo or mapper failure must not blow up the publish."""
        ingress = BridgeIngress(
            client, _registry(scheduler_effort="high"), pending_wires
        )

        def _raise_value_error(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("simulated provider misconfig")

        monkeypatch.setattr(
            "calfcord.bridge.ingress.resolve_provider",
            _raise_value_error,
        )

        with caplog.at_level(logging.WARNING):
            await ingress.handle(_wire(slash_target="scheduler"))

        assert client.send.call_args.kwargs["model_settings"] is None
        assert any(
            "model_settings resolution failed" in r.message for r in caplog.records
        )

    async def test_picks_up_runtime_effort_change(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        tmp_path: Path,
    ) -> None:
        """A registry mutation flows to the next call's settings (hot reload)."""
        import frontmatter

        md_path = tmp_path / "scheduler.md"
        post = frontmatter.Post(
            "Body.",
            name="scheduler",
            display_name="Aksel (Scheduler)",
            description="Calendar.",
            provider="anthropic",
        )
        md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

        registry = AgentRegistry.from_agents_dir(tmp_path)
        ingress = BridgeIngress(client, registry, pending_wires)

        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.send.call_args.kwargs["model_settings"] is None

        registry.apply_local_thinking_effort_override("scheduler", "high")
        await ingress.handle(_wire(event_id="evt-2", slash_target="scheduler"))
        assert client.send.call_args.kwargs["model_settings"] == {
            "anthropic_thinking": {"type": "enabled", "budget_tokens": 31999}
        }


class TestBootValidation:
    """At construction, the bridge validates every agent's `tools:` field
    against ``TOOL_REGISTRY`` so a typo in any `.md` surfaces at bridge
    boot rather than at agent invocation time. Mirrors the existing
    provider validation."""

    def test_unknown_tool_in_any_md_fails_at_construction(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    display_name="Aksel (Scheduler)",
                    description="Calendar.",
                    provider="anthropic",
                    tools=("calndar",),  # typo of "calendar"
                    system_prompt="x",
                ),
            ]
        )
        with pytest.raises(ValueError, match="unknown tool"):
            BridgeIngress(client, registry, pending_wires)

    def test_aggregates_multiple_unknowns_across_agents(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """A multi-typo `.md` set surfaces all offenders in one message —
        operators fix everything in one pass instead of chasing them
        boot-by-boot."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    display_name="Aksel",
                    description="Calendar.",
                    provider="anthropic",
                    tools=("calndar",),
                    system_prompt="x",
                ),
                AgentDefinition(
                    agent_id="scribe",
                    display_name="Scribe",
                    description="Notes.",
                    provider="openai",
                    tools=("emial",),
                    system_prompt="x",
                ),
            ]
        )
        with pytest.raises(ValueError) as excinfo:
            BridgeIngress(client, registry, pending_wires)
        assert "calndar" in str(excinfo.value)
        assert "emial" in str(excinfo.value)

    def test_known_tools_pass(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """`private_chat` is registered; an agent declaring it must
        construct successfully (regression guard against an over-eager
        validator that rejects all tools)."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scribe",
                    display_name="Scribe",
                    description="Notes.",
                    provider="openai",
                    tools=("private_chat",),
                    system_prompt="x",
                ),
            ]
        )
        ingress = BridgeIngress(client, registry, pending_wires)
        assert ingress is not None


class TestTempInstructions:
    """Per-call ``temp_instructions`` carries the channel peer roster and
    the @-mention rules so the LLM knows which peers it can loop in via
    ``@<agent_id>`` syntax. The block is tools-independent — the
    @-mention mechanism lives in the bridge normalizer, not in any tool,
    so it applies to every channel-invoked agent."""

    async def test_instructions_emitted_for_target_without_tools(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Default ``_registry()`` agents have no tools; the channel
        roster + @-mention rules still get injected. Tool-less agents
        must see the rules so they can use the mechanism — the
        @-mention path is bridge-level, not tool-gated."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        instructions = client.send.call_args.kwargs["temp_instructions"]
        assert instructions is not None
        assert "scribe" in instructions  # roster present
        assert "@<agent_id>" in instructions  # mention block present

    async def test_no_instructions_when_target_has_no_peers(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Single-agent registry → no peers to advertise and nothing
        to @-mention → ``None``."""
        solo = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    display_name="X",
                    description="Calendar.",
                    provider="anthropic",
                    system_prompt="x",
                ),
            ]
        )
        ingress = BridgeIngress(client, solo, pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.send.call_args.kwargs["temp_instructions"] is None

    async def test_instructions_injected_when_target_has_private_chat(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Target with ``private_chat`` in tools sees the same channel
        block as a tool-less target — channel context is tools-
        independent. Built from the registry per-call so a future
        hot-add reaches the next invocation immediately."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    display_name="Aksel (Scheduler)",
                    description="Calendar.",
                    provider="anthropic",
                    tools=("private_chat",),
                    system_prompt="x",
                ),
                AgentDefinition(
                    agent_id="scribe",
                    display_name="Scribe",
                    description="Notes.",
                    provider="openai",
                    system_prompt="x",
                ),
            ]
        )
        ingress = BridgeIngress(client, registry, pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        instructions = client.send.call_args.kwargs["temp_instructions"]
        assert instructions is not None
        assert "scribe" in instructions
        assert "scheduler" not in instructions  # self excluded
        assert "@<agent_id>" in instructions  # mention block present

    async def test_target_missing_from_phonebook_logs_error(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Mirrors ``TestModelSettings.test_target_missing_from_registry_passes_none``:
        when ``slash_target`` doesn't resolve in the phonebook, the
        resolver silently produces ``None`` instructions and the agent
        would run with no peer roster. The operator-actionable signal
        is the ERROR log on the call site — without it, the missing
        target would surface only as "no @-mentions ever fire" with no
        breadcrumb pointing at the cause."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        with caplog.at_level(logging.ERROR):
            await ingress.handle(_wire(slash_target="ghost"))
        assert client.send.call_args.kwargs["temp_instructions"] is None
        assert any("missing from phonebook" in r.message for r in caplog.records)


class TestMemoryPromptInjection:
    """The bridge ships the raw memory-prompt template in ``deps`` only when the
    registry holds at least one memory-enabled agent — so existing deployments
    are byte-identical and memory deployments get full (incl. A2A) coverage."""

    @staticmethod
    def _memory_registry() -> AgentRegistry:
        return AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    display_name="Aksel (Scheduler)",
                    description="Calendar.",
                    avatar_url="https://example.com/aksel.png",
                    provider="anthropic",
                    memory=True,
                    system_prompt="Scheduler with memory.",
                ),
            ]
        )

    async def test_omitted_when_no_memory_agent(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        _reset_memory_cache()
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        assert MEMORY_PROMPT_DEPS_KEY not in client.send.call_args.kwargs["deps"]

    async def test_injected_raw_when_memory_agent_present(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        _reset_memory_cache()
        ingress = BridgeIngress(client, self._memory_registry(), pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        deps = client.send.call_args.kwargs["deps"]
        assert MEMORY_PROMPT_DEPS_KEY in deps
        # The bridge ships the RAW template; per-agent localization is the
        # agent-side instructions hook's job, not the bridge's.
        assert "{{MEMORY_DIR}}" in deps[MEMORY_PROMPT_DEPS_KEY]

    async def test_degrades_and_logs_once_on_load_failure(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A bad CALFCORD_MEMORY_PROMPT_PATH must not crash the bridge: the
        invocation still publishes (without the key), and the error is logged
        once across multiple invocations rather than on every message."""
        _reset_memory_cache()
        monkeypatch.setenv("CALFCORD_MEMORY_PROMPT_PATH", "/nonexistent/memory.md")
        ingress = BridgeIngress(client, self._memory_registry(), pending_wires)
        with caplog.at_level(logging.ERROR):
            await ingress.handle(_wire(slash_target="scheduler"))
            await ingress.handle(_wire(slash_target="scheduler"))
        # Published despite the load failure; deps simply omits the key.
        assert client.send.called
        assert MEMORY_PROMPT_DEPS_KEY not in client.send.call_args.kwargs["deps"]
        # Logged exactly once across the two invocations (the one-shot latch).
        errs = [r for r in caplog.records if "failed to load the memory prompt" in r.getMessage()]
        assert len(errs) == 1
        _reset_memory_cache()
