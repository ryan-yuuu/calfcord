"""Deploy-time narrowing and aliasing of the tool surface.

:func:`apply_deploy_filters` turns the explicit list of tool nodes
(:data:`calfcord.tools.ALL_TOOLS`) into the name-keyed
:data:`~calfcord.tools.TOOL_REGISTRY`, applying two operator-facing,
env-driven transforms:

* ``CALFCORD_TOOLS_INCLUDE`` — restrict the hosted/advertised surface to a
  comma-separated allow-list of tool names. Per-tool Docker images
  (``calfcord-package-tools``) bake this in so a container subscribes to
  only the ``tool.<name>.input`` topics it was built for. Unset means
  "register every node".

* ``CALFCORD_TOOLS_ALIAS`` — clone a node under one or more additional wire
  identities (``src=dst`` pairs), so the same tool body is reachable on a
  second set of Kafka topics. The primary use is multi-host deployment of
  the same tool (e.g. ``terminal`` also exposed as ``terminal_eu`` so the
  LLM can pick which host services a call).

The transform is a **pure function of (nodes, env)**: no filesystem walk,
no import-time package scanning, no import cycle. That is the deliberate
replacement for the previous auto-discovery model — the tool surface is now
an explicit, auditable list (see :data:`calfcord.tools.ALL_TOOLS`) and this
module only narrows/renames it.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from collections.abc import Sequence

from calfkit.nodes.tool import ToolNodeDef

logger = logging.getLogger(__name__)

TOOL_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
"""Allowed character set + length for a tool schema name.

Anthropic and OpenAI accept the same character set (``[a-zA-Z0-9_-]``);
the length bounds differ — Anthropic up to 128, OpenAI up to 64. We use
the 128 upper bound to match calfcord's primary target (Anthropic /
pydantic-ai); operators targeting OpenAI-only deployments should keep
alias targets ≤ 64 chars to stay portable. The regex is enforced on
alias targets so a malformed name can't slip into a generated Kafka
topic (``tool.<name>.input``) and produce confusing routing failures
later."""

_INCLUDE_ENV = "CALFCORD_TOOLS_INCLUDE"
_ALIAS_ENV = "CALFCORD_TOOLS_ALIAS"


def _resolve_include_filter() -> set[str] | None:
    """Parse ``CALFCORD_TOOLS_INCLUDE`` into a set of tool names, or ``None``.

    Empty / unset / whitespace-only env returns ``None`` so the caller can
    use ``is None`` as the "no filter" signal — cleaner than checking for
    an empty set.
    """
    raw = os.environ.get(_INCLUDE_ENV, "").strip()
    if not raw:
        return None
    names = {name.strip() for name in raw.split(",") if name.strip()}
    return names or None


def _resolve_alias_map() -> dict[str, str]:
    """Parse ``CALFCORD_TOOLS_ALIAS`` into a ``{src: dst}`` dict.

    Empty / unset / whitespace-only env → ``{}``. Otherwise parsed
    strictly: any malformed entry, duplicate source, duplicate target,
    ``src==dst`` no-op, or DST that violates :data:`TOOL_NAME_REGEX`
    raises :class:`ValueError`. Strict-fail at boot is the right call —
    an alias misconfig otherwise manifests as silent dead config
    (``CALFCORD_TOOLS_ALIAS`` visible in ``docker inspect`` doing
    nothing), which is harder to debug. Every error message includes the
    raw env value so the operator sees all entries, not just the
    offending one.

    Raises:
        ValueError: on any of: malformed entry (no ``=`` or empty side),
            invalid DST (regex), ``src==dst``, duplicate source,
            duplicate target.
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
                f"name; must match {TOOL_NAME_REGEX.pattern} (full env: {raw!r})"
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

    A tool's wire-level identity is fixed by four fields, each baked in at
    :func:`calfkit.nodes.tool.agent_tool` construction time from the
    underlying function's ``__name__``:

    * ``tool_schema.name`` — the LLM-facing identity
    * ``subscribe_topics`` — the Kafka topic this node consumes from
    * ``publish_topic`` — the Kafka topic this node publishes results to
    * ``node_id`` — forensic logging identity

    A schema-name-only rewrite would leave the topics pointing at the
    original Kafka destinations, so all four must be replaced together.
    The inner tool body (the pydantic_ai ``Tool``) is preserved unchanged
    — both the original and the clone call the same Python body. That's
    the "rename, not duplicate" semantic: one body, multiple wire
    identities.

    Aliasing is only valid for tools with no node-scoped lifecycle state.
    A tool that registers ``@resource`` brackets or lifecycle hooks (only
    ``todo`` and ``private_chat`` today — both single-host by nature) can't
    be cloned under a second wire identity without its resource being built
    twice, so this raises rather than carrying brittle copy-the-internals
    logic for a path no real deployment exercises.

    Raises:
        ValueError: if ``node`` has node-scoped resources or lifecycle hooks.
    """
    if node.__dict__.get("_lifecycle_resource_cms") or node.__dict__.get(
        "_lifecycle_hooks"
    ):
        raise ValueError(
            f"cannot alias tool {node.tool_schema.name!r}: it registers "
            f"node-scoped resources or lifecycle hooks, which an alias clone "
            f"cannot share. Aliasing is for stateless / worker-resource tools "
            f"(e.g. multi-host 'terminal'); drop the CALFCORD_TOOLS_ALIAS entry "
            f"for {node.tool_schema.name!r}."
        )
    try:
        new_schema = dataclasses.replace(node.tool_schema, name=new_name)
        clone = dataclasses.replace(
            node,
            tool_schema=new_schema,
            subscribe_topics=[f"tool.{new_name}.input"],
            publish_topic=f"tool.{new_name}.output",
            node_id=f"tool_{new_name}",
        )
    except TypeError as e:
        # ``dataclasses.replace`` raises TypeError if the target isn't a
        # dataclass instance. A future calfkit release that switches
        # ``ToolNodeDef.tool_schema`` to a non-dataclass would surface
        # here; re-raise with explicit context so the version-mismatch
        # cause is one search away.
        raise RuntimeError(
            f"cannot clone tool {node.tool_schema.name!r} for alias "
            f"{new_name!r}: ToolNodeDef.tool_schema is not a dataclass in "
            f"this calfkit version. Aliasing requires calfkit's "
            f"ToolNodeDef + ToolDefinition to remain @dataclass."
        ) from e

    return clone


def apply_deploy_filters(nodes: Sequence[ToolNodeDef]) -> dict[str, ToolNodeDef]:
    """Compose the tool registry from ``nodes``, applying INCLUDE/ALIAS.

    Returns a ``{tool_name: ToolNodeDef}`` dict. Each node registers under
    its ``tool_schema.name``; an alias whose ``src`` matches a node also
    registers a clone under its ``dst``. The ``CALFCORD_TOOLS_INCLUDE``
    allow-list, when set, drops any name not listed (applied *after* alias
    expansion so ``--rename a=b`` + ``--include b`` yields a true rename).

    Args:
        nodes: The full, explicit tool surface to expose. Order is
            preserved in the returned dict.

    Raises:
        ValueError: when two nodes advertise the same name, when an alias
            target collides with another registered name, or when an alias
            source names no node in ``nodes``.
    """
    include_set = _resolve_include_filter()
    alias_map = _resolve_alias_map()

    # Validate the whole config BEFORE building anything, so a misconfig
    # fails with one clear error rather than after a stream of misleading
    # "registered tool=..." success logs.
    original_names: set[str] = set()
    for node in nodes:
        name = node.tool_schema.name
        if name in original_names:
            raise ValueError(
                f"tool name {name!r} appears twice in the tool surface; "
                f"each tool must be unique"
            )
        original_names.add(name)
    unknown_sources = set(alias_map) - original_names
    if unknown_sources:
        raise ValueError(
            f"CALFCORD_TOOLS_ALIAS sources not found in the tool surface: "
            f"{sorted(unknown_sources)}; valid sources: {sorted(original_names)}"
        )

    if include_set is not None:
        logger.info(
            "CALFCORD_TOOLS_INCLUDE is set; only registering tools in: %s",
            sorted(include_set),
        )
    if alias_map:
        pairs = ", ".join(f"{src}→{dst}" for src, dst in sorted(alias_map.items()))
        logger.info("CALFCORD_TOOLS_ALIAS is set; cloning tools as: %s", pairs)

    registry: dict[str, ToolNodeDef] = {}
    for node in nodes:
        original = node.tool_schema.name
        entries: list[tuple[str, ToolNodeDef]] = [(original, node)]
        for src, dst in alias_map.items():
            if src == original:
                entries.append((dst, _clone_with_name(node, dst)))

        for name, resolved in entries:
            if include_set is not None and name not in include_set:
                logger.debug("skipping tool=%s (not in CALFCORD_TOOLS_INCLUDE)", name)
                continue
            if name in registry:
                raise ValueError(
                    f"tool name {name!r} is registered twice; an alias target "
                    f"collides with an existing tool name"
                )
            registry[name] = resolved
            logger.info("registered tool=%s", name)

    # The set of names an include filter *could* have matched: every node
    # name plus every alias target (the rename-via-alias case where the
    # original is filtered out but the dst is kept).
    matchable = original_names | set(alias_map.values())
    if include_set is not None:
        unknown = include_set - matchable
        if unknown:
            logger.warning(
                "CALFCORD_TOOLS_INCLUDE names not found in the tool surface: "
                "%s; available: %s",
                sorted(unknown),
                sorted(matchable),
            )

    return registry
