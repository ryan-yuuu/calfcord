"""Unit tests for the /thinking-effort operator slash command callback.

The Discord layer is exercised via the internal ``_on_thinking_effort``
entry point so we don't need a live ``app_commands.CommandTree`` or a
real ``discord.Interaction``. The fake interaction is a SimpleNamespace
extended with an ``AsyncMock`` for ``response.send_message``.

After PR 3, ``/thinking-effort`` is fire-and-forget: the bridge
optimistically updates its in-memory registry via
``apply_local_thinking_effort_override`` and publishes a
:class:`SetThinkingEffortOp` to the agent's control topic. No disk I/O
is performed here — the agent rewrites its own ``.md`` asynchronously.

A fake calfkit ``Client`` records ``_connection.publish`` calls so we
can assert the published topic and payload (same pattern as
``tests/control_plane/test_publish.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import frontmatter
import pytest

from calfkit_organization.bridge.history import CLEAR_MARKER_TEXT
from calfkit_organization.bridge.ingress import BridgeIngress
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.slash import (
    _CLEAR_COMMAND_NAME,
    _THINKING_EFFORT_COMMAND_NAME,
    SlashCommandManager,
)
from calfkit_organization.control_plane.schema import (
    AgentControlEnvelope,
    SetThinkingEffortOp,
)
from calfkit_organization.control_plane.topics import control_topic_for

_OWNER_USER_ID = 9999


def _write_agent_md(
    dir_: Path,
    *,
    agent_id: str,
    provider: str = "openai",
    thinking_effort: str | None = None,
) -> Path:
    """Write a minimal valid .md file and return its path."""
    meta = {
        "name": agent_id,
        "display_name": agent_id.capitalize(),
        "description": f"Test agent {agent_id}.",
        "provider": provider,
    }
    if thinking_effort is not None:
        meta["thinking_effort"] = thinking_effort
    post = frontmatter.Post("System prompt body.", **meta)
    path = dir_ / f"{agent_id}.md"
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """Two agents on disk: ``scribe`` (openai) and ``echo`` (anthropic).

    The registry loads from this directory so we get realistic
    :class:`AgentDefinition` instances; the bridge slash path no longer
    touches these files but the loader is still the most convenient way
    to populate a registry for tests.
    """
    _write_agent_md(tmp_path, agent_id="scribe", provider="openai")
    _write_agent_md(tmp_path, agent_id="echo", provider="anthropic")
    return tmp_path


def _fake_discord_client() -> MagicMock:
    """A discord.Client mock that doesn't trip CommandTree's duplicate-tree guard."""
    client = MagicMock()
    client._connection._command_tree = None
    return client


def _interaction(*, user_id: int = _OWNER_USER_ID) -> Any:
    """Build a fake Discord interaction with an AsyncMock response."""
    response = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=user_id, name="alice", display_name="alice")
    return SimpleNamespace(id=42, user=user, response=response)


def _clear_interaction(*, user_id: int = _OWNER_USER_ID, channel: Any = None) -> Any:
    """A fake interaction for /clear — like ``_interaction`` plus a ``channel``."""
    response = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=user_id, name="alice", display_name="alice")
    return SimpleNamespace(id=42, user=user, response=response, channel=channel)


def _httpexception(status: int = 500) -> discord.HTTPException:
    """Build a discord.HTTPException with the given HTTP status."""
    response = SimpleNamespace(status=status, reason="x")
    return discord.HTTPException(response, "synthetic")  # type: ignore[arg-type]


class _FakeConnection:
    """Records each publish call so tests can assert topic + payload."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(
        self, payload: str, *, topic: str, key: bytes | None = None
    ) -> None:
        self.calls.append({"topic": topic, "payload": payload, "key": key})


class _FakeCalfkitClient:
    """A stand-in for ``calfkit.client.Client`` exposing only the bits
    the publish helpers reach into."""

    def __init__(self) -> None:
        self._connection = _FakeConnection()


def _make_manager(
    agents_dir: Path,
    *,
    owner_user_id: int | None = _OWNER_USER_ID,
    calfkit_client: _FakeCalfkitClient | None = None,
    guild_id: int | None = None,
) -> tuple[SlashCommandManager, _FakeCalfkitClient]:
    registry = AgentRegistry.from_agents_dir(agents_dir)
    client = calfkit_client or _FakeCalfkitClient()
    manager = SlashCommandManager(
        client=_fake_discord_client(),
        registry=registry,
        ingress=MagicMock(spec=BridgeIngress),
        slash_normalizer=MagicMock(),
        calfkit_client=client,  # type: ignore[arg-type]
        owner_user_id=owner_user_id,
        guild_id=guild_id,
    )
    return manager, client


@pytest.fixture
def manager_and_client(agents_dir: Path) -> tuple[SlashCommandManager, _FakeCalfkitClient]:
    return _make_manager(agents_dir)


@pytest.fixture
def manager(
    manager_and_client: tuple[SlashCommandManager, _FakeCalfkitClient],
) -> SlashCommandManager:
    return manager_and_client[0]


@pytest.fixture
def calfkit_client(
    manager_and_client: tuple[SlashCommandManager, _FakeCalfkitClient],
) -> _FakeCalfkitClient:
    return manager_and_client[1]


class TestAuthorization:
    async def test_non_owner_is_rejected_no_publish(
        self,
        manager: SlashCommandManager,
        calfkit_client: _FakeCalfkitClient,
    ) -> None:
        interaction = _interaction(user_id=_OWNER_USER_ID + 1)
        await manager._on_thinking_effort(interaction, "scribe", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "owner" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        # No control command was published, and the registry wasn't
        # optimistically updated.
        assert calfkit_client._connection.calls == []
        assert manager._registry.by_id("scribe").thinking_effort is None

    async def test_owner_id_unset_permits_any_caller(self, agents_dir: Path) -> None:
        """When ``owner_user_id`` is None, the slash is open to anyone."""
        manager, calfkit_client = _make_manager(agents_dir, owner_user_id=None)
        interaction = _interaction(user_id=123456)
        await manager._on_thinking_effort(interaction, "scribe", "low")

        # Optimistic update happened and the control command went out.
        assert manager._registry.by_id("scribe").thinking_effort == "low"
        assert len(calfkit_client._connection.calls) == 1


class TestOptimisticUpdate:
    async def test_swaps_in_memory_definition(
        self, manager: SlashCommandManager
    ) -> None:
        assert manager._registry.by_id("scribe").thinking_effort is None

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        assert manager._registry.by_id("scribe").thinking_effort == "high"

    async def test_does_not_touch_disk(
        self,
        manager: SlashCommandManager,
        agents_dir: Path,
    ) -> None:
        """The bridge no longer rewrites ``.md`` files; the agent does
        it asynchronously after applying the control command."""
        original = (agents_dir / "scribe.md").read_text(encoding="utf-8")

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        assert (agents_dir / "scribe.md").read_text(encoding="utf-8") == original

    async def test_overwrites_existing_value(
        self, agents_dir: Path
    ) -> None:
        # Pre-existing tier on the in-memory definition (loaded from
        # the pre-written .md).
        _write_agent_md(
            agents_dir, agent_id="scribe", provider="openai", thinking_effort="low"
        )
        manager, _ = _make_manager(agents_dir)

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "max")

        assert manager._registry.by_id("scribe").thinking_effort == "max"


class TestControlCommandPublish:
    async def test_publishes_set_thinking_effort_op_to_agent_topic(
        self,
        manager: SlashCommandManager,
        calfkit_client: _FakeCalfkitClient,
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        assert len(calfkit_client._connection.calls) == 1
        call = calfkit_client._connection.calls[0]
        assert call["topic"] == control_topic_for("scribe")

        envelope = AgentControlEnvelope.model_validate(call["payload"])
        assert isinstance(envelope.command, SetThinkingEffortOp)
        assert envelope.command.agent_id == "scribe"
        assert envelope.command.value == "high"
        assert envelope.command.issued_by == str(_OWNER_USER_ID)
        # request_id is a UUID4 string; assert it's non-empty (the
        # success reply checked below echoes the same id).
        assert envelope.command.request_id

    async def test_publish_failure_replies_with_error(
        self,
        manager: SlashCommandManager,
        calfkit_client: _FakeCalfkitClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Break the publish path.
        async def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("simulated broker failure")

        calfkit_client._connection.publish = _boom  # type: ignore[assignment]

        interaction = _interaction()
        with caplog.at_level("ERROR"):
            await manager._on_thinking_effort(interaction, "scribe", "high")

        msg, kwargs = interaction.response.send_message.call_args
        assert "couldn't publish" in msg[0].lower()
        assert "request_id" in msg[0].lower()
        assert kwargs.get("ephemeral") is True


class TestErrorPaths:
    async def test_unknown_agent_replies_ephemeral_no_publish(
        self,
        manager: SlashCommandManager,
        calfkit_client: _FakeCalfkitClient,
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "ghost", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "ghost" in msg[0]
        assert kwargs.get("ephemeral") is True
        assert calfkit_client._connection.calls == []

    async def test_unknown_effort_replies_ephemeral_no_publish(
        self,
        manager: SlashCommandManager,
        calfkit_client: _FakeCalfkitClient,
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "bananas")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "bananas" in msg[0].lower() or "unknown effort" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        assert manager._registry.by_id("scribe").thinking_effort is None
        assert calfkit_client._connection.calls == []


class TestReplyText:
    async def test_success_reply_mentions_fire_and_forget_and_request_id(
        self,
        manager: SlashCommandManager,
        calfkit_client: _FakeCalfkitClient,
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        msg = interaction.response.send_message.call_args[0][0]
        assert "scribe" in msg
        assert "high" in msg
        assert "fire-and-forget" in msg.lower()
        # The reply echoes the same request_id the envelope carries.
        envelope = AgentControlEnvelope.model_validate(
            calfkit_client._connection.calls[0]["payload"]
        )
        assert isinstance(envelope.command, SetThinkingEffortOp)
        assert envelope.command.request_id in msg


class TestRegister:
    def test_register_adds_thinking_effort_command_to_tree(
        self, manager: SlashCommandManager
    ) -> None:
        """The happy path: registration adds a single ``thinking-effort`` command."""
        manager.register_thinking_effort()
        cmd = manager._tree.get_command(_THINKING_EFFORT_COMMAND_NAME)
        assert cmd is not None
        assert cmd.name == _THINKING_EFFORT_COMMAND_NAME

    def test_thinking_effort_choices_exclude_router(
        self, manager: SlashCommandManager
    ) -> None:
        """The built-in router is project infrastructure, not a
        user-invocable agent; it must not appear in the slash UI's
        choice list."""
        agent_ids = {spec.agent_id for spec in manager._registry.all()}
        assert "_router" in agent_ids  # registry has it
        command = manager._build_thinking_effort_command()
        # The 'agent' parameter's choices come from the `agent_choices`
        # local. discord.py stores them on the param descriptor.
        agent_param = command._params["agent"]
        choice_ids = {c.value for c in agent_param.choices}
        assert "_router" not in choice_ids
        assert "scribe" in choice_ids
        assert "echo" in choice_ids


class TestScheduleResync:
    async def test_schedule_resync_coalesces(
        self,
        manager: SlashCommandManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multiple ``schedule_resync`` calls within the debounce window
        create exactly one resync task."""
        manager._resync_debounce_s = 0.05

        # Stub out the Discord-side work so the task body succeeds
        # without a real CommandTree / network sync.
        manager._tree.remove_command = MagicMock()  # type: ignore[method-assign]
        manager._tree.add_command = MagicMock()  # type: ignore[method-assign]

        async def _fake_sync(_guild_id: int | None) -> None:
            return None

        monkeypatch.setattr(manager, "sync", _fake_sync)

        manager.schedule_resync("scribe")
        first_task = manager._resync_task
        assert first_task is not None
        # Subsequent calls before the debounce fires reuse the same task.
        manager.schedule_resync("echo")
        manager.schedule_resync("ghost")
        assert manager._resync_task is first_task

        await first_task

        # remove_command + add_command + sync each fired exactly once.
        assert manager._tree.remove_command.call_count == 1
        assert manager._tree.add_command.call_count == 1

    async def test_debounced_resync_rebuilds_slash(
        self,
        manager: SlashCommandManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manager._resync_debounce_s = 0.01

        remove_calls = MagicMock()
        add_calls = MagicMock()
        manager._tree.remove_command = remove_calls  # type: ignore[method-assign]
        manager._tree.add_command = add_calls  # type: ignore[method-assign]

        sync_calls: list[int | None] = []

        async def _fake_sync(guild_id: int | None) -> None:
            sync_calls.append(guild_id)

        monkeypatch.setattr(manager, "sync", _fake_sync)

        manager.schedule_resync("scribe")
        task = manager._resync_task
        assert task is not None
        await task

        remove_calls.assert_called_once_with(_THINKING_EFFORT_COMMAND_NAME)
        add_calls.assert_called_once()
        assert sync_calls == [manager._guild_id]

    async def test_event_during_inflight_sync_chains_followup_cycle(
        self,
        manager: SlashCommandManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression test for H2: an event that arrives DURING an
        in-flight sync (after the debounce sleep, before sync() returns)
        used to be silently dropped because ``_resync_task.done()`` was
        False and the leading-edge guard short-circuited. The trailing-
        edge debounce sets ``_resync_pending`` and chains a follow-up
        cycle from the finally block.
        """
        # No debounce sleep so the test doesn't pay an extra second.
        manager._resync_debounce_s = 0.0

        remove_calls = MagicMock()
        add_calls = MagicMock()
        manager._tree.remove_command = remove_calls  # type: ignore[method-assign]
        manager._tree.add_command = add_calls  # type: ignore[method-assign]

        sync_started = asyncio.Event()
        sync_release = asyncio.Event()
        sync_count = 0

        async def _hanging_sync(_guild_id: int | None) -> None:
            nonlocal sync_count
            sync_count += 1
            sync_started.set()
            await sync_release.wait()
            sync_release.clear()
            sync_started.clear()

        monkeypatch.setattr(manager, "sync", _hanging_sync)

        # 1. Kick off the first resync. It'll skip the (zero-second)
        #    sleep and block on _hanging_sync.
        manager.schedule_resync("scribe")
        first_task = manager._resync_task
        assert first_task is not None
        await sync_started.wait()

        # 2. While the first sync is mid-flight, simulate a new agent
        #    arriving. With the OLD leading-edge debounce, this call
        #    would be a silent no-op; with the trailing-edge fix it
        #    sets ``_resync_pending`` so a follow-up cycle chains.
        manager.schedule_resync("echo")
        assert manager._resync_pending is True

        # 3. Release the first sync. Its finally block observes
        #    _resync_pending and schedules a follow-up task.
        sync_release.set()
        await first_task

        followup_task = manager._resync_task
        assert followup_task is not None
        assert followup_task is not first_task
        assert manager._resync_pending is False

        # 4. Run the follow-up cycle to completion.
        await sync_started.wait()
        sync_release.set()
        await followup_task

        # Two distinct sync cycles fired — the "echo" event was not
        # dropped.
        assert sync_count == 2
        assert remove_calls.call_count == 2
        assert add_calls.call_count == 2


class TestClear:
    """The /clear operator slash: owner-gated, posts the per-channel marker."""

    async def test_non_owner_rejected_no_marker(
        self, manager: SlashCommandManager
    ) -> None:
        channel = SimpleNamespace(id=12345, send=AsyncMock())
        interaction = _clear_interaction(user_id=_OWNER_USER_ID + 1, channel=channel)
        await manager._on_clear(interaction)

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "owner" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        channel.send.assert_not_awaited()

    async def test_owner_posts_marker_and_confirms(
        self, manager: SlashCommandManager
    ) -> None:
        channel = SimpleNamespace(id=12345, send=AsyncMock())
        interaction = _clear_interaction(channel=channel)
        await manager._on_clear(interaction)

        channel.send.assert_awaited_once_with(CLEAR_MARKER_TEXT)
        # Exactly one interaction response — no double-reply on the
        # success path (the failure branch must not also fire).
        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "cleared" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_owner_unset_permits_any_caller(self, agents_dir: Path) -> None:
        """When ``owner_user_id`` is None, /clear is open to anyone."""
        manager, _ = _make_manager(agents_dir, owner_user_id=None)
        channel = SimpleNamespace(id=12345, send=AsyncMock())
        interaction = _clear_interaction(user_id=123456, channel=channel)
        await manager._on_clear(interaction)

        channel.send.assert_awaited_once_with(CLEAR_MARKER_TEXT)

    async def test_marker_send_failure_reports_not_cleared(
        self, manager: SlashCommandManager
    ) -> None:
        channel = SimpleNamespace(
            id=12345, send=AsyncMock(side_effect=_httpexception())
        )
        interaction = _clear_interaction(channel=channel)
        await manager._on_clear(interaction)

        channel.send.assert_awaited_once()
        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "not cleared" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_marker_send_unexpected_error_reports_not_cleared(
        self, manager: SlashCommandManager
    ) -> None:
        """A non-HTTPException from channel.send (e.g. a connector error)
        must still surface 'not cleared' rather than escape into the
        command dispatcher as a generic 'did not respond'."""
        channel = SimpleNamespace(
            id=12345, send=AsyncMock(side_effect=RuntimeError("connector died"))
        )
        interaction = _clear_interaction(channel=channel)
        await manager._on_clear(interaction)

        channel.send.assert_awaited_once()
        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "not cleared" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

    async def test_no_channel_replies_ephemeral_no_send(
        self, manager: SlashCommandManager
    ) -> None:
        interaction = _clear_interaction(channel=None)
        await manager._on_clear(interaction)

        interaction.response.send_message.assert_awaited_once()
        _msg, kwargs = interaction.response.send_message.call_args
        assert "channel" in _msg[0].lower()
        assert kwargs.get("ephemeral") is True

    def test_register_adds_clear_command_to_tree(
        self, manager: SlashCommandManager
    ) -> None:
        manager.register_clear()
        cmd = manager._tree.get_command(_CLEAR_COMMAND_NAME)
        assert cmd is not None
        assert cmd.name == _CLEAR_COMMAND_NAME
        # Unlike /thinking-effort, /clear takes no parameters.
        assert cmd._params == {}
