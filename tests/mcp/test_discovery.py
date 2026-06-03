"""Tests for :func:`calfcord.mcp.discovery.discover_mcp_catalog`.

Mirrors :mod:`tests.tools.test_discovery`: each test builds a throwaway
*real* package on disk under ``tmp_path`` and prepends it to ``sys.path``
via ``monkeypatch`` (because :func:`pkgutil.iter_modules` walks the
filesystem — injected ``sys.modules`` entries are invisible to it). Each
fake package gets a unique name so ``sys.modules`` caching does not bleed
between tests.

The committed ``schemas/<server>.py`` modules declare top-level
``NAME = McpToolDef(...)`` constants, keyed in the catalog by the *module
name* (= server name). These tests reproduce that shape with handwritten
fixture modules.
"""

from __future__ import annotations

import importlib
import logging
import sys
import textwrap
from pathlib import Path

import pytest
from calfkit.mcp import McpToolDef

from calfcord.mcp.discovery import discover_mcp_catalog


def _write_package(root: Path, pkg_name: str, modules: dict[str, str]) -> Path:
    """Create a real package directory under ``root`` with the given modules.

    ``modules`` maps a stem (``"gmail"`` → ``gmail.py``) to its source text.
    An empty ``__init__.py`` is created automatically. Returns the package
    directory.
    """
    pkg_dir = root / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for stem, source in modules.items():
        (pkg_dir / f"{stem}.py").write_text(textwrap.dedent(source))
    return pkg_dir


def _import_fresh(pkg_name: str):
    """Import ``pkg_name`` after evicting any cached entries.

    Cached entries can survive across tests in the same interpreter while
    the on-disk files differ between tests.
    """
    for cached in [k for k in sys.modules if k == pkg_name or k.startswith(pkg_name + ".")]:
        del sys.modules[cached]
    return importlib.import_module(pkg_name)


# A schema-module snippet: one or more top-level ``McpToolDef`` constants.
def _schema_source(*tool_names: str) -> str:
    consts = "\n".join(
        f'{name.upper().replace("-", "_")} = McpToolDef(name={name!r})'
        for name in tool_names
    )
    return f"from calfkit.mcp import McpToolDef\n\n\n{consts}\n"


def test_smoke_two_servers_keyed_by_module_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg_name = "fake_mcp_schemas_smoke"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "gmail": _schema_source("search", "send"),
            "calendar": _schema_source("list-events"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    catalog = discover_mcp_catalog(pkg)

    assert set(catalog) == {"gmail", "calendar"}
    assert all(isinstance(t, McpToolDef) for defs in catalog.values() for t in defs)
    assert {t.name for t in catalog["gmail"]} == {"search", "send"}
    assert {t.name for t in catalog["calendar"]} == {"list-events"}


def test_instances_collected_not_class_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only top-level ``McpToolDef`` *instances* are collected; non-instance
    attributes (constants, a class wrapper, helper functions) are ignored —
    discovery does not depend on any codegen ``.ALL`` aggregation contract."""
    source = """\
from calfkit.mcp import McpToolDef


SCHEMA_VERSION = 3
SERVER_LABEL = "demo"


class DemoTools:
    \"\"\"Codegen ergonomics wrapper — not collected.\"\"\"


def helper() -> int:
    return 1


ECHO = McpToolDef(name="echo")
"""
    pkg_name = "fake_mcp_schemas_instances"
    _write_package(tmp_path, pkg_name, {"demo": source})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    catalog = discover_mcp_catalog(pkg)

    assert set(catalog) == {"demo"}
    assert [t.name for t in catalog["demo"]] == ["echo"]


def test_reexported_instance_deduped_by_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A constant re-exported within the same server module is collected
    once (deduped by object identity), not twice."""
    source = """\
from calfkit.mcp import McpToolDef


SEARCH = McpToolDef(name="search")
SEARCH_ALIAS = SEARCH  # same instance, re-exported under a second name
"""
    pkg_name = "fake_mcp_schemas_reexport"
    _write_package(tmp_path, pkg_name, {"gmail": source})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    catalog = discover_mcp_catalog(pkg)

    assert [t.name for t in catalog["gmail"]] == ["search"]
    assert len(catalog["gmail"]) == 1


def test_underscore_prefixed_modules_and_attrs_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``_helpers.py`` support module is not scanned, and an underscore-
    prefixed constant inside a scanned module is treated as a construction
    artifact, not a published tool."""
    pkg_name = "fake_mcp_schemas_underscore"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "gmail": (
                "from calfkit.mcp import McpToolDef\n\n\n"
                'SEARCH = McpToolDef(name="search")\n'
                '_DRAFT = McpToolDef(name="draft")\n'
            ),
            "_helpers": _schema_source("should_not_appear"),
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    catalog = discover_mcp_catalog(pkg)

    assert set(catalog) == {"gmail"}
    assert [t.name for t in catalog["gmail"]] == ["search"]
    # The underscore-prefixed module is never even imported.
    assert f"{pkg_name}._helpers" not in sys.modules


def test_empty_module_warns_and_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A schema module exposing no ``McpToolDef`` is almost certainly stale
    or mis-generated: it is omitted from the catalog (non-fatal) and warned
    about so an operator sees the cause."""
    pkg_name = "fake_mcp_schemas_empty"
    _write_package(
        tmp_path,
        pkg_name,
        {
            "gmail": _schema_source("search"),
            "stale": 'SCHEMA_VERSION = 1\n',  # no McpToolDef at all
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    with caplog.at_level(logging.WARNING):
        catalog = discover_mcp_catalog(pkg)

    assert set(catalog) == {"gmail"}
    assert "stale" not in catalog
    assert any(
        "exposed no McpToolDef" in r.getMessage() and "stale" in r.getMessage()
        for r in caplog.records
    )


def test_deterministic_attribute_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tools within a server are collected in sorted-attribute-name order so
    the catalog (and the boot log) is reproducible across interpreters."""
    source = """\
from calfkit.mcp import McpToolDef


ZULU = McpToolDef(name="zulu")
ALPHA = McpToolDef(name="alpha")
MIKE = McpToolDef(name="mike")
"""
    pkg_name = "fake_mcp_schemas_order"
    _write_package(tmp_path, pkg_name, {"demo": source})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    catalog = discover_mcp_catalog(pkg)

    # Sorted by ATTRIBUTE name (ALPHA, MIKE, ZULU), not tool name.
    assert [t.name for t in catalog["demo"]] == ["alpha", "mike", "zulu"]


def test_empty_package_yields_empty_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg_name = "fake_mcp_schemas_no_modules"
    _write_package(tmp_path, pkg_name, {})
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    assert discover_mcp_catalog(pkg) == {}


def test_invalid_module_name_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema module whose name is not a legal ``[a-z0-9_]{1,64}`` server
    segment (here an uppercase ``Uppercase.py``) would key the catalog under
    a server no ``mcp/...`` selector could reach, leaving its tools silently
    unreachable. Discovery rejects it loudly instead, naming the offending
    module and the required grammar."""
    pkg_name = "fake_mcp_schemas_invalid_name"
    _write_package(
        tmp_path,
        pkg_name,
        {"Uppercase": _schema_source("echo")},
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    pkg = _import_fresh(pkg_name)

    with pytest.raises(ValueError, match="invalid server name"):
        discover_mcp_catalog(pkg)
