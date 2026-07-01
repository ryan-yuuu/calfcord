"""``mcp.json`` mutation — the writer behind ``disco mcp add`` / ``remove``.

Same validate-before-write discipline as the agent ``.md`` writer
(:mod:`calfcord.agents.md_writer`): the new entry is shape-checked with the
*loader's own* validator before anything touches disk, so the writer can
never produce a file the loader (and therefore every server boot) would
reject. ``$VAR`` references are deliberately NOT expanded here — the writer
must work on hosts where the secrets are unset, and the file should carry
the reference, not the secret.

Writes are atomic (tmp + ``os.replace``) and keep the file at mode 0600
(entries may carry literal credentials). Unrelated top-level keys (e.g. a
``$schema`` line) and sibling servers ride through a mutation verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from calfcord._atomic import atomic_write_text
from calfcord.mcp.config import _SERVERS_KEY, McpConfigError, validate_entry_shape
from calfcord.mcp.selector import is_valid_server_name


def add_server(path: Path, name: str, entry: dict[str, Any], *, force: bool = False) -> None:
    """Add (or with ``force``, replace) server ``name``'s entry in ``path``.

    Creates the file with the ``mcpServers`` wrapper when missing. Raises
    :class:`McpConfigError` — with the file untouched — for an invalid
    name/entry, a corrupt file, or an existing name without ``force``.
    """
    if not is_valid_server_name(name):
        raise McpConfigError(
            f"invalid MCP server name {name!r}: must match [a-z0-9_]{{1,64}}"
        )
    validate_entry_shape(name, entry, path)

    raw = _read_raw(path)
    servers = raw[_SERVERS_KEY]
    if name in servers and not force:
        raise McpConfigError(
            f"MCP server {name!r} already exists in {path}; re-run with --force to replace it"
        )
    servers[name] = entry
    _atomic_write(path, raw)


def remove_server(path: Path, name: str) -> None:
    """Delete server ``name``'s entry from ``path``.

    Raises :class:`McpConfigError` when the file is missing/corrupt or the
    name is not configured (listing what is, so a typo is a one-look fix).
    """
    if not path.exists():
        raise McpConfigError(f"MCP config not found at {path}")
    raw = _read_raw(path)
    servers = raw[_SERVERS_KEY]
    if name not in servers:
        configured = ", ".join(servers) or "(none)"
        raise McpConfigError(
            f"no MCP server named {name!r} in {path}; configured: {configured}"
        )
    del servers[name]
    _atomic_write(path, raw)


def _read_raw(path: Path) -> dict[str, Any]:
    """The full JSON document with an ``mcpServers`` dict guaranteed present.

    A missing file yields a fresh skeleton; an existing file must already be
    a JSON object (the wrapper is added if absent ONLY when the document is
    empty — any other shape is the loader's error to report, not ours to
    silently rewrite).
    """
    if not path.exists():
        return {_SERVERS_KEY: {}}
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise McpConfigError(f"cannot read MCP config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"MCP config {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or (raw and _SERVERS_KEY not in raw):
        raise McpConfigError(
            f"MCP config {path} must be a JSON object with an {_SERVERS_KEY!r} key "
            '(e.g. {"mcpServers": {}})'
        )
    raw.setdefault(_SERVERS_KEY, {})
    if not isinstance(raw[_SERVERS_KEY], dict):
        raise McpConfigError(f"{_SERVERS_KEY!r} in {path} must be an object of server entries")
    return raw


def _atomic_write(path: Path, raw: dict[str, Any]) -> None:
    """Serialize ``raw`` to ``path`` atomically at mode 0600.

    Delegates to the package's shared writer — the same one the sibling
    ``config/`` secret files (``.env`` upserts, the setup checkpoint) use.
    """
    atomic_write_text(path, json.dumps(raw, indent=2) + "\n", mode=0o600)
