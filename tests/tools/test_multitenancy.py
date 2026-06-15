"""Multi-tenancy: agents sharing a stateful tool node don't leak state.

The vendored hermes nodes key all per-session state by the calling agent's
identity — ``ctx.agent_name``, stamped by calfkit from the unspoofable
``x-calf-emitter`` Kafka header — so one agent's shell session, working
directory, files-in-flight, and task list are invisible to another.

These tests exercise the requirement through calfcord's *composed* registry
node (``TOOL_REGISTRY["todo"]``), not a freshly imported one, so a future
change to the composition or wiring that dropped per-agent isolation would
fail here. ``todo`` is used as the representative stateful node: it is pure
in-memory (no subprocess/timing), and it shares the exact ``session_key``
derivation (``f"{agent_name}:{deps.get('session_id','default')}"``) that the
terminal, process, files, and execute_code nodes use, so its isolation
behavior is theirs too.
"""

from __future__ import annotations

import pytest
from calfkit.models import ToolContext
from calfkit_tools.hermes.node import InMemoryTodoStore

from calfcord.tools import TOOL_REGISTRY

# The composed registry node's underlying callable (its tool body).
_TODO_BODY = TOOL_REGISTRY["todo"]._tool.function


def _ctx(agent_name: str | None, store: InMemoryTodoStore) -> ToolContext:
    """A ToolContext as calfkit builds one for a tool call: agent identity
    from the emitter header, plus the node's worker-lifetime ``todo_state``
    resource shared across all callers (one store, many tenants)."""
    return ToolContext(
        agent_name=agent_name, deps={}, resources={"todo_state": store}
    )


def _items(content: str) -> list[dict]:
    return [{"id": "1", "content": content, "status": "pending"}]


class TestStatefulToolIsolationAcrossAgents:
    def test_one_agents_state_is_invisible_to_another(self) -> None:
        # A single store instance stands in for the worker-lifetime resource
        # shared by every agent that calls the one hosted todo node.
        store = InMemoryTodoStore()

        # Agent A writes a private task list.
        _TODO_BODY(_ctx("agent_a", store), todos=_items("agent-a-secret"))

        # Agent B, hitting the SAME node/store, must see nothing of A's.
        b_view = _TODO_BODY(_ctx("agent_b", store), todos=None)
        assert b_view["todos"] == []

        # Agent A still sees its own list — isolation, not erasure.
        a_view = _TODO_BODY(_ctx("agent_a", store), todos=None)
        assert [t["content"] for t in a_view["todos"]] == ["agent-a-secret"]

    def test_each_agent_keeps_its_own_independent_state(self) -> None:
        store = InMemoryTodoStore()
        _TODO_BODY(_ctx("agent_a", store), todos=_items("a-task"))
        _TODO_BODY(_ctx("agent_b", store), todos=_items("b-task"))

        a_view = _TODO_BODY(_ctx("agent_a", store), todos=None)
        b_view = _TODO_BODY(_ctx("agent_b", store), todos=None)
        assert [t["content"] for t in a_view["todos"]] == ["a-task"]
        assert [t["content"] for t in b_view["todos"]] == ["b-task"]

    def test_missing_agent_identity_fails_closed(self) -> None:
        """An unstamped caller (no ``agent_name``) must NOT be silently
        merged into a shared tenancy bucket — the node raises instead."""
        store = InMemoryTodoStore()
        with pytest.raises(ValueError, match="agent_name"):
            _TODO_BODY(_ctx(None, store), todos=None)


class TestTerminalSessionIsolation:
    """The terminal is the highest-blast-radius tool: a shared shell session
    would leak one agent's working directory, environment, and processes into
    another's. This drives a real shell through the composed ``terminal`` node
    to prove the session is per-agent. (Spawns a subprocess; POSIX-only.)"""

    def test_working_directory_does_not_leak_across_agents(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        terminal = TOOL_REGISTRY["terminal"]._tool.function

        def ctx(agent_name: str) -> ToolContext:
            return ToolContext(agent_name=agent_name, deps={}, resources={})

        # Agent A changes its session's working directory to root.
        terminal(ctx("agent_a"), command="cd /")
        a_pwd = terminal(ctx("agent_a"), command="pwd")["output"].strip()
        # Agent B, calling the SAME terminal node, must not inherit A's cd.
        b_pwd = terminal(ctx("agent_b"), command="pwd")["output"].strip()

        assert a_pwd == "/"  # A's `cd` persisted within A's own session
        assert b_pwd != "/"  # B is isolated — still in its own session cwd
