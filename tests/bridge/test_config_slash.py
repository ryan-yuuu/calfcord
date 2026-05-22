"""Unit tests for the /thinking-effort operator slash command callback.

The Discord layer is exercised via the internal ``_on_thinking_effort``
entry point so we don't need a live ``app_commands.CommandTree`` or a
real ``discord.Interaction``. The fake interaction is a SimpleNamespace
extended with an ``AsyncMock`` for ``response.send_message``.

Each test writes its agent ``.md`` files to ``tmp_path`` so the
registry's ``set_thinking_effort`` path can rewrite real frontmatter.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest

from calfkit_organization.bridge.ingress import BridgeIngress
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.slash import (
    _THINKING_EFFORT_COMMAND_NAME,
    SlashCommandManager,
)


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
        "slash": f"/{agent_id}",
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
    """Two agents on disk: ``scribe`` (openai) and ``echo`` (no provider)."""
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


@pytest.fixture
def manager(agents_dir: Path) -> SlashCommandManager:
    """A SlashCommandManager backed by a real registry loaded from tmp_path."""
    registry = AgentRegistry.from_agents_dir(agents_dir)
    return SlashCommandManager(
        client=_fake_discord_client(),
        registry=registry,
        ingress=MagicMock(spec=BridgeIngress),
        slash_normalizer=MagicMock(),
        owner_user_id=_OWNER_USER_ID,
    )


class TestAuthorization:
    async def test_non_owner_is_rejected_no_write(
        self, manager: SlashCommandManager, agents_dir: Path
    ) -> None:
        original = (agents_dir / "scribe.md").read_text(encoding="utf-8")

        interaction = _interaction(user_id=_OWNER_USER_ID + 1)
        await manager._on_thinking_effort(interaction, "scribe", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "owner" in msg[0].lower()
        assert kwargs.get("ephemeral") is True

        # File unchanged.
        assert (agents_dir / "scribe.md").read_text(encoding="utf-8") == original

    async def test_owner_id_unset_permits_any_caller(self, agents_dir: Path) -> None:
        """When ``owner_user_id`` is None, the slash is open to anyone."""
        registry = AgentRegistry.from_agents_dir(agents_dir)
        manager = SlashCommandManager(
            client=_fake_discord_client(),
            registry=registry,
            ingress=MagicMock(spec=BridgeIngress),
            slash_normalizer=MagicMock(),
            owner_user_id=None,
        )
        interaction = _interaction(user_id=123456)
        await manager._on_thinking_effort(interaction, "scribe", "low")

        assert registry.by_id("scribe").thinking_effort == "low"


class TestPersistence:
    async def test_writes_thinking_effort_to_frontmatter(
        self, manager: SlashCommandManager, agents_dir: Path
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        reloaded = frontmatter.load(agents_dir / "scribe.md")
        assert reloaded.metadata["thinking_effort"] == "high"

    async def test_swaps_in_memory_definition(
        self, manager: SlashCommandManager
    ) -> None:
        assert manager._registry.by_id("scribe").thinking_effort is None

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        assert manager._registry.by_id("scribe").thinking_effort == "high"

    async def test_overwrites_existing_value(
        self, agents_dir: Path
    ) -> None:
        # Pre-existing tier in the file.
        _write_agent_md(agents_dir, agent_id="scribe", provider="openai", thinking_effort="low")
        registry = AgentRegistry.from_agents_dir(agents_dir)
        manager = SlashCommandManager(
            client=_fake_discord_client(),
            registry=registry,
            ingress=MagicMock(spec=BridgeIngress),
            slash_normalizer=MagicMock(),
            owner_user_id=_OWNER_USER_ID,
        )

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "max")

        assert registry.by_id("scribe").thinking_effort == "max"
        reloaded = frontmatter.load(agents_dir / "scribe.md")
        assert reloaded.metadata["thinking_effort"] == "max"

    async def test_preserves_other_frontmatter_fields(
        self, manager: SlashCommandManager, agents_dir: Path
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "medium")

        reloaded = frontmatter.load(agents_dir / "scribe.md")
        assert reloaded.metadata["name"] == "scribe"
        assert reloaded.metadata["slash"] == "/scribe"
        assert reloaded.metadata["provider"] == "openai"
        assert reloaded.content.strip() == "System prompt body."


class TestErrorPaths:
    async def test_unknown_agent_replies_ephemeral_no_write(
        self, manager: SlashCommandManager, agents_dir: Path
    ) -> None:
        original = (agents_dir / "scribe.md").read_text(encoding="utf-8")

        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "ghost", "high")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "ghost" in msg[0]
        assert kwargs.get("ephemeral") is True
        assert (agents_dir / "scribe.md").read_text(encoding="utf-8") == original

    async def test_unknown_effort_replies_ephemeral(
        self, manager: SlashCommandManager
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "bananas")

        interaction.response.send_message.assert_awaited_once()
        msg, kwargs = interaction.response.send_message.call_args
        assert "bananas" in msg[0].lower() or "unknown effort" in msg[0].lower()
        assert kwargs.get("ephemeral") is True
        assert manager._registry.by_id("scribe").thinking_effort is None

    async def test_missing_md_file_replies_with_internal_error(
        self,
        manager: SlashCommandManager,
        agents_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Delete the .md AFTER the registry has indexed it — the slash
        # path then tries to rewrite a file that vanished.
        (agents_dir / "scribe.md").unlink()

        interaction = _interaction()
        with caplog.at_level("ERROR"):
            await manager._on_thinking_effort(interaction, "scribe", "high")

        msg = interaction.response.send_message.call_args[0][0]
        assert "missing" in msg.lower() or "internal error" in msg.lower()

    async def test_rewrite_oserror_replies_apologetically_with_id(
        self,
        manager: SlashCommandManager,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Disk-full / permission denied during rewrite: filesystem-error
        reply with interaction id.

        Patches the registry's bound reference (not just the module
        attribute) so the real ``registry.set_thinking_effort`` control
        flow is exercised — including the lock acquire/release and the
        index not being touched on failure.
        """

        def _raise_oserror(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("simulated disk full")

        monkeypatch.setattr(
            "calfkit_organization.bridge.registry.update_thinking_effort",
            _raise_oserror,
        )

        interaction = _interaction()
        with caplog.at_level("ERROR"):
            await manager._on_thinking_effort(interaction, "scribe", "high")

        msg, kwargs = interaction.response.send_message.call_args
        assert "filesystem error" in msg[0].lower()
        assert str(interaction.id) in msg[0]
        assert kwargs.get("ephemeral") is True
        # In-memory definition stays at the pre-error value because
        # registry._replace runs only after a successful write.
        assert manager._registry.by_id("scribe").thinking_effort is None

    async def test_rewrite_validation_error_replies_invalid_frontmatter(
        self,
        manager: SlashCommandManager,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Existing .md has malformed YAML / fails validation: invalid-frontmatter reply."""

        def _raise_value_error(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("simulated malformed frontmatter")

        monkeypatch.setattr(
            "calfkit_organization.bridge.registry.update_thinking_effort",
            _raise_value_error,
        )

        interaction = _interaction()
        with caplog.at_level("ERROR"):
            await manager._on_thinking_effort(interaction, "scribe", "high")

        msg, kwargs = interaction.response.send_message.call_args
        assert "frontmatter is invalid" in msg[0].lower()
        assert str(interaction.id) in msg[0]
        assert kwargs.get("ephemeral") is True


class TestReplyText:
    async def test_success_reply_mentions_agent_and_effort(
        self, manager: SlashCommandManager
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        msg = interaction.response.send_message.call_args[0][0]
        assert "scribe" in msg
        assert "high" in msg
        assert "next" in msg.lower()  # informs about take-effect timing

    async def test_none_effort_reply_says_disabled(
        self, manager: SlashCommandManager
    ) -> None:
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "none")

        msg = interaction.response.send_message.call_args[0][0]
        assert "disabled" in msg.lower()

    async def test_reply_mentions_restart_for_ambient(
        self, manager: SlashCommandManager
    ) -> None:
        """The reply should remind operators that ambient messages need a restart."""
        interaction = _interaction()
        await manager._on_thinking_effort(interaction, "scribe", "high")

        msg = interaction.response.send_message.call_args[0][0]
        assert "restart" in msg.lower() and "ambient" in msg.lower()


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
        """The built-in router has ``source_path=None`` so the
        ``set_thinking_effort`` rewrite path would raise on selection.
        It must not appear in the slash UI's choice list."""
        # The manager fixture uses `from_agents_dir`, which auto-appends
        # the built-in router. Verify it's in the registry but NOT in
        # the choice list.
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
