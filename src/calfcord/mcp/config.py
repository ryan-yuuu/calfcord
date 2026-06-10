"""``mcp.json`` loading — the server-path half of calfcord's MCP boundary.

``mcp.json`` declares the MCP servers calfcord may host, using the same
``{"mcpServers": {...}}`` shape Cursor and Claude Code use so operators can
paste entries straight from those tools' docs:

* stdio: ``{"command": ..., "args": [...], "env": {...}, "cwd": ...}``
  (optionally an explicit ``"type": "stdio"``);
* HTTP (Streamable HTTP): ``{"type": "http", "url": ..., "headers": {...}}``.

Values may reference environment variables as ``$VAR`` / ``${VAR}``
(``$$`` escapes a literal dollar); references are expanded at load time and
an unset reference fails the load naming the variable. Literal values are
also legal — the file lives next to ``config/.env`` with the same 0600
expectations — but ``$VAR`` keeps secrets out of the file and is what the
``calfcord mcp add`` wizard nudges toward.

Deployment boundary: only the ``calfkit-mcp`` runner and the
``calfcord mcp`` CLI read this module. Agents resolve MCP tools from the
broker's capability view (:mod:`calfcord.mcp.agent_select`) and never need
this file on their host — pinned by ``tests/mcp/test_import_isolation.py``.

Two reader depths, matching who needs secrets:

* :func:`list_server_names` — names only, **no expansion**: roster/compose
  generation and CLI pick-lists run on hosts where the referenced secrets
  may be unset, and a missing file just means "no MCP servers".
* :func:`load_mcp_servers` — full parse + expansion into ready
  :class:`~calfkit.mcp.mcp_toolbox.MCPToolbox` defs; only the runner (and
  the wizard's optional start step) call this.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from calfkit.mcp.mcp_toolbox import MCPToolbox
from calfkit.mcp.mcp_transport import StdioServerParameters, StreamableHttpParameters

from calfcord.mcp.selector import is_valid_server_name

CONFIG_ENV_VAR = "CALFCORD_MCP_CONFIG"
"""Environment override for the ``mcp.json`` path."""

_SERVERS_KEY = "mcpServers"

# Matches the ``$$`` escape FIRST (it collapses to a literal ``$``, never a
# reference), then balanced ``${VAR}``, then bare ``$VAR``. An unbalanced
# ``${VAR`` matches nothing — it would ship as a literal — so the expander
# rejects it explicitly rather than passing a half-reference to a server.
_VAR_PATTERN = re.compile(r"\$\$|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
_UNBALANCED_BRACE = re.compile(r"\$\{(?![A-Za-z_][A-Za-z0-9_]*\})")

# Key sets are closed so a typo ("evn") fails loud instead of silently
# spawning a server without its credentials.
_STDIO_KEYS = frozenset({"type", "command", "args", "env", "cwd"})
_HTTP_KEYS = frozenset({"type", "url", "headers"})


class McpConfigError(Exception):
    """A problem with ``mcp.json``: unreadable, malformed, or invalid.

    Every message names the offending file/server/key/variable so the fix
    is a config edit, never a stack-trace dig.
    """


def resolve_config_path() -> Path:
    """The ``mcp.json`` path for this process.

    ``$CALFCORD_MCP_CONFIG`` wins (explicit operator intent), then the
    installed location ``$CALFCORD_HOME/config/mcp.json`` (next to
    ``config/.env``), then ``./mcp.json`` for repo-checkout dev runs —
    the same native-vs-dev split ``cli/init.py::resolve_paths`` uses.
    """
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    home = os.environ.get("CALFCORD_HOME")
    if home:
        return Path(home) / "config" / "mcp.json"
    return Path("mcp.json")


def expand_vars(value: str, env: Mapping[str, str]) -> str:
    """Expand ``$VAR`` / ``${VAR}`` references in ``value`` against ``env``.

    ``$$`` collapses to a literal ``$``. Raises :class:`McpConfigError` for
    an unset reference (naming the variable) or an unbalanced ``${``.
    """
    if _UNBALANCED_BRACE.search(value):
        raise McpConfigError(
            f"unbalanced '${{' in {value!r}: use ${{VAR}} with a closing brace, or $$ for a literal $"
        )

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "$$":
            return "$"
        name = token[2:-1] if token.startswith("${") else token[1:]
        if name not in env:
            raise McpConfigError(
                f"environment variable {name!r} (referenced as {token!r}) is not set"
            )
        return env[name]

    return _VAR_PATTERN.sub(_replace, value)


def list_server_names(path: Path) -> list[str]:
    """Configured server names in declaration order, without expanding values.

    A missing file means "no MCP servers" (the installer seeds an empty
    registry, but enumeration must not explode before first seed). Shape
    problems still raise: a config that *exists* but is invalid should fail
    everywhere, not just at server start.
    """
    if not path.exists():
        return []
    return [name for name, _ in _validated_entries(path)]


def load_mcp_servers(path: Path) -> dict[str, MCPToolbox]:
    """Parse ``mcp.json`` into one ready :class:`MCPToolbox` per server.

    Expands ``$VAR`` references (this is the secrets-touching step), builds
    the transport params, and returns ``{name: toolbox}`` in declaration
    order. Raises :class:`McpConfigError` on any file/shape/expansion
    problem, including a missing file — the runner wants "you asked to host
    servers but there's no config" loud, unlike enumeration.
    """
    if not path.exists():
        raise McpConfigError(
            f"MCP config not found at {path} — create it (or run 'calfcord mcp add') first"
        )
    servers: dict[str, MCPToolbox] = {}
    for name, entry in _validated_entries(path):
        servers[name] = MCPToolbox(name, connection_params=_build_params(name, entry))
    return servers


def _validated_entries(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Read + shape-validate ``mcp.json`` without expanding any values."""
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise McpConfigError(f"cannot read MCP config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"MCP config {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict) or _SERVERS_KEY not in raw:
        raise McpConfigError(
            f"MCP config {path} must be a JSON object with an {_SERVERS_KEY!r} key "
            '(e.g. {"mcpServers": {}})'
        )
    server_map = raw[_SERVERS_KEY]
    if not isinstance(server_map, dict):
        raise McpConfigError(f"{_SERVERS_KEY!r} in {path} must be an object of server entries")

    entries: list[tuple[str, dict[str, Any]]] = []
    for name, entry in server_map.items():
        if not is_valid_server_name(name):
            raise McpConfigError(
                f"invalid MCP server name {name!r} in {path}: must match [a-z0-9_]{{1,64}} "
                "(it doubles as a Kafka topic segment and roster process name)"
            )
        if not isinstance(entry, dict):
            raise McpConfigError(f"server {name!r} in {path} must be an object")
        _validate_entry_shape(name, entry, path)
        entries.append((name, entry))
    return entries


def _validate_entry_shape(name: str, entry: dict[str, Any], path: Path) -> None:
    """Reject malformed entries with the server + key named."""
    entry_type = entry.get("type")
    has_command = "command" in entry
    has_url = "url" in entry

    if entry_type not in (None, "stdio", "http"):
        hint = " (Streamable HTTP covers SSE — use \"type\": \"http\")" if entry_type == "sse" else ""
        raise McpConfigError(
            f"server {name!r}: unsupported type {entry_type!r}; expected \"stdio\" or \"http\"{hint}"
        )
    if has_command and (has_url or entry_type == "http"):
        raise McpConfigError(
            f"server {name!r}: declares both stdio ('command') and HTTP ('url'/'type: http') — pick one"
        )
    if has_url and entry_type != "http":
        raise McpConfigError(
            f"server {name!r}: a 'url' entry must declare \"type\": \"http\" explicitly"
        )

    if has_command:
        allowed, kind = _STDIO_KEYS, "stdio"
    elif entry_type == "http":
        if not has_url:
            raise McpConfigError(f"server {name!r}: \"type\": \"http\" requires a 'url'")
        allowed, kind = _HTTP_KEYS, "http"
    else:
        raise McpConfigError(
            f"server {name!r}: must have either a 'command' (stdio) or \"type\": \"http\" with a 'url'"
        )

    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise McpConfigError(
            f"server {name!r}: unknown key(s) {unknown} for a {kind} entry; allowed: {sorted(allowed)}"
        )

    _require_str(name, entry, "command")
    _require_str(name, entry, "url")
    _require_str(name, entry, "cwd")
    if "args" in entry and not (
        isinstance(entry["args"], list) and all(isinstance(a, str) for a in entry["args"])
    ):
        raise McpConfigError(f"server {name!r}: 'args' must be a list of strings")
    for map_key in ("env", "headers"):
        value = entry.get(map_key)
        if value is None:
            continue
        if not (
            isinstance(value, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        ):
            raise McpConfigError(
                f"server {name!r}: '{map_key}' must be an object of string values"
            )


def _require_str(name: str, entry: dict[str, Any], key: str) -> None:
    if key in entry and not (isinstance(entry[key], str) and entry[key]):
        raise McpConfigError(f"server {name!r}: '{key}' must be a non-empty string")


def _build_params(
    name: str, entry: dict[str, Any]
) -> StdioServerParameters | StreamableHttpParameters:
    """Expand ``$VAR`` references and build the calfkit transport params."""
    env = os.environ

    def expand(value: str) -> str:
        try:
            return expand_vars(value, env)
        except McpConfigError as exc:
            raise McpConfigError(f"server {name!r}: {exc}") from exc

    if "command" in entry:
        return StdioServerParameters(
            command=expand(entry["command"]),
            args=[expand(a) for a in entry.get("args", [])],
            env={k: expand(v) for k, v in entry["env"].items()} if "env" in entry else None,
            cwd=expand(entry["cwd"]) if "cwd" in entry else None,
        )
    return StreamableHttpParameters(
        url=expand(entry["url"]),
        headers={k: expand(v) for k, v in entry["headers"].items()}
        if "headers" in entry
        else None,
    )
