"""Tests for :func:`calfkit_organization.tools.discovery.discover_tools`.

Each test builds a throwaway *real* package on disk under ``tmp_path``
and prepends that directory to ``sys.path`` via ``monkeypatch``. We use
real files (rather than ``sys.modules`` injection) because
:func:`pkgutil.iter_modules` walks the filesystem; injected modules with
no on-disk presence are invisible to it.

Each fake package is given a unique name (parameterised by the test
function) so ``sys.modules`` caching from one test does not bleed into
another. We deliberately do not import or touch
:data:`calfkit_organization.tools.TOOL_REGISTRY` from these tests — every
test passes a fresh ``{}`` to ``discover_tools`` so the real registry
stays out of the picture.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest
from calfkit.nodes.tool import ToolNodeDef

from calfkit_organization.tools.discovery import discover_tools


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

    # Filter would skip ``broken`` even by name, but it's processed
    # alphabetically BEFORE alpha, so its import error fires before
    # the filter ever gets to consider it.
    monkeypatch.setenv("CALFCORD_TOOLS_INCLUDE", "alpha")
    registry: dict[str, ToolNodeDef] = {}
    with pytest.raises(ImportError, match="intentionally broken"):
        discover_tools(pkg, registry)
