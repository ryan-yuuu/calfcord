"""Tests for :mod:`calfcord.mcp.schema_build`.

Covers selector → schema-only-node resolution (the agent-side workhorse),
the unknown-server / unknown-tool error contract shared with
``validate_mcp_references``, and a topic-parity test that locks the
agent-built schema-only node's wire topics to those a real (un-opened)
``McpServer`` would produce — the agent↔bridge wire agreement.
"""

from __future__ import annotations

import pytest
from calfkit.mcp import McpServer, McpToolDef

from calfcord.mcp.schema_build import (
    resolve_mcp_selectors,
    schema_only_server,
    validate_mcp_references,
)


def _catalog() -> dict[str, list[McpToolDef]]:
    return {
        "demo": [McpToolDef(name="echo"), McpToolDef(name="get-x")],
        "gmail": [McpToolDef(name="search"), McpToolDef(name="send")],
    }


class TestResolveBareServer:
    def test_bare_server_expands_to_all_tools(self) -> None:
        nodes = resolve_mcp_selectors(["mcp/demo"], _catalog())
        names = {n.tool_schema.name for n in nodes}
        assert names == {"demo_echo", "demo_get-x"}
        # Exactly the server's two tools — no extras, no duplicates.
        assert len(nodes) == 2

    def test_topics_use_original_tool_name(self) -> None:
        """LLM-facing name is flattened (``demo_echo``) but the wire topics
        keep the *original* tool name (``mcp.demo.echo.{input,output}``)."""
        nodes = resolve_mcp_selectors(["mcp/demo"], _catalog())
        by_name = {n.tool_schema.name: n for n in nodes}
        assert by_name["demo_echo"].subscribe_topics == ["mcp.demo.echo.input"]
        assert by_name["demo_echo"].publish_topic == "mcp.demo.echo.output"
        # Hyphenated original tool name is preserved verbatim in the topic.
        assert by_name["demo_get-x"].subscribe_topics == ["mcp.demo.get-x.input"]
        assert by_name["demo_get-x"].publish_topic == "mcp.demo.get-x.output"


class TestResolveSingleTool:
    def test_single_tool_selector_yields_only_that_tool(self) -> None:
        nodes = resolve_mcp_selectors(["mcp/demo/echo"], _catalog())
        assert {n.tool_schema.name for n in nodes} == {"demo_echo"}

    def test_dedupes_bare_and_explicit_overlap(self) -> None:
        """A bare server plus an explicit tool of it collapses (no duplicate
        ``demo_echo`` node)."""
        nodes = resolve_mcp_selectors(["mcp/demo", "mcp/demo/echo"], _catalog())
        names = sorted(n.tool_schema.name for n in nodes)
        assert names == ["demo_echo", "demo_get-x"]


class TestCrossServerOrdering:
    """The emitted node list is deterministic: servers are processed in
    sorted order regardless of selector input order (and tools within a
    server are sorted by ``_group_selected_tools``)."""

    def test_servers_emitted_in_sorted_order_regardless_of_input(self) -> None:
        catalog = {
            "alpha": [McpToolDef(name="a")],
            "zeta": [McpToolDef(name="z")],
        }
        # Selectors given in REVERSE-alpha order; nodes must still come out
        # sorted by server name (alpha before zeta).
        nodes = resolve_mcp_selectors(["mcp/zeta", "mcp/alpha"], catalog)
        names = [n.tool_schema.name for n in nodes]
        assert names == ["alpha_a", "zeta_z"]


class TestUnknownReferences:
    def test_unknown_server_raises_and_lists_known(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            resolve_mcp_selectors(["mcp/nope"], _catalog())
        msg = str(excinfo.value)
        assert "nope" in msg
        # Alternatives listed so the operator can fix a typo without grepping.
        assert "demo" in msg and "gmail" in msg

    def test_unknown_tool_raises_and_lists_available(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            resolve_mcp_selectors(["mcp/demo/missing"], _catalog())
        msg = str(excinfo.value)
        assert "missing" in msg
        assert "echo" in msg and "get-x" in msg


class TestValidateMcpReferences:
    def test_passes_for_valid_selectors(self) -> None:
        assert validate_mcp_references(["mcp/demo", "mcp/gmail/search"], _catalog()) is None

    def test_raises_on_unknown_server(self) -> None:
        with pytest.raises(ValueError, match="nope"):
            validate_mcp_references(["mcp/nope"], _catalog())

    def test_raises_on_unknown_tool(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            validate_mcp_references(["mcp/demo/missing"], _catalog())

    def test_builds_no_nodes(self) -> None:
        """Validation-only: it shares ``_group_selected_tools`` with the
        resolver but constructs nothing — a valid pass returns ``None``."""
        assert validate_mcp_references([], _catalog()) is None


class TestTopicParity:
    """Lock the agent↔bridge wire agreement: the schema-only node the agent
    builds and the node a real (credentialed, but here un-opened)
    :class:`McpServer` would emit must carry IDENTICAL ``subscribe_topics``
    and ``publish_topic`` per tool. If these ever drift, an agent would
    publish ``Call`` envelopes to a topic the bridge's server doesn't
    consume (or vice versa), silently breaking every MCP tool call.
    """

    def test_schema_only_matches_real_server_topics(self) -> None:
        defs = [McpToolDef(name="echo"), McpToolDef(name="get-x")]

        # Agent path: transport-free, never opened.
        agent_server = schema_only_server("demo", defs)
        agent_by_orig = {n.tool_schema.name: n for n in agent_server}

        # Bridge path: a REAL stdio server with the same tools and explicit
        # name. We never ``open()`` it — only iterate it for schemas — so no
        # subprocess spawns and no ``$VAR`` is expanded.
        bridge_server = McpServer.stdio("npx", "-y", "x", tools=defs, name="demo")
        bridge_by_orig = {n.tool_schema.name: n for n in bridge_server}

        assert set(agent_by_orig) == set(bridge_by_orig) == {"echo", "get-x"}
        for orig in agent_by_orig:
            assert agent_by_orig[orig].subscribe_topics == bridge_by_orig[orig].subscribe_topics
            assert agent_by_orig[orig].publish_topic == bridge_by_orig[orig].publish_topic
        # And concretely the expected topic shape.
        assert agent_by_orig["echo"].subscribe_topics == ["mcp.demo.echo.input"]
        assert agent_by_orig["echo"].publish_topic == "mcp.demo.echo.output"
