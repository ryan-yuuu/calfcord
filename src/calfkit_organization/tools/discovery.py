"""Auto-discovery of builtin tools by walking the ``tools/builtin`` package.

Background
----------

The original ``calfkit_organization.tools`` package required every new
builtin tool to be registered in two places: the module that defines it,
and an explicit ``TOOL_REGISTRY[name] = name_tool`` line at the bottom
of :mod:`calfkit_organization.tools.__init__`. That coupling is a paper
cut — easy to forget, easy to typo — and the failure mode is "LLM gets
an ``unknown tool`` error at runtime" rather than a build-time signal.

This module replaces the explicit lines with a pytest-style discovery
walk: drop a ``foo.py`` under ``tools/builtin/``, expose a
``foo_tool: ToolNodeDef`` (or any other attribute holding a
:class:`ToolNodeDef`), and it is picked up at import time. No edit to
the registry module required.

Design choices
--------------

* **One file per tool, drop-in registration** — mirrors pytest's
  ``test_*.py`` discovery. The cost of an import-time directory walk is
  paid once at process boot; the saving in operator friction is
  permanent.

* **Underscore-prefixed modules AND attributes are skipped** — pytest
  treats ``conftest.py`` and ``_*.py`` as private support code, never as
  test files. We use the same convention at both layers: a module like
  ``_observation.py`` lives next to the tools without being scanned, and
  an attribute like ``_fixture_tool: ToolNodeDef`` declared inside a
  registered module is recognized as a construction artifact and not
  registered. Symmetric rules at both layers let authors hide
  ``ToolNodeDef`` instances they don't want auto-registered (e.g. for
  parametrized variants or unit-test fixtures) without needing a
  separate hidden module.

* **Instances are deduped by :func:`id`** — when a tool is re-exported
  through a second module (``from .foo import foo_tool``), we'd see the
  same :class:`ToolNodeDef` object twice. Without the dedupe, the
  second encounter would trip the collision check below and turn a
  harmless re-export into a hard failure. We compare by object identity
  (not equality) because two distinct registrations of "the same"
  schema name are exactly the case we *do* want to flag.

* **Schema-name collisions raise** — silently letting a later module
  override an earlier registration is a security footgun: a typo'd
  builtin could shadow an audited one, and the LLM would happily
  invoke the impostor. The :class:`ValueError` names both module paths
  so the operator can resolve the conflict without grepping.

* **Modules and attributes are sorted** — :func:`pkgutil.iter_modules`
  and :func:`dir` do not guarantee a deterministic order across
  interpreter versions or filesystems. Sorting makes boot logs
  reproducible (handy for diffing across deployments) and makes the
  "first registration wins / second collides" rule predictable.

* **:class:`ImportError` is allowed to propagate** — a broken tool
  module is an operator-visible config bug, not a condition to swallow.
  A silent ``except ImportError`` would let the agent boot with a
  partial tool surface; the LLM would then fail to invoke the missing
  tool with a confusing ``unknown tool`` error far from the root
  cause. Crashing at import time is louder and shorter to diagnose.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
from types import ModuleType

from calfkit.nodes.tool import ToolNodeDef

logger = logging.getLogger(__name__)

_INCLUDE_ENV = "CALFCORD_TOOLS_INCLUDE"
"""Env var that, when set, restricts registration to a comma-separated list
of tool schema names. Used by per-tool images built with
``calfcord-package-tools`` to host only the subset they were built for.
Unset (or empty) means "register everything the walk finds" — the
default for the all-in-one image.

The filter happens AFTER the import has succeeded, so a tool's heavy
deps still load (no Python-dep slimming in v1). The win is wire-level:
the worker subscribes to only the listed ``tool.<name>.input`` topics,
and the boot log shows operators exactly which surface the container
serves."""


def _resolve_include_filter() -> set[str] | None:
    """Parse ``CALFCORD_TOOLS_INCLUDE`` into a set of tool names, or ``None``.

    Empty / unset env returns ``None`` so the caller can use ``is None``
    as the "no filter" signal — cleaner than checking for an empty set.
    Whitespace-only values are also treated as unset (defensive against
    a stray space in an env file).
    """
    raw = os.environ.get(_INCLUDE_ENV, "").strip()
    if not raw:
        return None
    names = {name.strip() for name in raw.split(",") if name.strip()}
    if not names:
        return None
    return names


def discover_tools(
    package: ModuleType,
    registry: dict[str, ToolNodeDef],
) -> None:
    """Walk ``package`` and register every :class:`ToolNodeDef` into ``registry``.

    Iterates the immediate submodules of ``package`` in alphabetical
    order, imports each one (which executes its top-level
    ``agent_tool(...)`` calls), and scans the resulting module for
    attributes whose value is a :class:`ToolNodeDef`. Each unique
    ``ToolNodeDef`` instance is registered under
    ``value.tool_schema.name``.

    Args:
        package: The package to walk. Must be an actual package
            (have ``__path__``); plain modules are not supported because
            :func:`pkgutil.iter_modules` requires a search path.
        registry: A name-to-node dict mutated in place. Pre-existing
            entries are honored — a new module declaring the same name
            raises :class:`ValueError`.

    Raises:
        ValueError: When two distinct :class:`ToolNodeDef` instances
            advertise the same ``tool_schema.name``, or when a discovered
            tool's name is already present in ``registry``. The message
            includes the offending ``module:attribute`` paths so the
            collision can be resolved without grepping.
        ImportError: Propagated unchanged from
            :func:`importlib.import_module` when a submodule fails to
            import. A broken tool module is treated as a hard config
            error (see module docstring).
    """
    seen: set[int] = set()
    # Track the discovery origin of every name we register so a later collision
    # can blame both sides — operators shouldn't have to grep to find the prior
    # registration. Names already present in ``registry`` on entry have no
    # known origin (they were pre-populated by the caller); we surface that
    # explicitly with ``<pre-populated>`` in the message.
    origins: dict[str, str] = {name: "<pre-populated>" for name in registry}
    include_set = _resolve_include_filter()
    if include_set is not None:
        logger.info(
            "CALFCORD_TOOLS_INCLUDE is set; only registering tools in: %s",
            sorted(include_set),
        )
    modules = sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name)
    for mod_info in modules:
        # Underscore prefix marks "private support module, not a tool" — same
        # convention pytest uses for conftest/_*.py. Keeps `_observation.py`
        # and similar shared helpers out of the scan.
        if mod_info.name.startswith("_"):
            continue
        full_name = f"{package.__name__}.{mod_info.name}"
        module = importlib.import_module(full_name)
        # Sort attribute names so first-wins / collision-detection ordering is
        # deterministic regardless of how Python lays out the module dict.
        for attr_name in sorted(dir(module)):
            # Underscore prefix on the ATTRIBUTE means "construction artifact,
            # not a registered tool" — symmetric with the module-level rule.
            # Authors use ``_foo_tool = agent_tool(...)`` for fixtures or
            # parametrized variants that should not be auto-registered.
            if attr_name.startswith("_"):
                continue
            value = getattr(module, attr_name)
            if not isinstance(value, ToolNodeDef):
                continue
            # Same instance re-exported from another module — register once.
            if id(value) in seen:
                continue
            seen.add(id(value))
            tool_name = value.tool_schema.name
            origin = f"{full_name}:{attr_name}"
            # CALFCORD_TOOLS_INCLUDE filter: skip tools not on the list.
            # Applied AFTER the module import + ToolNodeDef discovery so
            # the import-error contract (broken modules fail loud)
            # stays intact regardless of whether the tool ends up
            # registered. Logs at DEBUG to keep boot output tidy when
            # the filter is doing its job; the INFO line above already
            # told operators what's coming.
            if include_set is not None and tool_name not in include_set:
                logger.debug(
                    "skipping tool=%s (not in CALFCORD_TOOLS_INCLUDE)",
                    tool_name,
                )
                continue
            if tool_name in registry:
                raise ValueError(
                    f"tool name {tool_name!r} from {origin} collides with "
                    f"existing registration from {origins[tool_name]}"
                )
            registry[tool_name] = value
            origins[tool_name] = origin
            logger.info(
                "registered builtin tool=%s from=%s",
                tool_name,
                origin,
            )
