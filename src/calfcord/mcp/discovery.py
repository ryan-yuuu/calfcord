"""Auto-discovery of MCP tool schemas by walking the ``mcp/schemas`` package.

Background
----------

Each MCP server we integrate gets one committed module under
``calfcord/mcp/schemas/<server>.py``, produced by::

    calfkit mcp codegen <server> -o src/calfcord/mcp/schemas/<server>.py

That generated module declares one top-level
``NAME = McpToolDef(...)`` constant per tool the server publishes (plus a
class wrapper for ergonomics, which we ignore). The *module name* is the
*server name*: ``schemas/gmail.py`` describes the ``gmail`` server.

This module mirrors the drop-in discovery used for builtin tools in
:mod:`calfcord.tools.discovery`: drop a generated ``<server>.py`` into
``schemas/`` and it is picked up at catalog-build time, keyed by server
name, with no edit to any registry module. The result is a
``{server: [McpToolDef, ...]}`` catalog consumed by both the agent path
(:mod:`calfcord.mcp.schema_build`, schema-only) and the bridge path
(:mod:`calfcord.mcp.servers`, real transport).

Design choices
--------------

* **One module per server, keyed by module name** — unlike the builtin
  tool registry (keyed by ``tool_schema.name``), MCP tools are grouped by
  *server*, and a server's name is not carried on the individual
  :class:`~calfkit.mcp.McpToolDef` (which knows only its own tool name).
  The module name is therefore the only reliable server identity, so we
  key the catalog by it. This also matches the ``mcp/<server>`` selector
  grammar, where ``<server>`` must equal a ``schemas/`` module name.

* **Collect top-level instances, not an ``.ALL`` attribute** — the
  generated module emits both top-level ``McpToolDef`` constants and a
  class wrapper that may aggregate them. We collect the *instances*
  directly (every top-level attribute that *is* an ``McpToolDef``) rather
  than reading any ``.ALL`` convenience attribute, so the discovery does
  not depend on a codegen aggregation contract that could change shape.

* **Underscore-prefixed modules AND attributes are skipped** — same
  pytest-style convention as :mod:`calfcord.tools.discovery`: a
  ``_helpers.py`` support module is not scanned, and a ``_draft`` constant
  inside a scanned module is treated as a construction artifact, not a
  registered tool.

* **Instances are deduped by :func:`id`** — a re-exported constant
  (``from .gmail import GMAIL_SEARCH``) would otherwise be collected twice
  for the same server. We compare by object identity so a genuine
  re-export is collapsed to one entry while two *distinct* defs that
  happen to share a tool name remain visible (the selector resolver, not
  this walk, owns same-name conflict semantics).

* **Modules and attributes are sorted** — :func:`pkgutil.iter_modules`
  and :func:`dir` are not order-stable across interpreters/filesystems.
  Sorting makes the catalog (and the boot log) reproducible across
  deployments.

* **Zero-def modules warn (not raise)** — a ``schemas/<server>.py`` that
  yields no ``McpToolDef`` is almost certainly a stale or mis-generated
  file, but it is not fatal: the catalog simply omits that server, and a
  later ``mcp/<server>`` selector fails with a precise "unknown server"
  error in :mod:`calfcord.mcp.schema_build`. We surface the empty module
  at WARNING level so an operator sees the cause without grepping.

* **:class:`ImportError` is allowed to propagate** — a broken generated
  module is an operator-visible config bug, not a condition to swallow
  (same rationale as the builtin-tool walk).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType

from calfkit.mcp import McpToolDef

from calfcord.mcp.selector import is_valid_server_name

logger = logging.getLogger(__name__)


def discover_mcp_catalog(package: ModuleType) -> dict[str, list[McpToolDef]]:
    """Walk ``package`` and build a ``{server: [McpToolDef, ...]}`` catalog.

    Iterates the immediate submodules of ``package`` (expected to be
    :mod:`calfcord.mcp.schemas`) in alphabetical order, imports each one,
    and collects every top-level attribute that is an
    :class:`~calfkit.mcp.McpToolDef` instance. Each module's defs are keyed
    in the result under the *module name*, which is the MCP server name.

    Args:
        package: The package to walk. Must be an actual package (have
            ``__path__``); plain modules are unsupported because
            :func:`pkgutil.iter_modules` requires a search path.

    Returns:
        A dict mapping server name to the list of that server's
        :class:`~calfkit.mcp.McpToolDef` instances, in deterministic
        (alphabetical-by-attribute-name) order. Servers whose module
        exposed no defs are omitted (and warned about).

    Raises:
        ValueError: When a (non-underscore) submodule's name is not a legal
            ``[a-z0-9_]{1,64}`` server segment (e.g. an uppercase
            ``Gmail.py``) — such a name would key the catalog under a server
            no ``mcp/...`` selector could reach, so discovery fails loudly
            instead of registering an unreachable server.
        ImportError: Propagated unchanged from
            :func:`importlib.import_module` when a generated submodule
            fails to import — treated as a hard config error.
    """
    catalog: dict[str, list[McpToolDef]] = {}

    modules = sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name)
    for mod_info in modules:
        # Underscore prefix marks "private support module, not a server
        # schema" — same convention pytest uses for conftest/_*.py and the
        # builtin-tool walk uses for shared helpers.
        if mod_info.name.startswith("_"):
            continue

        # The module name becomes the catalog key, i.e. the MCP server name,
        # and every ``mcp/<server>`` selector segment must match the
        # ``[a-z0-9_]{1,64}`` server grammar. A module whose name violates
        # that grammar (e.g. an uppercase ``Gmail.py``) would otherwise key
        # the catalog under ``Gmail`` — a name no valid selector can ever
        # reach, leaving its tools silently unreachable. Reject it loudly so
        # the misnamed file surfaces at boot rather than as a baffling
        # "unknown server" later.
        if not is_valid_server_name(mod_info.name):
            raise ValueError(
                f"MCP schema module {package.__name__}.{mod_info.name!r} has an "
                f"invalid server name {mod_info.name!r}; it must match the "
                f"selector server grammar [a-z0-9_]{{1,64}}. Rename the schema "
                f"module to lowercase [a-z0-9_]; the module name is the server "
                f"name and every selector segment must match."
            )

        server_name = mod_info.name
        full_name = f"{package.__name__}.{mod_info.name}"
        module = importlib.import_module(full_name)

        seen: set[int] = set()
        tools: list[McpToolDef] = []
        # Sort attribute names so collection order is deterministic
        # regardless of how Python lays out the module dict.
        for attr_name in sorted(dir(module)):
            # Underscore prefix on the ATTRIBUTE means "construction
            # artifact, not a published tool" — symmetric with the
            # module-level rule.
            if attr_name.startswith("_"):
                continue
            value = getattr(module, attr_name)
            if not isinstance(value, McpToolDef):
                continue
            # Same instance re-exported within the module — collect once.
            if id(value) in seen:
                continue
            seen.add(id(value))
            tools.append(value)

        if not tools:
            # A generated server module with no McpToolDef is almost
            # certainly stale or mis-generated. Non-fatal (the server is
            # simply absent from the catalog; a later selector referencing
            # it fails with a precise error), but worth surfacing loudly.
            logger.warning(
                "MCP schema module %s exposed no McpToolDef instances; "
                "skipping server %r (did `calfkit mcp codegen` succeed?)",
                full_name,
                server_name,
            )
            continue

        catalog[server_name] = tools
        logger.info(
            "discovered MCP server=%s tools=%s from=%s",
            server_name,
            [t.name for t in tools],
            full_name,
        )

    return catalog
