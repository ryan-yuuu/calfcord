"""Unit tests for ``BridgeIngress.handle``.

Mocks the calfkit ``Client.invoke_node`` and asserts the bridge
publishes the right envelope and maintains the ``PendingWires`` map.
No Kafka, no Discord, no LLM.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.ingress import BridgeIngress
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireAuthor, WireMessage


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
                slash="/scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar.",
                avatar_url="https://example.com/aksel.png",
                provider="anthropic",
                thinking_effort=scheduler_effort,  # type: ignore[arg-type]
                system_prompt="Anthropic scheduler.",
            ),
            AgentDefinition(
                agent_id="scribe",
                slash="/scribe",
                display_name="Scribe",
                description="Notes.",
                avatar_url="https://example.com/scribe.png",
                provider="openai",
                thinking_effort=scribe_effort,  # type: ignore[arg-type]
                system_prompt="OpenAI scribe.",
            ),
        ]
    )


def _fresh_handle() -> MagicMock:
    """A fake InvocationHandle exposing a real asyncio.Future as ``_future``.

    BridgeIngress.handle calls ``handle._future.cancel()`` after a successful
    publish; using a real future makes that observable.
    """
    handle = MagicMock()
    handle._future = asyncio.get_event_loop().create_future()
    return handle


@pytest.fixture
def client() -> MagicMock:
    """Fake calfkit Client covering both ingress paths.

    The slash branch goes through ``client.invoke_node``; the ambient
    branch (Phase 4) goes through ``client._invoke`` via
    :func:`invoke_node_with_metadata`. Both must return a handle whose
    ``_future`` is a real :class:`asyncio.Future` so the ingress's
    cancel-after-publish step has something cancellable. The
    ``reply_topic`` property is also touched by the helper when no
    explicit reply_topic is passed.
    """
    c = MagicMock()
    c.invoke_node = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c._invoke = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c.reply_topic = "discord.outbox"
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

        kwargs = client.invoke_node.call_args.kwargs
        assert kwargs["topic"] == "discord.channel.6789.in"
        assert kwargs["correlation_id"] == "evt-1"
        assert kwargs["output_type"] is str
        # Full wire round-trips as a dep so the agent's gates can inspect it.
        assert kwargs["deps"]["discord"]["channel_id"] == 6789
        assert kwargs["deps"]["discord"]["slash_target"] == "scheduler"

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

        phonebook = client.invoke_node.call_args.kwargs["deps"]["phonebook"]
        assert isinstance(phonebook, list)
        ids = sorted(e["agent_id"] for e in phonebook)
        assert ids == ["scheduler", "scribe"]
        # Each entry carries the fields downstream needs.
        scribe = next(e for e in phonebook if e["agent_id"] == "scribe")
        assert scribe["display_name"] == "Scribe"
        assert "description" in scribe

    async def test_records_wire_in_pending_wires_before_invoke(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Consumer might fire before invoke_node returns — wire must be visible already."""
        observed: dict[str, WireMessage | None] = {}

        async def _capture(*_args: Any, **kwargs: Any) -> Any:
            entry = pending_wires.get(kwargs["correlation_id"])
            observed["wire"] = entry.wire if entry is not None else None
            return MagicMock()

        client.invoke_node.side_effect = _capture
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire()
        await ingress.handle(wire)

        assert observed["wire"] is wire

    async def test_pops_wire_when_invoke_raises(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        client.invoke_node.side_effect = RuntimeError("kafka down")
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire()

        with pytest.raises(RuntimeError):
            await ingress.handle(wire)

        assert pending_wires.get(wire.event_id) is None

    async def test_cancels_dispatcher_future_after_publish(
        self,
        pending_wires: PendingWires,
    ) -> None:
        """Cancellation pops the dispatcher's ``_pending`` entry so a no-reply
        event doesn't leak the future, and a redelivered correlation_id
        doesn't crash inside :meth:`_ReplyDispatcher.expect`."""
        client = MagicMock()
        captured: dict[str, Any] = {}

        async def _invoke(*_args: Any, **kwargs: Any) -> Any:
            handle = MagicMock()
            handle._future = asyncio.get_event_loop().create_future()
            captured["handle"] = handle
            return handle

        client.invoke_node = AsyncMock(side_effect=_invoke)

        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())

        assert captured["handle"]._future.cancelled()


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
        assert client.invoke_node.call_args.kwargs["model_settings"] == expected

    @pytest.mark.parametrize(
        ("effort", "expected_value"),
        [
            # Matches the operator → OpenAI mapping in
            # :mod:`calfkit_organization.agents.thinking`. The ramp
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
        assert client.invoke_node.call_args.kwargs["model_settings"] == {
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
        assert client.invoke_node.call_args.kwargs["model_settings"] == {}

    async def test_no_effort_in_definition_passes_none(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.invoke_node.call_args.kwargs["model_settings"] is None

    async def test_ambient_message_passes_no_model_settings(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """slash_target=None → bridge doesn't know the recipient → no
        override. Phase 4 ambient now goes through ``_invoke`` directly
        (via :func:`invoke_node_with_metadata`), which does not accept
        ``model_settings`` at all; the original intent (no per-call
        thinking override for ambient) is preserved by inspecting that
        the slash-path ``invoke_node`` was NOT called instead."""
        ingress = BridgeIngress(
            client, _registry(scheduler_effort="max"), pending_wires
        )
        await ingress.handle(_wire(slash_target=None, kind="message"))
        # Ambient skips the slash path; invoke_node not called.
        client.invoke_node.assert_not_called()
        # Ambient path used _invoke; verify no model_settings ever
        # showed up on its overrides (the helper would surface them
        # via OverridesState if any were set, but ambient has none).
        kwargs = client._invoke.await_args.kwargs
        assert kwargs.get("overrides") is None

    async def test_target_missing_from_registry_passes_none(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        with caplog.at_level(logging.ERROR):
            await ingress.handle(_wire(slash_target="ghost"))
        assert client.invoke_node.call_args.kwargs["model_settings"] is None
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
            "calfkit_organization.bridge.ingress.resolve_provider",
            _raise_value_error,
        )

        with caplog.at_level(logging.WARNING):
            await ingress.handle(_wire(slash_target="scheduler"))

        assert client.invoke_node.call_args.kwargs["model_settings"] is None
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
            slash="/scheduler",
            display_name="Aksel (Scheduler)",
            description="Calendar.",
            provider="anthropic",
        )
        md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

        registry = AgentRegistry.from_agents_dir(tmp_path)
        ingress = BridgeIngress(client, registry, pending_wires)

        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.invoke_node.call_args.kwargs["model_settings"] is None

        await registry.set_thinking_effort("scheduler", "high")
        await ingress.handle(_wire(event_id="evt-2", slash_target="scheduler"))
        assert client.invoke_node.call_args.kwargs["model_settings"] == {
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
                    slash="/scheduler",
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
                    slash="/scheduler",
                    display_name="Aksel",
                    description="Calendar.",
                    provider="anthropic",
                    tools=("calndar",),
                    system_prompt="x",
                ),
                AgentDefinition(
                    agent_id="scribe",
                    slash="/scribe",
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
                    slash="/scribe",
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

    def test_router_included_registry_passes_boot_validation(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Production registries include the built-in router (via
        ``AgentRegistry.from_agents_dir``'s auto-append); the boot
        validation loops at ``BridgeIngress.__init__`` iterate
        ``registry.all()``, which contains the router. A future
        refactor of ``resolve_provider`` that broke on the router's
        attributes (e.g. ``source_path=None``, empty ``tools``)
        would crash bridge boot in production while every previous
        test passed. Pin the contract: a router-included registry
        must construct cleanly."""
        from calfkit_organization.router.definition import (
            build_router_definition,
        )

        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scribe",
                    slash="/scribe",
                    display_name="Scribe",
                    description="Notes.",
                    provider="openai",
                    tools=("private_chat",),
                    system_prompt="x",
                ),
                build_router_definition(),
            ]
        )
        ingress = BridgeIngress(client, registry, pending_wires)
        assert ingress is not None


class TestTempInstructions:
    """Per-call ``temp_instructions`` carries the peer roster for A2A-enabled
    targets so the LLM knows which peers it can call via ``private_chat``."""

    async def test_no_instructions_when_target_lacks_private_chat(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Default ``_registry()`` agents have no tools; no roster injected."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        assert client.invoke_node.call_args.kwargs["temp_instructions"] is None

    async def test_ambient_messages_skip_slash_path(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Ambient (no slash_target) is now routed to the router's
        ambient ingress (Phase 4), not to a channel topic. The
        slash-path ``client.invoke_node`` MUST NOT be called for
        ambient — that would publish to the channel topic and bypass
        the router entirely. The router-roster ``temp_instructions``
        injection for ambient is covered by
        ``tests/bridge/test_ingress_router.py``."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(slash_target=None, kind="message"))
        client.invoke_node.assert_not_called()

    async def test_instructions_injected_when_target_has_private_chat(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """Target with ``private_chat`` in tools sees the peer roster as
        temp_instructions on this call. Built from the registry per-call so
        a future hot-add reaches the next invocation immediately."""
        registry = AgentRegistry(
            [
                AgentDefinition(
                    agent_id="scheduler",
                    slash="/scheduler",
                    display_name="Aksel (Scheduler)",
                    description="Calendar.",
                    provider="anthropic",
                    tools=("private_chat",),
                    system_prompt="x",
                ),
                AgentDefinition(
                    agent_id="scribe",
                    slash="/scribe",
                    display_name="Scribe",
                    description="Notes.",
                    provider="openai",
                    system_prompt="x",
                ),
            ]
        )
        ingress = BridgeIngress(client, registry, pending_wires)
        await ingress.handle(_wire(slash_target="scheduler"))
        instructions = client.invoke_node.call_args.kwargs["temp_instructions"]
        assert instructions is not None
        assert "scribe" in instructions
        assert "scheduler" not in instructions  # self excluded
