"""Registry-level invariants for the tool surface.

The tool surface is an explicit, auditable composition
(:data:`calfcord.tools.ALL_TOOLS`) of the vendored ``calfkit-tools`` nodes
plus the first-party ``private_chat`` tool. These tests pin that surface
and guard against drift between what calfcord exposes and what the
vendored package actually publishes.
"""

from __future__ import annotations

import pytest
from calfkit.nodes.tool import ToolNodeDef
from calfkit_tools.hermes.node import HERMES_NODES

from calfcord.tools import ALL_TOOLS, TOOL_REGISTRY

# The exact tool surface calfcord exposes. Edit this when adopting or
# dropping a tool — the tests below then flag any drift.
EXPECTED_TOOLS = frozenset(
    {
        # hermes (vendored)
        "terminal",
        "process",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "todo",
        "execute_code",
        "web_search",
        "web_extract",
        # web_fetch (vendored, separate subpackage)
        "web_fetch",
        # first-party
        "private_chat",
    }
)

# Vendored hermes tools we deliberately do NOT expose. Empty today: we
# adopt the full hermes surface. Listing a name here is the explicit,
# reviewable way to drop a published tool (see the drift-guard below).
_EXCLUDED_HERMES_NODES: frozenset[str] = frozenset()

# Names contributed by sources other than HERMES_NODES.
_NON_HERMES_TOOLS = frozenset({"web_fetch", "private_chat"})


class TestToolRegistry:
    def test_registry_is_non_empty(self) -> None:
        assert TOOL_REGISTRY, "TOOL_REGISTRY is empty — composition did not populate it"

    def test_registry_matches_expected_surface_exactly(self) -> None:
        actual = set(TOOL_REGISTRY)
        missing = EXPECTED_TOOLS - actual
        extras = actual - EXPECTED_TOOLS
        assert not missing and not extras, (
            f"registry drift: missing={sorted(missing)} extras={sorted(extras)}"
        )

    def test_every_entry_is_a_tool_node_def(self) -> None:
        for name, node in TOOL_REGISTRY.items():
            assert isinstance(node, ToolNodeDef), (
                f"{name!r} is {type(node).__name__}, not ToolNodeDef"
            )

    @pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
    def test_schema_name_matches_registry_key(self, name: str) -> None:
        node = TOOL_REGISTRY[name]
        assert node.tool_schema.name == name, (
            f"registry key {name!r} maps to a tool whose schema is named "
            f"{node.tool_schema.name!r} — schema/registry drift makes LLMs "
            "fail to invoke the tool by name"
        )

    @pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
    def test_subscribe_topic_matches_name_convention(self, name: str) -> None:
        node = TOOL_REGISTRY[name]
        assert node.subscribe_topics == [f"tool.{name}.input"]


class TestSurfaceDriftGuard:
    """Keep calfcord's exposed surface in lockstep with the vendored package."""

    def test_exposed_hermes_tools_are_all_published_by_package(self) -> None:
        """Loud failure if the package renames/removes a tool we still list.

        Every hermes-sourced name in ``ALL_TOOLS`` must still be published
        in ``HERMES_NODES``; otherwise a dependency bump silently broke a
        tool calfcord advertises.
        """
        published = {n.tool_schema.name for n in HERMES_NODES}
        exposed_hermes = {n.tool_schema.name for n in ALL_TOOLS} - _NON_HERMES_TOOLS
        unpublished = exposed_hermes - published
        assert not unpublished, (
            f"calfcord exposes hermes tools the package no longer publishes: "
            f"{sorted(unpublished)}; published={sorted(published)}"
        )

    def test_all_published_hermes_tools_are_deliberately_handled(self) -> None:
        """Force a deliberate edit when the package adds a hermes tool.

        Each published hermes node must be either exposed in ``ALL_TOOLS``
        or listed in ``_EXCLUDED_HERMES_NODES``. A new upstream tool then
        fails this test until someone consciously adopts or excludes it —
        nothing reaches agents without a reviewable decision.
        """
        published = {n.tool_schema.name for n in HERMES_NODES}
        exposed_hermes = {n.tool_schema.name for n in ALL_TOOLS} - _NON_HERMES_TOOLS
        unhandled = published - exposed_hermes - _EXCLUDED_HERMES_NODES
        assert not unhandled, (
            f"package publishes hermes tools calfcord neither exposes nor "
            f"excludes: {sorted(unhandled)}; add them to ALL_TOOLS or to "
            f"_EXCLUDED_HERMES_NODES with a rationale"
        )
