"""Load the deployment's MCP server registry from an ``mcp.json`` file.

Replaces the former hand-authored ``MCP_SERVERS`` dict. Transport and ``$VAR``
credential references live in ``mcp.json`` (operator config, resolved from the
environment at *parse* time); the tool *schemas* continue to come from the
committed, codegen-generated :data:`~calfcord.mcp.catalog.MCP_CATALOG`.
calfkit's :class:`~calfkit.mcp.McpServers` marries the two.

This module is read only by a *deployment* process when it calls
:func:`load_mcp_servers` — today the ``calfkit-mcp`` bridge runner (the unified
tools-host will reuse it). The agent path never calls it, so no MCP credential
is ever required to import or run an agent — the boundary the deleted
``servers.py`` documented (and ``test_import_isolation.py`` asserts) is now
enforced simply by not calling this loader.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from calfkit.mcp import McpServers, McpToolDef

from calfcord.mcp.catalog import MCP_CATALOG

_CONFIG_PATH_ENV = "CALFCORD_MCP_CONFIG"
_DEFAULT_CONFIG_PATH = "mcp.json"


def resolve_config_path() -> Path:
    """Return the mcp.json path: ``$CALFCORD_MCP_CONFIG`` or ``./mcp.json``."""
    return Path(os.getenv(_CONFIG_PATH_ENV) or _DEFAULT_CONFIG_PATH)


def load_mcp_servers(
    path: str | Path,
    catalog: Mapping[str, list[McpToolDef]] | None = None,
) -> McpServers:
    """Build the live :class:`~calfkit.mcp.McpServers` from ``path``.

    Transport + ``$VAR``-expanded credentials come from the ``mcp.json`` at
    ``path``; tool schemas come from ``catalog`` (default
    :data:`~calfcord.mcp.catalog.MCP_CATALOG`). calfkit parses the file once,
    expands ``$VAR`` references against the environment, builds each server
    with ``name=<config key>``, and validates that every configured server has
    a matching committed schema.

    ``catalog`` is injectable so tests can supply a fake; production always
    uses the default.

    Raises:
        calfkit.mcp.exceptions.McpConfigError: a missing file, malformed JSON,
            an unset ``$VAR`` referenced in the config, an unknown transport,
            or a configured server with no committed schema in ``catalog``. The
            runner converts this to a clean ``SystemExit`` at process top level.
    """
    catalog = MCP_CATALOG if catalog is None else catalog
    return McpServers.from_file(path, schemas=dict(catalog))
