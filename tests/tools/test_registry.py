"""Registry-level invariants for TOOL_REGISTRY.

These catch regressions where a tool is renamed in its module but the
registry entry isn't updated, or where a tool's ToolNodeDef has the
wrong schema name (which would cause LLMs to fail with "unknown tool"
at runtime).
"""

from __future__ import annotations

import pytest
from calfkit.nodes.tool import ToolNodeDef

from calfkit_organization.tools import TOOL_REGISTRY

# The expected set of builtins. Edit this when adding a new tool —
# the test will then flag any drift.
EXPECTED_BUILTINS = frozenset(
    {
        "edit_file",
        "glob",
        "grep",
        "private_chat",
        "read_file",
        "shell",
        "todo_view",
        "todo_write",
        "web_fetch",
        "web_search",
        "write_file",
    }
)


class TestToolRegistry:
    def test_registry_is_non_empty(self) -> None:
        """Belt-and-suspenders: the parametrized assertions below would
        all pass vacuously if both ``EXPECTED_BUILTINS`` and
        ``TOOL_REGISTRY`` became empty (e.g. the auto-discovery walk
        skipped every module due to a regression). A direct truthiness
        check catches the "registry is empty AND nobody noticed" case."""
        assert TOOL_REGISTRY, (
            "TOOL_REGISTRY is empty — auto-discovery did not populate it at import"
        )

    def test_registry_matches_expected_builtins_exactly(self) -> None:
        """Auto-discovery makes ``EXPECTED_BUILTINS`` the authoritative
        shipped set. Both directions of drift surface here:

        * ``missing`` — a builtin was removed or renamed without
          updating ``EXPECTED_BUILTINS``. The downstream impact is
          agents declaring it in ``.md`` failing at boot.
        * ``extras`` — a new tool was added to ``tools/builtin/``
          without bumping ``EXPECTED_BUILTINS``. Forgetting is harmless
          at runtime but means the project's claimed surface drifts
          from reality; this assertion forces the bump.
        """
        actual = set(TOOL_REGISTRY)
        missing = EXPECTED_BUILTINS - actual
        extras = actual - EXPECTED_BUILTINS
        assert not missing and not extras, (
            f"registry drift: missing={sorted(missing)} extras={sorted(extras)}"
        )

    def test_every_entry_is_a_tool_node_def(self) -> None:
        for name, node in TOOL_REGISTRY.items():
            assert isinstance(node, ToolNodeDef), f"{name!r} is {type(node).__name__}, not ToolNodeDef"

    @pytest.mark.parametrize("name", sorted(EXPECTED_BUILTINS))
    def test_schema_name_matches_registry_key(self, name: str) -> None:
        node = TOOL_REGISTRY[name]
        assert node.tool_schema.name == name, (
            f"registry key {name!r} maps to a tool whose schema is named "
            f"{node.tool_schema.name!r} — schema/registry drift will make LLMs "
            "fail to invoke the tool by name"
        )

    @pytest.mark.parametrize("name", sorted(EXPECTED_BUILTINS))
    def test_subscribe_topic_matches_name_convention(self, name: str) -> None:
        node = TOOL_REGISTRY[name]
        # Calfkit derives ``tool.<name>.input`` from the bare function
        # name. If our wrapper accidentally renamed the function or
        # decorated something with the wrong name, the topic will not
        # match and the runner will silently fail to dispatch calls.
        assert node.subscribe_topics == [f"tool.{name}.input"]
