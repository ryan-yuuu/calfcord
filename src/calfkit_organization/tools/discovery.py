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

import dataclasses
import importlib
import logging
import os
import pkgutil
import re
from types import ModuleType

from calfkit.nodes.tool import ToolNodeDef

logger = logging.getLogger(__name__)

TOOL_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
"""Allowed character set + length for a tool schema name.

Anthropic and OpenAI accept the same character set
(``[a-zA-Z0-9_-]``); the length bounds differ — Anthropic up to 128,
OpenAI up to 64. We use the 128 upper bound to match calfcord's
primary target (Anthropic / pydantic-ai); operators targeting
OpenAI-only deployments should keep rename targets ≤ 64 chars to
stay portable. The regex enforces the character set strictly at
both the build-time CLI and the boot-time env-var parser so a
malformed name can't slip into a generated Kafka topic
(``tool.<name>.input``) and produce confusing routing failures
later."""

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


_ALIAS_ENV = "CALFCORD_TOOLS_ALIAS"
"""Env var that, when set, registers cloned copies of tools under
additional schema names. Each comma-separated ``src=dst`` pair clones
the ``src`` tool's :class:`ToolNodeDef` with the new name baked into
``tool_schema.name``, ``subscribe_topics``, ``publish_topic``, and
``node_id``. The result: the same underlying Python function is
exposed on the calfkit wire under multiple Kafka topics, one per
registered name.

The primary use is multi-host deployment of the same tool — e.g. an
``edit_file`` worker running on both a workstation and an EU VM, with
the EU host renamed to ``edit_file_eu`` so the agent's LLM can pick
which host services a given call. Combine with :data:`_INCLUDE_ENV` on
the tool host to drop the original name (true rename behavior); on
the agent host leave the include filter unset so both names stay
visible for declaration validation and topic routing.

v1 restrictions: one alias per source, one alias per target, no
transitive chains (``a=b,b=c`` does not register ``c`` from ``a``).
The parser enforces the first two; chains are silently non-chasing
and the tail warning surfaces unmatched alias sources."""


def _resolve_alias_map() -> dict[str, str]:
    """Parse ``CALFCORD_TOOLS_ALIAS`` into a ``{src: dst}`` dict.

    Empty / unset / whitespace-only env → ``{}``. Otherwise the env
    var is parsed strictly: any malformed entry, duplicate source,
    duplicate target, ``src==dst`` no-op, or DST that violates
    :data:`TOOL_NAME_REGEX` raises :class:`ValueError`. Strict-fail
    at boot is the right call here — the include filter typo case
    (``CALFCORD_TOOLS_INCLUDE`` with an unknown name) merely shrinks
    the registered surface and surfaces as ``unknown tool`` at first
    use, but an alias misconfig manifests as silent dead config
    (``CALFCORD_TOOLS_ALIAS`` is visible in ``docker inspect`` doing
    nothing) which is harder to debug. Every error message includes
    the raw env-var value so the operator sees ALL entries — not just
    the offending one — without having to grep ``.env`` for the
    second offender.

    Raises:
        ValueError: on any of: malformed entry (no ``=`` or empty
            side), invalid DST (regex), ``src==dst``, duplicate
            source, duplicate target.
    """
    raw = os.environ.get(_ALIAS_ENV, "").strip()
    if not raw:
        return {}
    result: dict[str, str] = {}
    used_targets: set[str] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"CALFCORD_TOOLS_ALIAS entry {chunk!r} has no '=' — "
                f"expected src=dst (full env: {raw!r})"
            )
        src, _, dst = chunk.partition("=")
        src = src.strip()
        dst = dst.strip()
        if not src or not dst:
            raise ValueError(
                f"CALFCORD_TOOLS_ALIAS entry {chunk!r} has empty src or dst "
                f"(full env: {raw!r})"
            )
        if not TOOL_NAME_REGEX.match(dst):
            raise ValueError(
                f"CALFCORD_TOOLS_ALIAS target {dst!r} is not a valid tool "
                f"name; must match {TOOL_NAME_REGEX.pattern} (full env: "
                f"{raw!r})"
            )
        if src == dst:
            raise ValueError(
                f"CALFCORD_TOOLS_ALIAS entry {chunk!r} aliases a tool to "
                f"itself; drop the entry or pick a distinct target "
                f"(full env: {raw!r})"
            )
        if src in result:
            raise ValueError(
                f"CALFCORD_TOOLS_ALIAS source {src!r} is aliased multiple "
                f"times; only one alias per source is supported "
                f"(full env: {raw!r})"
            )
        if dst in used_targets:
            raise ValueError(
                f"CALFCORD_TOOLS_ALIAS target {dst!r} is used by multiple "
                f"aliases; only one src may alias to a given dst "
                f"(full env: {raw!r})"
            )
        result[src] = dst
        used_targets.add(dst)
    return result


def _clone_with_name(node: ToolNodeDef, new_name: str) -> ToolNodeDef:
    """Clone ``node`` with all four name-bound fields rewritten.

    A tool's wire-level identity is fixed by four fields, each baked in
    at :func:`calfkit.nodes.tool.agent_tool` construction time from the
    underlying Python function's ``__name__``:

    * ``tool_schema.name`` — the LLM-facing identity
    * ``subscribe_topics`` — the Kafka topic this node consumes from
    * ``publish_topic`` — the Kafka topic this node publishes results to
    * ``node_id`` — forensic logging identity

    A schema-name-only rewrite would leave the topics pointing at the
    original Kafka destinations, so an alias clone with only
    ``tool_schema.name`` updated would silently route to the wrong
    consumer (or get load-balanced against the original on the same
    topic). All four must be replaced together.

    The inner ``_tool`` field (the pydantic_ai :class:`Tool` carrying
    the function-execution machinery) is preserved unchanged — both
    the original and the clone call the same Python body. That's the
    "rename, not duplicate" semantic: one body, multiple wire
    identities.
    """
    try:
        new_schema = dataclasses.replace(node.tool_schema, name=new_name)
        return dataclasses.replace(
            node,
            tool_schema=new_schema,
            subscribe_topics=[f"tool.{new_name}.input"],
            publish_topic=f"tool.{new_name}.output",
            node_id=f"tool_{new_name}",
        )
    except TypeError as e:
        # ``dataclasses.replace`` raises TypeError if the target isn't a
        # dataclass instance. A future calfkit release that switches
        # ``ToolNodeDef.tool_schema`` to a non-dataclass (e.g. a pydantic
        # model or a frozen-with-slots class) would surface here as a
        # bare TypeError propagating from ``calfkit_organization.tools``
        # module import — operator sees a stack trace pointing inside
        # this helper with no attribution to the version-mismatch
        # cause. Re-raise with explicit context so the failure mode is
        # one search away.
        raise RuntimeError(
            f"cannot clone tool {node.tool_schema.name!r} for alias "
            f"{new_name!r}: ToolNodeDef.tool_schema is not a dataclass in "
            f"this calfkit version. Aliasing requires calfkit's "
            f"ToolNodeDef + ToolDefinition to remain @dataclass."
        ) from e


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
    alias_map = _resolve_alias_map()
    # Track every ORIGINAL tool name encountered during the walk (pre-filter,
    # pre-alias-expansion). Used by the tail warning to itemise which alias
    # sources didn't match any discovered tool — important because the filter
    # may drop the original from the final registry, so checking against
    # ``origins`` alone would false-warn for the "rename via alias + include"
    # pattern.
    discovered_originals: set[str] = set()
    if include_set is not None:
        logger.info(
            "CALFCORD_TOOLS_INCLUDE is set; only registering tools in: %s",
            sorted(include_set),
        )
    if alias_map:
        # Format as ``a→b, c→d`` to match the generated Dockerfile's
        # banner format — a single line operators can read at a glance
        # and grep across build-time and runtime logs without parsing
        # ``[('a','b'),('c','d')]`` tuple syntax.
        alias_pairs = ", ".join(
            f"{src}→{dst}" for src, dst in sorted(alias_map.items())
        )
        logger.info(
            "CALFCORD_TOOLS_ALIAS is set; cloning tools under additional names: %s",
            alias_pairs,
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
            original_name = value.tool_schema.name
            original_origin = f"{full_name}:{attr_name}"
            discovered_originals.add(original_name)
            # Build the set of (name, node, origin) tuples this discovered
            # tool should register under: always the original name, plus a
            # clone for each alias whose src matches. The aliases share the
            # same filter / collision / logging path as the original — no
            # special-casing keeps the boot-log shape uniform.
            entries: list[tuple[str, ToolNodeDef, str]] = [
                (original_name, value, original_origin)
            ]
            for src, dst in alias_map.items():
                if src == original_name:
                    entries.append(
                        (
                            dst,
                            _clone_with_name(value, dst),
                            f"{original_origin} (alias of {src})",
                        )
                    )
            for tool_name, node, origin in entries:
                # CALFCORD_TOOLS_INCLUDE filter: skip tools not on the list.
                # Applied AFTER the module import + ToolNodeDef discovery so
                # the import-error contract (broken modules fail loud)
                # stays intact regardless of whether the tool ends up
                # registered. Applied AFTER alias expansion so an operator
                # who wants true rename behavior (drop the original, keep
                # the clone) can pair ``--rename a=b`` with ``--include b``.
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
                registry[tool_name] = node
                origins[tool_name] = origin
                logger.info(
                    "registered builtin tool=%s from=%s",
                    tool_name,
                    origin,
                )

    # If the include filter named tools that don't exist in this
    # package, every one of them was silently skipped above. Warn at
    # WARNING level so operators with a typo in their
    # ``CALFCORD_TOOLS_INCLUDE`` env var see the cause. The downstream
    # ``_resolve_tool_nodes`` SystemExit also names the env-var value,
    # but does NOT itemise which entries were typo'd nor list what was
    # discovered — this warning fills that gap so the operator can fix
    # the typo without grepping logs for the registered-builtin lines.
    if include_set is not None:
        unknown_in_filter = include_set - {
            name for name, origin in origins.items() if origin != "<pre-populated>"
        }
        if unknown_in_filter:
            logger.warning(
                "CALFCORD_TOOLS_INCLUDE names not found in discovered tools: %s; "
                "discovered: %s",
                sorted(unknown_in_filter),
                sorted(registry),
            )

    # If the operator typed an alias whose src doesn't match any
    # discovered tool, the clone never registers. Under the dominant
    # deploy pattern (alias + ``CALFCORD_TOOLS_INCLUDE=<dst>``) this
    # leaves the registry EMPTY — discovery's
    # ``_resolve_tool_nodes`` later raises SystemExit("registry is
    # empty"), but that error doesn't name the alias typo as the
    # cause and the chain-of-blame from "registry empty" back to "I
    # typo'd one env var" is too long to be useful. Hard-fail with
    # the typo'd source named explicitly so the operator's diagnosis
    # is one step, not five. ``discovered_originals`` is the
    # pre-filter set so an operator using the rename-via-alias-plus-
    # include pattern doesn't false-positive when the source got
    # filter-dropped from the final registry.
    if alias_map:
        unknown_sources = set(alias_map) - discovered_originals
        if unknown_sources:
            raise ValueError(
                f"CALFCORD_TOOLS_ALIAS sources not found in discovered "
                f"tools: {sorted(unknown_sources)}; valid sources: "
                f"{sorted(discovered_originals)}"
            )
