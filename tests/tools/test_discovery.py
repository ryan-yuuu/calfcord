"""Tests for :func:`calfcord.tools.discovery.discover_tools`.

Each test builds a throwaway *real* package on disk under ``tmp_path``
and prepends that directory to ``sys.path`` via ``monkeypatch``. We use
real files (rather than ``sys.modules`` injection) because
:func:`pkgutil.iter_modules` walks the filesystem; injected modules with
no on-disk presence are invisible to it.

Each fake package is given a unique name (parameterised by the test
function) so ``sys.modules`` caching from one test does not bleed into
another. We deliberately do not import or touch
:data:`calfcord.tools.TOOL_REGISTRY` from these tests — every
test passes a fresh ``{}`` to ``discover_tools`` so the real registry
stays out of the picture.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from calfkit.nodes.tool import ToolNodeDef

from calfcord.tools.discovery import (
    _clone_with_name,
    _resolve_alias_map,
    discover_tools,
)


def _write_package(root: Path, pkg_name: str, modules: dict[str, str]) -> Path:
    """Create a real package directory under ``root`` with the given modules.

    ``modules`` maps a stem (e.g. ``"foo"`` → ``foo.py``) to its source
    text. An empty ``__init__.py`` is created automatically.
    Returns the package directory.
    """
    pkg_dir = root / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for stem, source in modules.items():
        (pkg_dir / f"{stem}.py").write_text(textwrap.dedent(source))
    return pkg_dir


def _import_fresh(pkg_name: str):
    """Import ``pkg_name`` after evicting any cached entries.

    Cached entries can survive across tests when pytest uses the same
    interpreter, and the on-disk files differ between tests.
    """
    for cached in [k for k in sys.modules if k == pkg_name or k.startswith(pkg_name + ".")]:
        del sys.modules[cached]
    return importlib.import_module(pkg_name)


# A small tool-source snippet reused across tests. The function name
# becomes the schema name, so the registered key matches whichever
# ``func_name`` the caller picks.
_TOOL_SOURCE_TEMPLATE = """\
from calfkit.nodes import agent_tool, ToolNodeDef


async def {func_name}(ctx, payload: str) -> str:
    \"\"\"Trivial tool for tests.\"\"\"
    return payload


{var_name}: ToolNodeDef = agent_tool({func_name})
"""


def _tool_source(func_name: str, var_name: str | None = None) -> str:
    return _TOOL_SOURCE_TEMPLATE.format(
        func_name=func_name,
        var_name=var_name or f"{func_name}_tool",
    )


def test_smoke_two_tools_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg_name = "fake_pkg_smoke"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "alpha": _tool_source("alpha"),
            "beta": _tool_source("beta"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)

    assert set(registry) == {"alpha", "beta"}
    assert all(isinstance(v, ToolNodeDef) for v in registry.values())
    assert registry["alpha"].tool_schema.name == "alpha"
    assert registry["beta"].tool_schema.name == "beta"


def test_underscore_prefixed_modules_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg_name = "fake_pkg_underscore"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "public": _tool_source("public"),
            "_internal": _tool_source("hidden"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)

    assert "public" in registry
    assert "hidden" not in registry
    # And the underscore-prefixed module should not even have been imported,
    # since iter_modules + the prefix check happen before importlib.
    assert f"{pkg_name}._internal" not in sys.modules


def test_name_collision_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two distinct ToolNodeDef instances that advertise the same schema
    # name. We achieve this by defining two functions whose ``__name__``
    # is rewritten to the same value before ``agent_tool`` wraps them.
    collider_source = """\
from calfkit.nodes import agent_tool, ToolNodeDef


async def _impl(ctx, payload: str) -> str:
    \"\"\"Trivial tool for tests.\"\"\"
    return payload


_impl.__name__ = "duplicate"
duplicate_tool: ToolNodeDef = agent_tool(_impl)
"""
    pkg_name = "fake_pkg_collision"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "first": collider_source,
            "second": collider_source,
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ValueError) as exc_info:
        discover_tools(pkg, registry)

    msg = str(exc_info.value)
    # Both module paths must be named so the operator can resolve the
    # conflict without grepping.
    assert f"{pkg_name}.first" in msg
    assert f"{pkg_name}.second" in msg
    assert "duplicate" in msg


def test_reexport_deduped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``module_b`` re-imports ``foo_tool`` from ``module_a``. The discovery
    # walk will see the same ToolNodeDef instance twice; it must register
    # it once, not raise on the second sighting.
    pkg_name = "fake_pkg_reexport"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "module_a": _tool_source("foo"),
            "module_b": "from .module_a import foo_tool  # re-export\n",
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)

    assert set(registry) == {"foo"}
    # Belt-and-suspenders: a switch to "last-write-wins silently" would
    # still produce a one-key registry, so the name-set check alone is
    # too weak. Assert exactly one entry AND that re-export and origin
    # resolve to the same instance.
    assert len(registry) == 1
    mod_a = sys.modules[f"{pkg_name}.module_a"]
    mod_b = sys.modules[f"{pkg_name}.module_b"]
    assert registry["foo"] is mod_a.foo_tool is mod_b.foo_tool


def test_non_tool_attributes_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = """\
from calfkit.nodes import agent_tool, ToolNodeDef


SOME_CONSTANT = 42
OTHER_CONSTANT = "hello"


class HelperClass:
    pass


def helper_function() -> int:
    return 1


async def real_tool(ctx, payload: str) -> str:
    \"\"\"Trivial tool for tests.\"\"\"
    return payload


real_tool_tool: ToolNodeDef = agent_tool(real_tool)
"""
    pkg_name = "fake_pkg_non_tool"
    _write_package(tmp_path, pkg_name, {"mixed": source})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)

    # Only the one ToolNodeDef should be registered, under its schema name.
    assert set(registry) == {"real_tool"}


def test_import_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg_name = "fake_pkg_import_error"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "broken": "raise ImportError('this module is broken on purpose')\n",
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ImportError, match="broken on purpose") as exc_info:
        discover_tools(pkg, registry)
    # Stronger: assert the broken module's qualified name appears in the
    # traceback. A "fix" that swallows the original ImportError and
    # re-raises a generic one would pass the ``match=`` check above (if
    # the message text were preserved), so checking the traceback frame
    # ties the assertion to the actual offending source.
    import traceback
    tb = "".join(traceback.format_exception(exc_info.value))
    assert f"{pkg_name}/broken.py" in tb or f"{pkg_name}.broken" in tb


def test_registry_already_populated_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-populating the registry via a separate ``discover_tools`` run
    seeds the colliding name's origin with that prior run's module path,
    so the collision message names a real source. This tests the common
    case (two discoveries against the same registry)."""
    pkg_name = "fake_pkg_prepopulated"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "only": _tool_source("preexisting"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    placeholder_pkg_name = "fake_pkg_prepopulated_placeholder"
    _write_package(
        tmp_path,
        placeholder_pkg_name,
        {"holder": _tool_source("preexisting", var_name="holder_tool")},
    )
    placeholder_pkg = _import_fresh(placeholder_pkg_name)
    placeholder_registry: dict[str, ToolNodeDef] = {}
    discover_tools(placeholder_pkg, placeholder_registry)

    registry: dict[str, ToolNodeDef] = dict(placeholder_registry)
    assert "preexisting" in registry

    # ``origins`` is per-call, so the second ``discover_tools`` invocation
    # cannot know the placeholder run's module path — every name already
    # in ``registry`` on entry is labelled ``<pre-populated>``. The
    # collision message surfaces that sentinel so the operator knows the
    # prior registration came from outside this walk.
    with pytest.raises(ValueError) as exc_info:
        discover_tools(pkg, registry)
    msg = str(exc_info.value)
    assert "preexisting" in msg
    assert f"{pkg_name}.only" in msg
    assert "<pre-populated>" in msg


def test_prepopulated_unknown_origin_surfaces_as_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Variant of the collision case where the prior entry was injected
    directly (bypassing ``discover_tools`` entirely) rather than seeded
    by a prior walk. Both paths must produce the ``<pre-populated>``
    sentinel — exercising the raw-injection path here pins it against a
    future refactor that ties ``origins`` seeding to having run a walk."""
    from calfkit.nodes import agent_tool

    async def _raw(ctx, payload: str) -> str:
        """Trivial tool built outside any module-discovery walk."""
        return payload

    _raw.__name__ = "preexisting"
    raw_tool = agent_tool(_raw)

    pkg_name = "fake_pkg_sentinel"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "only": _tool_source("preexisting"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {"preexisting": raw_tool}
    with pytest.raises(ValueError) as exc_info:
        discover_tools(pkg, registry)
    msg = str(exc_info.value)
    assert "preexisting" in msg
    assert f"{pkg_name}.only" in msg
    assert "<pre-populated>" in msg


def test_empty_package_leaves_registry_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a package with no submodules is a no-op walk. The
    registry must not be touched. Guards against a refactor that would
    accidentally seed defaults on an empty walk."""
    pkg_name = "fake_pkg_empty"
    _write_package(tmp_path, pkg_name, {})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert registry == {}


def test_package_with_only_underscore_modules_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adjacent boundary: a package whose only submodules are private
    support files must register nothing — and must not import any of
    them (the skip happens before importlib)."""
    pkg_name = "fake_pkg_only_private"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "_alpha": _tool_source("hidden_alpha"),
            "_beta": _tool_source("hidden_beta"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert registry == {}
    assert f"{pkg_name}._alpha" not in sys.modules
    assert f"{pkg_name}._beta" not in sys.modules


def test_underscore_prefixed_attributes_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric with the module-level rule: an attribute starting with
    ``_`` is treated as a construction artifact and not registered, so
    authors can declare ``_fixture_tool = agent_tool(...)`` for tests
    or parametrized variants without triggering auto-discovery."""
    source = """\
from calfkit.nodes import agent_tool, ToolNodeDef


async def public(ctx, payload: str) -> str:
    \"\"\"Trivial tool for tests.\"\"\"
    return payload


async def hidden(ctx, payload: str) -> str:
    \"\"\"Trivial tool that should NOT auto-register.\"\"\"
    return payload


public_tool: ToolNodeDef = agent_tool(public)
_hidden_tool: ToolNodeDef = agent_tool(hidden)
"""
    pkg_name = "fake_pkg_underscore_attr"
    _write_package(tmp_path, pkg_name, {"mixed": source})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"public"}
    # The hidden tool's schema name is still "hidden" — confirm it's
    # not lurking under that name either.
    assert "hidden" not in registry


def test_dunder_all_does_not_gate_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the deliberate choice to use ``dir(module)`` (not
    ``module.__all__``) for attribute discovery. Tools register based
    on type identity, not export decorations. A future refactor to
    ``__all__`` would silently disappear any tool not listed there."""
    source = _tool_source("included") + "\n__all__ = []\n"
    pkg_name = "fake_pkg_dunder_all"
    _write_package(tmp_path, pkg_name, {"mod": source})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert "included" in registry


# ── CALFCORD_TOOLS_INCLUDE filter ─────────────────────────────────────────
#
# The filter is the load-bearing half of the per-tool image story (PR 5):
# images bake ``ENV CALFCORD_TOOLS_INCLUDE=...`` so the same calfcord
# image hosts a different tool subset at boot. The tests below pin every
# documented edge case of ``_resolve_include_filter`` and the
# ``discover_tools`` skip branch.


def _three_tool_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pkg_name: str):
    """Build a fixture package with three tools (alpha, beta, gamma).

    Returns the imported package object, ready for ``discover_tools``.
    Used by every filter test so the fixture is consistent and the
    test bodies focus on the include-set behavior under test.
    """
    _write_package(
        tmp_path,
        pkg_name,
        {
            "alpha": _tool_source("alpha"),
            "beta": _tool_source("beta"),
            "gamma": _tool_source("gamma"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return _import_fresh(pkg_name)


def test_include_filter_restricts_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CALFCORD_TOOLS_INCLUDE=alpha,gamma`` registers only those two."""
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha,gamma")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_filter_subset")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha", "gamma"}


def test_include_filter_unset_registers_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default behavior when the env var is absent — every tool registers."""
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_filter_unset")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha", "beta", "gamma"}


def test_include_filter_empty_string_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty string == unset. Pins the documented semantic so a future
    refactor that changes "empty" to mean "register nothing" fails CI."""
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_filter_empty")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha", "beta", "gamma"}


def test_include_filter_whitespace_only_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-with-commas is the kind of thing that ends up in a
    hand-edited ``.env``. Must normalize to "no filter," not "empty
    include list."""
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "  ,  ")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_filter_whitespace")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha", "beta", "gamma"}


def test_include_filter_unknown_name_yields_empty_registry_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo in the env var (``shel`` instead of ``shell``) produces an
    empty registry. ``discover_tools`` MUST emit a WARNING with the
    typo'd name so the operator's "why is my registry empty"
    investigation lands on the cause immediately."""
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "definitely_not_a_tool")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_filter_typo")

    registry: dict[str, ToolNodeDef] = {}
    with caplog.at_level("WARNING"):
        discover_tools(pkg, registry)
    assert registry == {}
    # The warning message must name the typo'd entry AND list what was
    # actually discovered — both are needed for the operator to
    # diagnose the typo without grepping the source.
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("definitely_not_a_tool" in m for m in warnings), (
        f"expected a WARNING naming the unknown filter entry; got: {warnings}"
    )


def test_include_filter_does_not_short_circuit_import_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter is applied AFTER import. A broken module in the
    package must still fail loud even when the filter would have
    excluded it — preserves the ``ImportError`` propagation contract
    documented in the module docstring."""
    pkg_name = "fake_pkg_filter_and_broken"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "alpha": _tool_source("alpha"),
            "broken": "raise ImportError('intentionally broken')\n",
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    # ``alpha`` registers first (alphabetical iteration); ``broken``'s
    # import then raises ImportError before the filter has a chance to
    # short-circuit it. The contract under test is that the filter does
    # NOT swallow import failures regardless of whether the offending
    # module would have been filtered out — broken code is always loud.
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha")
    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ImportError, match="intentionally broken"):
        discover_tools(pkg, registry)


# ── CALFCORD_TOOLS_ALIAS expansion ────────────────────────────────────────
#
# Aliasing clones a discovered ToolNodeDef under a new schema name with
# all four name-bound fields rewritten (``tool_schema.name``,
# ``subscribe_topics``, ``publish_topic``, ``node_id``). Pairs with the
# include filter to give per-host rename semantics for multi-host
# deployments of the same tool. The tests below pin the parser
# semantics, the additive registry behavior, the wire-level isolation
# of clone vs. original, and the failure modes operators most often hit.


class TestCloneCarriesLifecycle:
    """A renamed clone must carry the source node's lifecycle registrations.

    ``_clone_with_name`` rebuilds the node via ``dataclasses.replace``, which
    copies only dataclass fields — the lifecycle ``@resource`` brackets and
    hooks live in ``__dict__`` (lazily created by ``LifecycleHookMixin``), so
    without an explicit carry-over a clone would silently lose them and the
    aliased tool's body would never receive its node-scoped resource at runtime.
    """

    def test_clone_preserves_resource_brackets(self) -> None:
        from calfkit.nodes import agent_tool

        async def _impl(ctx) -> str:  # noqa: ANN001 - ToolContext
            return "ok"

        node = agent_tool(_impl)

        @node.resource("conn")
        async def _conn(ctx):  # noqa: ANN001 - ResourceSetupContext
            yield object()

        clone = _clone_with_name(node, "impl_eu")

        assert "conn" in dict(clone._resource_cms())

    def test_clone_preserves_lifecycle_hooks(self) -> None:
        from calfkit.nodes import agent_tool

        async def _impl(ctx) -> str:  # noqa: ANN001 - ToolContext
            return "ok"

        node = agent_tool(_impl)

        @node.on_startup
        async def _warm(ctx) -> None:  # noqa: ANN001 - LifecycleContext
            return None

        clone = _clone_with_name(node, "impl_eu")

        assert clone._hooks_for("on_startup")

    def test_clone_registry_is_independent_of_source(self) -> None:
        """The carry-over must be a copy, not a shared container — registering
        a new bracket on the clone must not mutate the original's registry."""
        from calfkit.nodes import agent_tool

        async def _impl(ctx) -> str:  # noqa: ANN001 - ToolContext
            return "ok"

        node = agent_tool(_impl)

        @node.resource("conn")
        async def _conn(ctx):  # noqa: ANN001 - ResourceSetupContext
            yield object()

        clone = _clone_with_name(node, "impl_eu")

        @clone.resource("extra")
        async def _extra(ctx):  # noqa: ANN001 - ResourceSetupContext
            yield object()

        assert "extra" not in dict(node._resource_cms())


class TestResolveAliasMap:
    """Direct unit tests for ``_resolve_alias_map``.

    The function runs at boot time before ``discover_tools`` has any
    chance to log richer context, so its parse-time WARNINGs and
    ValueErrors are the operator's only signal for env-var typos. Pin
    each documented edge case so a future refactor that drops one of
    them fails CI.
    """

    def test_unset_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CALFCORD_TOOLS_ALIAS", raising=False)
        assert _resolve_alias_map() == {}

    def test_empty_string_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "")
        assert _resolve_alias_map() == {}

    def test_whitespace_only_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "  ,  ")
        assert _resolve_alias_map() == {}

    def test_single_pair_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "edit_file=edit_file_eu")
        assert _resolve_alias_map() == {"edit_file": "edit_file_eu"}

    def test_multiple_pairs_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=b,c=d")
        assert _resolve_alias_map() == {"a": "b", "c": "d"}

    def test_whitespace_around_pairs_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "  edit_file = edit_file_eu  ")
        assert _resolve_alias_map() == {"edit_file": "edit_file_eu"}

    def test_malformed_entry_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pair with no ``=`` is a config typo and would silently
        produce dead config (``CALFCORD_TOOLS_ALIAS`` visible in
        ``docker inspect`` but parsing to fewer entries than the
        operator intended). Hard-fail at boot so the typo can't ship.
        The error includes the full env-var value so the operator sees
        every entry without grepping ``.env``."""
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=b,bogus,c=d")
        with pytest.raises(ValueError, match="bogus") as exc_info:
            _resolve_alias_map()
        # Full env in the message so the operator sees ALL entries, not
        # just the offending one.
        assert "a=b,bogus,c=d" in str(exc_info.value)

    def test_empty_side_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``a=`` and ``=b`` are config typos — dead config if accepted.
        Hard-fail at parse time."""
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=,c=d")
        with pytest.raises(ValueError, match="empty"):
            _resolve_alias_map()

    def test_self_alias_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``foo=foo`` has no legitimate use — every legitimate alias
        renames a tool to a DIFFERENT name. Symmetric with the CLI's
        ``--rename foo=foo`` rejection so build-time and boot-time
        config validation behave identically."""
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "foo=foo")
        with pytest.raises(ValueError, match=r"itself|distinct"):
            _resolve_alias_map()

    def test_invalid_dst_regex_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DST that violates the tool-name regex (e.g. contains
        spaces) would generate a malformed Kafka topic
        ``tool.<dst>.input`` at boot — agent timeouts surface far from
        the cause. Hard-fail at parse time so the bad name can't reach
        the topic-generation code. Symmetric with the CLI's regex
        check; previously the env-only path (operator hand-editing
        ``.env`` post-build) bypassed this validation."""
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=has spaces")
        with pytest.raises(ValueError, match="valid tool name"):
            _resolve_alias_map()

    def test_duplicate_source_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """v1 supports one alias per source. Two pairs with the same
        ``src`` are ambiguous (which clone wins?) — hard fail."""
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "foo=a,foo=b")
        with pytest.raises(ValueError, match="aliased multiple times") as exc_info:
            _resolve_alias_map()
        # Full env in the message — operator with a long alias list
        # needs to see both offenders, not just the first.
        assert "foo=a,foo=b" in str(exc_info.value)

    def test_duplicate_target_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two pairs aliasing to the same target would collide at registry
        time anyway; surface it at parse time for better attribution."""
        monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "a=x,b=x")
        with pytest.raises(ValueError, match="used by multiple aliases") as exc_info:
            _resolve_alias_map()
        assert "a=x,b=x" in str(exc_info.value)


def test_alias_adds_clone_to_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CALFCORD_TOOLS_ALIAS=alpha=alpha_eu`` registers a clone under
    the new name while leaving the original in place. This is the
    additive semantic — needed on the agent host so an agent declaring
    BOTH ``alpha`` and ``alpha_eu`` resolves both via the registry."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_adds")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha", "alpha_eu", "beta", "gamma"}
    # The clone's schema name matches the alias target.
    assert registry["alpha_eu"].tool_schema.name == "alpha_eu"
    # The original is untouched.
    assert registry["alpha"].tool_schema.name == "alpha"


def test_alias_plus_include_filter_yields_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pairing the alias with an include filter on the TARGET produces
    true rename behavior — the original drops out, only the clone
    survives. This is the deploy pattern for the EU tool host: it
    subscribes only to ``tool.alpha_eu.input`` and doesn't race with
    the local box on ``tool.alpha.input``."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha_eu")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_plus_include")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha_eu"}


def test_alias_clone_has_distinct_topics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire-level isolation contract: the clone's ``subscribe_topics``
    and ``publish_topic`` must use the alias target name, not the
    original. A regression that updates ``tool_schema.name`` but leaves
    the topics pointing at the original would silently load-balance
    agents' RPCs across both hosts."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_topics")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    original = registry["alpha"]
    clone = registry["alpha_eu"]
    assert original.subscribe_topics == ["tool.alpha.input"]
    assert clone.subscribe_topics == ["tool.alpha_eu.input"]
    assert original.publish_topic == "tool.alpha.output"
    assert clone.publish_topic == "tool.alpha_eu.output"
    # node_id moves too — operator log forensics use this field.
    assert original.node_id == "tool_alpha"
    assert clone.node_id == "tool_alpha_eu"


def test_alias_clone_shares_underlying_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "rename, not duplicate" contract: the clone re-uses the
    original's ``_tool`` (the pydantic_ai Tool carrying the function
    schema + body). Different wire identity, same execution
    machinery."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_shared_body")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert registry["alpha"]._tool is registry["alpha_eu"]._tool


def test_alias_target_collision_with_existing_tool_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aliasing ``alpha`` to ``beta`` where ``beta`` is already a real
    tool would silently shadow the audited ``beta``. ValueError at
    boot, message naming both origins so the operator can resolve
    without grepping."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=beta")
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_collision")

    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ValueError, match="collides"):
        discover_tools(pkg, registry)


def test_alias_unknown_source_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in the alias source means the clone never registers.
    Under the dominant deploy pattern (alias + include filter on the
    target), this would silently produce an empty registry — and the
    downstream SystemExit("registry is empty") doesn't name the alias
    typo as the cause. Hard-fail at boot with the offending source
    AND the valid set so the operator's diagnosis is one step. The
    earlier WARN-only behavior left operators chasing a five-step
    chain of blame from "registry empty" back to "I typo'd one env
    var"; the ValueError shortcuts that."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "definitely_not_a_tool=foo_eu")
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_unknown_src")

    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ValueError, match="not found in discovered tools") as exc_info:
        discover_tools(pkg, registry)
    msg = str(exc_info.value)
    # Typo'd source must be named so the operator's diagnosis is one
    # step. Valid sources must be listed so they can fix the typo
    # without grepping the source tree.
    assert "definitely_not_a_tool" in msg
    assert "alpha" in msg and "beta" in msg and "gamma" in msg


def test_alias_plus_include_on_source_drops_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MIRROR of ``test_alias_plus_include_filter_yields_rename``:
    if the operator pairs the alias with an include filter on the
    SOURCE (not the target), the original survives and the clone
    drops out. This is the deploy pattern for a host that wants to
    keep serving the original tool while *also* allowing aliases to
    be added on other hosts.

    Most importantly, this test defends against the most plausible
    accidental regression in ``discover_tools``: a refactor that
    applies the include filter BEFORE alias expansion would skip the
    source tool entirely (filter excludes ``alpha_eu`` if checked
    pre-expansion against the original name ``alpha``), producing an
    EMPTY registry instead of just the original. The current code
    applies the filter AFTER expansion inside the per-entry loop —
    correct, but unpinned until now."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_include_src")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha"}
    # The original survives; specifically, the original schema name
    # (NOT the alias target) is what's registered.
    assert registry["alpha"].tool_schema.name == "alpha"


def test_alias_plus_include_listing_both_registers_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented AGENT-HOST deploy pattern: an agent host that
    needs to declare BOTH ``alpha`` and ``alpha_eu`` in its
    frontmatter has its alias env set AND includes BOTH names in the
    filter. The discovery loader must register both with distinct
    topics so factory tool resolution succeeds and the LLM's choice
    of name routes to the right host. This pattern is described in
    the discovery docstring (lines 122-126) but was unpinned by any
    test; a regression here is "tool not found at agent boot" that
    only operators see."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha,alpha_eu")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_include_both")

    registry: dict[str, ToolNodeDef] = {}
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha", "alpha_eu"}
    assert registry["alpha"].tool_schema.name == "alpha"
    assert registry["alpha_eu"].tool_schema.name == "alpha_eu"
    # Both topics distinct — required for the broker to route to the
    # right host based on which name the agent picked.
    assert registry["alpha"].subscribe_topics[0] != registry["alpha_eu"].subscribe_topics[0]


def test_alias_target_collides_with_prepopulated_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision check has two code paths: collision-with-walked-
    tool (covered by ``test_alias_target_collision_...``) and
    collision-with-pre-populated-registry (here). The pre-populated
    case has a different ``origins`` seed (``<pre-populated>``
    sentinel) and a different message-construction branch. A
    regression that mishandles the sentinel branch (e.g. a refactor
    using the wrong dict for the origin lookup) would slip through
    the existing test."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_prepop_collide")

    # Pre-populate ``alpha_eu`` in the registry — the alias would try
    # to register a clone under the same name, colliding with the
    # pre-populated entry.
    prepopulated_node = MagicMock(spec=ToolNodeDef)
    registry: dict[str, ToolNodeDef] = {"alpha_eu": prepopulated_node}
    with pytest.raises(ValueError, match="collides") as exc_info:
        discover_tools(pkg, registry)
    # The collision message must name both sides: the alias clone's
    # origin AND the pre-populated sentinel so the operator can tell
    # which side is the surprise.
    msg = str(exc_info.value)
    assert "alias of alpha" in msg
    assert "<pre-populated>" in msg


def test_alias_target_collision_names_alias_in_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tighter version of ``test_alias_target_collision_with_existing_
    tool_raises`` — that test asserts the bare word ``collides`` which
    would survive a regression that drops the ``(alias of X)``
    attribution. The attribution is operator-forensic — without it,
    the error tells the operator there IS a collision but not which
    side is the alias. Pin it explicitly."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=beta")
    monkeypatch.delenv("CALFCORD_TOOLS_INCLUDE", raising=False)
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_collision_attribution")

    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ValueError) as exc_info:
        discover_tools(pkg, registry)
    msg = str(exc_info.value)
    assert "collides" in msg
    # The clone's origin is "<package>.alpha:alpha_tool (alias of alpha)".
    # The original ``beta``'s origin is "<package>.beta:beta_tool". Both
    # must appear so the operator can resolve the conflict.
    assert "alias of alpha" in msg
    assert "beta" in msg


def test_alias_unknown_source_does_not_false_warn_under_rename_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unknown-source check uses ``discovered_originals``
    (pre-filter set) rather than ``origins`` (post-filter registry).
    The distinction matters for the dominant deploy pattern: alias
    ``alpha=alpha_eu`` plus include filter ``alpha_eu`` — the
    original ``alpha`` is discovered then filter-dropped. A naive
    implementation checking against the post-filter registry would
    falsely warn ("alpha not in discovered tools") for the very
    pattern the alias system was built for. This test pins the
    distinction so a refactor swapping the set source can't silently
    re-introduce a false-positive boot warning."""
    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha_eu")
    pkg = _three_tool_pkg(tmp_path, monkeypatch, "fake_pkg_alias_no_false_warn")

    registry: dict[str, ToolNodeDef] = {}
    # Must NOT raise. Specifically must not raise ValueError("sources
    # not found in discovered tools"). ``alpha`` was discovered (then
    # filtered) so the warn-set is empty.
    discover_tools(pkg, registry)
    assert set(registry) == {"alpha_eu"}


def test_alias_does_not_short_circuit_import_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric to the include-filter import-error test: an alias env
    var must NOT swallow a broken module's ImportError. Broken code is
    always loud regardless of whether the operator was hoping to
    rename it."""
    pkg_name = "fake_pkg_alias_and_broken"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "alpha": _tool_source("alpha"),
            "broken": "raise ImportError('intentionally broken')\n",
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    monkeypatch.setenv("CALFCORD_TOOLS_ALIAS", "alpha=alpha_eu")
    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ImportError, match="intentionally broken"):
        discover_tools(pkg, registry)
