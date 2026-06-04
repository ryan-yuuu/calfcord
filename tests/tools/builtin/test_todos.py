"""Tests for the per-agent todo tools."""

from __future__ import annotations

import pytest
from calfkit.models import ToolContext

from calfcord.tools.builtin import todos


def _ctx(agent: str) -> ToolContext:
    return ToolContext(
        deps={},
        run_id="c",
        agent_name=agent,
    )


@pytest.fixture(autouse=True)
def _reset_executors() -> None:
    todos._reset_for_tests()
    yield
    todos._reset_for_tests()


class TestTodoWrite:
    async def test_creates_initial_list(self) -> None:
        result = await todos.todo_write(
            _ctx("alice"),
            [
                {"title": "first", "status": "todo"},
                {"title": "second", "notes": "extra", "status": "in_progress"},
            ],
        )
        assert "2 item" in result

    async def test_replaces_existing_list(self) -> None:
        await todos.todo_write(_ctx("alice"), [{"title": "old"}])
        result = await todos.todo_write(
            _ctx("alice"), [{"title": "new", "status": "done"}],
        )
        view = await todos.todo_view(_ctx("alice"))
        assert "new" in view
        assert "old" not in view
        assert "1 item" in result

    async def test_invalid_status_returns_error(self) -> None:
        result = await todos.todo_write(
            _ctx("alice"), [{"title": "x", "status": "bogus"}],
        )
        assert result.startswith("error:")
        # Nothing was written.
        view = await todos.todo_view(_ctx("alice"))
        assert "No task list" in view

    async def test_missing_title_returns_error(self) -> None:
        result = await todos.todo_write(_ctx("alice"), [{"notes": "no title"}])
        assert result.startswith("error:")


class TestTodoView:
    async def test_empty_list_yields_hint(self) -> None:
        result = await todos.todo_view(_ctx("alice"))
        assert "No task list" in result

    async def test_shows_status_icons(self) -> None:
        await todos.todo_write(
            _ctx("alice"),
            [
                {"title": "a", "status": "done"},
                {"title": "b", "status": "in_progress"},
                {"title": "c", "status": "todo"},
            ],
        )
        view = await todos.todo_view(_ctx("alice"))
        # The upstream formatter uses unicode icons; verify presence of
        # each task title at minimum.
        assert "a" in view and "b" in view and "c" in view


class TestPerAgentIsolation:
    async def test_alice_and_bob_have_separate_lists(self) -> None:
        await todos.todo_write(_ctx("alice"), [{"title": "alice task"}])
        await todos.todo_write(_ctx("bob"), [{"title": "bob task"}])
        a = await todos.todo_view(_ctx("alice"))
        b = await todos.todo_view(_ctx("bob"))
        assert "alice task" in a and "bob task" not in a
        assert "bob task" in b and "alice task" not in b

    async def test_missing_agent_name_raises_runtime_error(self) -> None:
        """``ctx.agent_name`` is normally populated by calfkit from the
        ``x-calf-emitter`` Kafka header. A missing value means the
        dispatch path was bypassed — an infrastructure bug, not a user
        condition. The project convention (see private_chat) is to
        raise RuntimeError on infra bugs so operators see the failure,
        rather than silently sharing one task list across "different"
        callers."""
        ctx = ToolContext(
            deps={},
            run_id="c",
            agent_name=None,
        )
        with pytest.raises(RuntimeError, match="agent_name"):
            await todos.todo_write(ctx, [{"title": "anon"}])
        with pytest.raises(RuntimeError, match="agent_name"):
            await todos.todo_view(ctx)
