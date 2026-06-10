"""Unit tests for :mod:`calfcord.mcp.agent_select` — the agent-path selector.

Agents resolve ``mcp/...`` frontmatter entries through calfkit's public
:class:`~calfkit.mcp.MCPToolboxRef` — an identity-only handle constructible
with just the server name, so distributed agent hosts never need
``mcp.json``. The contract pins below are deliberate: calfcord's secrets
boundary and degradation policy ride on this upstream behavior, so drift
in any of it must fail loudly here rather than silently in production.

Pinned:

* protocol compliance — calfkit's ``split_tool_declarations`` must classify
  the ref as deferred (that classification is what makes ``Worker``
  auto-register the capability view);
* view resolution — ``include`` scoping, non-strict-by-default policy,
  missing-server degradation;
* grouping — ``selectors_from_entries`` merges an agent's ``mcp/...``
  frontmatter entries into one ref per server with the old schema-build
  semantics (bare form subsumes explicit; dedup; sorted). This part is
  calfcord semantics, not calfkit's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from calfkit.mcp import MCPToolboxRef
from calfkit.models.capability import CapabilityRecord, CapabilityToolDef
from calfkit.models.tool_dispatch import ToolSelector, split_tool_declarations

from calfcord.mcp.agent_select import selectors_from_entries


def _record(server: str = "gmail", tools: tuple[str, ...] = ("search", "send")) -> CapabilityRecord:
    return CapabilityRecord(
        toolbox_id=server,
        dispatch_topic=f"mcp_server.{server}",
        tools=[
            CapabilityToolDef(name=t, description=None, parameters_json_schema={"type": "object"})
            for t in tools
        ],
        published_at=datetime.now(tz=UTC),
    )


class TestProtocolCompliance:
    def test_satisfies_tool_selector_protocol(self) -> None:
        assert isinstance(MCPToolboxRef("gmail"), ToolSelector)

    def test_split_tool_declarations_classifies_as_deferred_selector(self) -> None:
        """``Worker._maybe_register_capability_view`` keys off the agent's
        ``_tool_selectors`` — which exist only if calfkit's partitioner
        routes the ref to the deferred side. This is the wire that makes
        per-turn discovery work end-to-end."""
        bindings, selectors = split_tool_declarations([MCPToolboxRef("gmail")])
        assert bindings == []
        assert len(selectors) == 1


class TestResolveTools:
    def test_bare_selector_resolves_all_advertised_tools(self) -> None:
        result = MCPToolboxRef("gmail").resolve_tools({"gmail": _record()})
        assert [b.name for b in result.bindings] == ["search", "send"]
        assert result.bindings[0].dispatch_topic == "mcp_server.gmail"
        assert not result.unresolved

    def test_include_scopes_to_named_tools(self) -> None:
        sel = MCPToolboxRef("gmail", include=("search",))
        result = sel.resolve_tools({"gmail": _record()})
        assert [b.name for b in result.bindings] == ["search"]

    def test_missing_server_degrades_not_raises(self) -> None:
        result = MCPToolboxRef("gmail").resolve_tools({})
        assert result.missing_toolbox is True
        assert result.bindings == []

    def test_non_strict_policy(self) -> None:
        """calfcord policy: agents boot and run when their MCP servers are
        down; the turn degrades with a warning rather than failing. The
        upstream default is non-strict and ``selectors_from_entries`` never
        overrides it — both pinned here."""
        assert MCPToolboxRef("gmail").resolve_tools({}).strict is False
        assert all(
            s.strict is False
            for s in selectors_from_entries(["mcp/gmail", "mcp/docs/search"])
        )

    def test_missing_included_tool_reported(self) -> None:
        sel = MCPToolboxRef("gmail", include=("search", "nope"))
        result = sel.resolve_tools({"gmail": _record()})
        assert result.missing_tools == ("nope",)


class TestSelectorsFromEntries:
    def test_explicit_tools_merge_per_server_sorted_deduped(self) -> None:
        sels = selectors_from_entries(
            ["mcp/gmail/send", "mcp/gmail/search", "mcp/gmail/send"]
        )
        assert sels == [MCPToolboxRef("gmail", include=("search", "send"))]

    def test_bare_server_selects_all(self) -> None:
        assert selectors_from_entries(["mcp/gmail"]) == [MCPToolboxRef("gmail", include=None)]

    def test_bare_subsumes_explicit(self) -> None:
        """``mcp/gmail`` + ``mcp/gmail/search`` collapses to the wildcard —
        the old schema-build dedup semantics."""
        sels = selectors_from_entries(["mcp/gmail/search", "mcp/gmail"])
        assert sels == [MCPToolboxRef("gmail", include=None)]

    def test_servers_sorted_for_determinism(self) -> None:
        sels = selectors_from_entries(["mcp/zeta", "mcp/alpha"])
        assert [s.toolbox_id for s in sels] == ["alpha", "zeta"]

    def test_malformed_selector_raises_naming_entry(self) -> None:
        with pytest.raises(ValueError, match="mcp/a/b/c"):
            selectors_from_entries(["mcp/a/b/c"])

    def test_non_mcp_entry_rejected(self) -> None:
        """The caller (factory) partitions builtins out first; a bare name
        reaching this function is a programming error, not user input."""
        with pytest.raises(ValueError, match="shell"):
            selectors_from_entries(["shell"])

    def test_empty_input_yields_empty(self) -> None:
        assert selectors_from_entries([]) == []
