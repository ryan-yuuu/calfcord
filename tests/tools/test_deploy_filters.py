"""Tests for :mod:`calfcord.tools.deploy_filters`.

``apply_deploy_filters`` composes the tool registry from an explicit list
of :class:`ToolNodeDef` objects, applying the deploy-time
``CALFCORD_TOOLS_INCLUDE`` (per-host tool narrowing) and ``CALFCORD_TOOLS_ALIAS``
(multi-host rename) transforms. It is a pure function of (nodes, env): no
filesystem walk, no import cycle. These tests build real ``ToolNodeDef``
instances via ``agent_tool`` and exercise the transform directly.
"""

from __future__ import annotations

import pytest
from calfkit.nodes import ToolNodeDef, agent_tool

from calfcord.tools.deploy_filters import (
    TOOL_NAME_REGEX,
    _clone_with_name,
    _resolve_alias_map,
    _resolve_include_filter,
    apply_deploy_filters,
    is_aliasable,
    parse_alias_csv,
    serialize_alias_map,
    validate_alias,
)


def _make_node(name: str) -> ToolNodeDef:
    """Build a real ``ToolNodeDef`` whose ``tool_schema.name`` is ``name``.

    ``agent_tool`` derives the schema name (and the wire topics) from the
    function's ``__name__``, so we stamp the name onto a fresh function.
    """

    async def _impl(ctx, payload: str) -> str:
        """Trivial tool for tests."""
        return payload

    _impl.__name__ = name
    _impl.__qualname__ = name
    return agent_tool(_impl)


@pytest.fixture(autouse=True)
def _clear_deploy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no include/alias env so cases are isolated."""
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    monkeypatch.delenv("CALFCORD_TOOLS_ALIAS", raising=False)


# --------------------------------------------------------------------------
# apply_deploy_filters — the public transform
# --------------------------------------------------------------------------


def test_no_env_registers_every_node_by_schema_name() -> None:
    nodes = [_make_node("alpha"), _make_node("beta")]
    registry = apply_deploy_filters(nodes)
    assert set(registry) == {"alpha", "beta"}
    assert registry["alpha"] is nodes[0]
    assert all(isinstance(v, ToolNodeDef) for v in registry.values())


def test_include_filters_to_listed_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha")
    registry = apply_deploy_filters([_make_node("alpha"), _make_node("beta")])
    assert set(registry) == {"alpha"}


def test_include_unset_registers_all() -> None:
    registry = apply_deploy_filters([_make_node("alpha"), _make_node("beta")])
    assert set(registry) == {"alpha", "beta"}


def test_include_unknown_name_warns_and_returns_known_subset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha,does_not_exist")
    with caplog.at_level("WARNING"):
        registry = apply_deploy_filters([_make_node("alpha")])
    assert set(registry) == {"alpha"}
    assert any("does_not_exist" in r.message for r in caplog.records)


def test_duplicate_input_name_raises() -> None:
    with pytest.raises(ValueError, match="alpha"):
        apply_deploy_filters([_make_node("alpha"), _make_node("alpha")])


# --------------------------------------------------------------------------
# Aliasing
# --------------------------------------------------------------------------


def test_alias_clones_under_new_name_and_keeps_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    registry = apply_deploy_filters([_make_node("alpha")])
    assert set(registry) == {"alpha", "alpha_eu"}


def test_alias_clone_rewrites_topics_and_node_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    registry = apply_deploy_filters([_make_node("alpha")])
    clone = registry["alpha_eu"]
    assert clone.tool_schema.name == "alpha_eu"
    assert clone.subscribe_topics == ["tool.alpha_eu.input"]
    assert clone.publish_topic == "tool.alpha_eu.output"
    assert clone.node_id == "tool_alpha_eu"


def test_alias_plus_include_dst_is_true_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha_eu")
    registry = apply_deploy_filters([_make_node("alpha")])
    assert set(registry) == {"alpha_eu"}


def test_alias_unknown_source_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "ghost=ghost_eu")
    with pytest.raises(ValueError, match="ghost"):
        apply_deploy_filters([_make_node("alpha")])


def test_alias_dst_colliding_with_existing_tool_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=beta")
    with pytest.raises(ValueError, match="beta"):
        apply_deploy_filters([_make_node("alpha"), _make_node("beta")])


# --------------------------------------------------------------------------
# _resolve_include_filter — ported from discovery
# --------------------------------------------------------------------------


class TestResolveIncludeFilter:
    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _resolve_include_filter() is None

    def test_empty_string_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "")
        assert _resolve_include_filter() is None

    def test_whitespace_only_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "   ")
        assert _resolve_include_filter() is None

    def test_comma_list_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "a, b ,c")
        assert _resolve_include_filter() == {"a", "b", "c"}


# --------------------------------------------------------------------------
# _resolve_alias_map — ported from discovery
# --------------------------------------------------------------------------


class TestResolveAliasMap:
    def test_unset_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _resolve_alias_map() == {}

    def test_single_pair_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=b")
        assert _resolve_alias_map() == {"a": "b"}

    def test_multiple_pairs_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=b,c=d")
        assert _resolve_alias_map() == {"a": "b", "c": "d"}

    def test_malformed_entry_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "noseparator")
        with pytest.raises(ValueError):
            _resolve_alias_map()

    def test_empty_side_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=")
        with pytest.raises(ValueError):
            _resolve_alias_map()

    def test_self_alias_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=a")
        with pytest.raises(ValueError):
            _resolve_alias_map()

    def test_invalid_dst_regex_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=bad name!")
        with pytest.raises(ValueError):
            _resolve_alias_map()

    def test_duplicate_source_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=b,a=c")
        with pytest.raises(ValueError):
            _resolve_alias_map()

    def test_duplicate_target_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=z,b=z")
        with pytest.raises(ValueError):
            _resolve_alias_map()

    def test_trailing_comma_empty_chunk_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A trailing/double comma yields empty chunks that are skipped,
        # not errored — operators shouldn't trip over a stray comma.
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=b,,c=d,")
        assert _resolve_alias_map() == {"a": "b", "c": "d"}


# --------------------------------------------------------------------------
# _clone_with_name — ported from discovery
# --------------------------------------------------------------------------


class TestCloneWithName:
    def test_rewrites_all_four_name_bound_fields(self) -> None:
        clone = _clone_with_name(_make_node("alpha"), "alpha_eu")
        assert clone.tool_schema.name == "alpha_eu"
        assert clone.subscribe_topics == ["tool.alpha_eu.input"]
        assert clone.publish_topic == "tool.alpha_eu.output"
        assert clone.node_id == "tool_alpha_eu"

    def test_aliasing_node_with_resource_bracket_raises(self) -> None:
        # A node-scoped @resource can't be safely shared under a second wire
        # identity, so aliasing it fails loud (the only bracketed tools —
        # todo, private_chat — are single-host and never aliased).
        node = _make_node("alpha")

        @node.resource("thing")
        async def _thing(setup_ctx):  # pragma: no cover - body not run
            yield object()

        with pytest.raises(ValueError, match="node-scoped resources"):
            _clone_with_name(node, "alpha_eu")

    def test_aliasing_node_with_lifecycle_hook_raises(self) -> None:
        node = _make_node("alpha")

        @node.on_startup
        async def _boot(setup_ctx):  # pragma: no cover - body not run
            return None

        with pytest.raises(ValueError, match="node-scoped resources"):
            _clone_with_name(node, "alpha_eu")

    def test_non_dataclass_tool_schema_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a future calfkit makes ToolNodeDef.tool_schema non-dataclass,
        ``dataclasses.replace`` raises TypeError; the helper must re-raise a
        RuntimeError naming the version-mismatch cause."""
        import calfcord.tools.deploy_filters as df

        def _boom(*args, **kwargs):
            raise TypeError("not a dataclass")

        monkeypatch.setattr(df.dataclasses, "replace", _boom)
        with pytest.raises(RuntimeError, match="not a dataclass in this calfkit"):
            _clone_with_name(_make_node("alpha"), "alpha_eu")


class TestToolNameRegex:
    def test_accepts_valid(self) -> None:
        assert TOOL_NAME_REGEX.match("terminal_eu-1")

    def test_rejects_invalid(self) -> None:
        assert TOOL_NAME_REGEX.match("bad name") is None


# --------------------------------------------------------------------------
# parse_alias_csv / serialize_alias_map — the shared CSV grammar used by both
# the runtime (_resolve_alias_map) and the calfcord tools alias CLI.
# --------------------------------------------------------------------------


class TestParseAliasCsv:
    def test_empty_returns_empty(self) -> None:
        assert parse_alias_csv("") == {}

    def test_whitespace_returns_empty(self) -> None:
        assert parse_alias_csv("   ") == {}

    def test_single_pair(self) -> None:
        assert parse_alias_csv("a=b") == {"a": "b"}

    def test_multiple_pairs(self) -> None:
        assert parse_alias_csv("a=b,c=d") == {"a": "b", "c": "d"}

    def test_trailing_and_double_commas_tolerated(self) -> None:
        assert parse_alias_csv("a=b,,c=d,") == {"a": "b", "c": "d"}

    def test_no_separator_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_alias_csv("noeq")

    def test_empty_dst_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_alias_csv("a=")

    def test_empty_src_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_alias_csv("=b")

    def test_invalid_dst_regex_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_alias_csv("a=bad name")

    def test_self_alias_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_alias_csv("a=a")

    def test_duplicate_source_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_alias_csv("a=b,a=c")

    def test_duplicate_target_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_alias_csv("a=z,b=z")


class TestSerializeAliasMap:
    def test_empty_is_empty_string(self) -> None:
        assert serialize_alias_map({}) == ""

    def test_single(self) -> None:
        assert serialize_alias_map({"a": "b"}) == "a=b"

    def test_sorted_for_determinism(self) -> None:
        assert serialize_alias_map({"c": "d", "a": "b"}) == "a=b,c=d"

    def test_round_trips_with_parse(self) -> None:
        aliases = {"terminal": "terminal_eu", "patch": "patch_eu"}
        assert parse_alias_csv(serialize_alias_map(aliases)) == aliases


# --------------------------------------------------------------------------
# is_aliasable — a tool with node-scoped lifecycle state (an @resource bracket
# or a lifecycle hook) cannot be cloned under a second wire identity.
# --------------------------------------------------------------------------


class TestIsAliasable:
    def test_plain_node_is_aliasable(self) -> None:
        assert is_aliasable(_make_node("terminal")) is True

    def test_resource_bracket_node_is_not_aliasable(self) -> None:
        node = _make_node("todo")

        @node.resource("state")
        async def _state(setup_ctx):  # pragma: no cover - body not run
            yield object()

        assert is_aliasable(node) is False

    def test_lifecycle_hook_node_is_not_aliasable(self) -> None:
        node = _make_node("private_chat")

        @node.on_startup
        async def _boot(setup_ctx):  # pragma: no cover - body not run
            return None

        assert is_aliasable(node) is False


# --------------------------------------------------------------------------
# validate_alias — the CLI add-time validator (the seven rules in the spec).
# --------------------------------------------------------------------------

_TOOLS = {"terminal", "read_file", "todo"}
_ALIASABLE = {"terminal", "read_file"}  # todo holds per-session state


class TestValidateAlias:
    def test_valid_passes(self) -> None:
        validate_alias(
            "terminal", "terminal_eu",
            tool_names=_TOOLS, aliasable_names=_ALIASABLE, existing={},
        )

    def test_unknown_src_raises(self) -> None:
        with pytest.raises(ValueError, match="not a known tool"):
            validate_alias(
                "ghost", "ghost_eu",
                tool_names=_TOOLS, aliasable_names=_ALIASABLE, existing={},
            )

    def test_non_aliasable_src_raises(self) -> None:
        with pytest.raises(ValueError, match="can't be aliased"):
            validate_alias(
                "todo", "todo_eu",
                tool_names=_TOOLS, aliasable_names=_ALIASABLE, existing={},
            )

    def test_invalid_dst_regex_raises(self) -> None:
        with pytest.raises(ValueError, match="valid tool name"):
            validate_alias(
                "terminal", "bad name",
                tool_names=_TOOLS, aliasable_names=_ALIASABLE, existing={},
            )

    def test_self_alias_raises(self) -> None:
        with pytest.raises(ValueError, match="itself"):
            validate_alias(
                "terminal", "terminal",
                tool_names=_TOOLS, aliasable_names=_ALIASABLE, existing={},
            )

    def test_dst_collides_with_real_tool_raises(self) -> None:
        with pytest.raises(ValueError, match="already a tool"):
            validate_alias(
                "terminal", "read_file",
                tool_names=_TOOLS, aliasable_names=_ALIASABLE, existing={},
            )

    def test_dst_collides_with_existing_alias_target_raises(self) -> None:
        with pytest.raises(ValueError, match="already used"):
            validate_alias(
                "read_file", "terminal_eu",
                tool_names=_TOOLS, aliasable_names=_ALIASABLE,
                existing={"terminal": "terminal_eu"},
            )

    def test_src_already_aliased_raises(self) -> None:
        with pytest.raises(ValueError, match="already aliased"):
            validate_alias(
                "terminal", "terminal_2",
                tool_names=_TOOLS, aliasable_names=_ALIASABLE,
                existing={"terminal": "terminal_eu"},
            )
