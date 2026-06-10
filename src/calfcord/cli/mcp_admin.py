"""``calfcord mcp add|list|remove`` — manage the ``mcp.json`` server registry.

``add`` is dual-mode, picked by the flags:

* **Wizard** (no ``--command``/``--url``): an InquirerPy flow over the
  injected :class:`Prompter` seam — name, transport, command/URL,
  env/header loop, a JSON preview confirm, then an optional start. This is
  the onboarding-grade path (``calfcord mcp add`` and you're done).
* **Flags** (``--command``/``--url`` given): non-interactive, scripting
  parity with the old ``calfcord-mcp-add`` (``--env NAME`` is shorthand for
  ``NAME=$NAME``).

Both modes funnel into :func:`calfcord.mcp.config_write.add_server`, the one
validated writer, so a wizard answer and a flag can never diverge in what
they persist. Literal secret values are allowed (the file is 0600 and the
schema matches Cursor/Claude Code, whose configs users paste from) but any
value without a ``$VAR`` reference earns a one-line nudge toward one.

Like the rest of the CLI, operator-recoverable problems print an actionable
message and return non-zero — never a traceback.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from calfcord.cli._prompts import Choice, Prompter
from calfcord.mcp.config import McpConfigError, references_var
from calfcord.mcp.config_write import add_server, remove_server
from calfcord.mcp.selector import is_valid_server_name

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _nudge_literals(pairs: dict[str, str], *, kind: str) -> None:
    literals = sorted(k for k, v in pairs.items() if not references_var(v))
    if literals:
        print(
            f"note: {kind} value(s) {literals} are literals; consider a $VAR "
            "reference (set the secret in config/.env) so mcp.json never holds it."
        )


def _parse_env_token(token: str) -> tuple[str, str]:
    """``NAME=VALUE`` → pair; bare ``NAME`` → ``(NAME, "$NAME")`` passthrough."""
    if "=" in token:
        key, value = token.split("=", 1)
        return key, value
    if not _ENV_NAME.match(token):
        raise McpConfigError(
            f"invalid --env shorthand {token!r}: a bare name must be a valid "
            "environment variable name (it expands to NAME=$NAME)"
        )
    return token, f"${token}"


def _parse_header_token(token: str) -> tuple[str, str]:
    if "=" not in token:
        raise McpConfigError(f"invalid --header {token!r}: expected KEY=VALUE")
    key, value = token.split("=", 1)
    return key, value


def _pairs(
    tokens: list[str], parse: Callable[[str], tuple[str, str]], *, kind: str
) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in tokens:
        key, value = parse(token)
        if key in out:
            raise McpConfigError(f"duplicate {kind} key {key!r}")
        out[key] = value
    return out


def _stdio_entry(command_line: str, env: dict[str, str], cwd: str | None) -> dict[str, Any]:
    parts = shlex.split(command_line)
    if not parts:
        raise McpConfigError("command must not be empty")
    entry: dict[str, Any] = {"command": parts[0]}
    if parts[1:]:
        entry["args"] = parts[1:]
    if env:
        entry["env"] = env
    if cwd:
        entry["cwd"] = cwd
    return entry


def _http_entry(url: str, headers: dict[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "http", "url": url}
    if headers:
        entry["headers"] = headers
    return entry


def _preview(name: str, entry: dict[str, Any]) -> str:
    return json.dumps({"mcpServers": {name: entry}}, indent=2)


def run_add(
    prompter: Prompter,
    *,
    config_path: Path,
    server: str | None,
    command: str | None,
    env: list[str],
    url: str | None,
    header: list[str],
    cwd: str | None,
    force: bool,
    dry_run: bool,
    start: bool,
    home: Path | None,
) -> int:
    """Add one server to ``mcp.json`` (wizard or flags) and optionally start it."""
    try:
        if command is not None or url is not None:
            name, entry = _add_from_flags(
                server=server, command=command, env=env, url=url, header=header, cwd=cwd
            )
        else:
            wizard = _add_from_wizard(prompter, server=server)
            if wizard is None:
                return 1  # operator declined the preview — nothing written
            name, entry = wizard

        # Nudge before any early return: the dry-run preview is exactly the
        # moment an operator is inspecting what they're about to commit.
        _nudge_literals(entry.get("env", {}), kind="env")
        _nudge_literals(entry.get("headers", {}), kind="header")

        if dry_run:
            print(_preview(name, entry))
            print("(dry run: nothing written)")
            return 0

        add_server(config_path, name, entry, force=force)
    except McpConfigError as exc:
        print(f"error: {exc}")
        return 1

    print(f"added MCP server {name!r} to {config_path}")

    wants_start = start or (
        command is None and url is None and prompter.confirm(f"Start {name} now?", default=True)
    )
    if not wants_start:
        print(
            f"next: `calfcord mcp start {name}` (a server new to this workspace "
            "needs `calfcord stop && calfcord start` first to declare its slot)"
        )
        return 0
    if home is None:
        print(
            "cannot start it from here (no CALFCORD_HOME — dev run); "
            f"run `calfcord mcp start {name}` from the install."
        )
        return 0

    from calfcord.supervisor import mcp_roster

    return asyncio.run(mcp_roster.mcp_start(home, server=name))


def _add_from_flags(
    *,
    server: str | None,
    command: str | None,
    env: list[str],
    url: str | None,
    header: list[str],
    cwd: str | None,
) -> tuple[str, dict[str, Any]]:
    """Build the entry from the non-interactive flags (cross-validated)."""
    if server is None:
        raise McpConfigError("give the server name (calfcord mcp add <server> --command ...)")
    if command is not None and url is not None:
        raise McpConfigError("--command and --url are mutually exclusive")
    if env and url is not None:
        raise McpConfigError("--env is for stdio servers; use --header with --url")
    if header and command is not None:
        raise McpConfigError("--header is for HTTP servers; use --env with --command")
    if cwd and command is None:
        raise McpConfigError("--cwd is for stdio servers")

    if command is not None:
        return server, _stdio_entry(command, _pairs(env, _parse_env_token, kind="env"), cwd)
    # The caller dispatches here only when command or url is given, and the
    # exclusivity check above rejected "both" — so url is set on this branch.
    if url is None:
        raise McpConfigError("give --command (stdio) or --url (HTTP)")
    return server, _http_entry(url, _pairs(header, _parse_header_token, kind="header"))


def _add_from_wizard(
    prompter: Prompter, *, server: str | None
) -> tuple[str, dict[str, Any]] | None:
    """The interactive flow; ``None`` when the operator declines the preview."""
    name = server
    while name is None or not is_valid_server_name(name):
        if name is not None:
            print(f"error: invalid server name {name!r}; use [a-z0-9_]{{1,64}}")
        name = prompter.text("Server name (lowercase, digits, underscore)").strip()

    transport = prompter.select(
        "Transport",
        [
            Choice("stdio", "stdio — a local command this host launches (npx/uvx/binary)"),
            Choice("http", "HTTP — a running Streamable-HTTP MCP endpoint"),
        ],
    )

    if transport == "stdio":
        command_line = ""
        while not command_line.strip():
            command_line = prompter.text(
                "Command (e.g. npx -y @modelcontextprotocol/server-github)"
            )
        env = _collect_pairs(
            prompter,
            "Environment variable (NAME=VALUE, or NAME to pass $NAME through; empty to finish)",
            _parse_env_token,
        )
        entry = _stdio_entry(command_line, env, None)
    else:
        endpoint = ""
        while not endpoint.strip():
            endpoint = prompter.text("Server URL (https://.../mcp)")
        headers = _collect_pairs(
            prompter,
            "Header (KEY=VALUE, e.g. Authorization=Bearer $TOKEN; empty to finish)",
            _parse_header_token,
        )
        entry = _http_entry(endpoint.strip(), headers)

    print(_preview(name, entry))
    if not prompter.confirm(f"Write {name} to mcp.json?", default=True):
        print("nothing written")
        return None
    return name, entry


def _collect_pairs(
    prompter: Prompter, message: str, parse: Callable[[str], tuple[str, str]]
) -> dict[str, str]:
    """Prompt-loop key=value pairs until an empty line; bad input re-prompts."""
    pairs: dict[str, str] = {}
    while True:
        token = prompter.text(message).strip()
        if not token:
            return pairs
        try:
            key, value = parse(token)
        except McpConfigError as exc:
            print(f"error: {exc}")
            continue
        if key in pairs:
            print(f"error: duplicate key {key!r}")
            continue
        pairs[key] = value


def run_list(*, config_path: Path, home: Path | None) -> int:
    """Print the configured servers (name, transport summary, running state)."""
    from calfcord.mcp.config import list_server_entries

    try:
        entries = list_server_entries(config_path)
    except McpConfigError as exc:
        print(f"error: {exc}")
        return 1
    if not entries:
        print(f"no MCP servers configured in {config_path}; add one with `calfcord mcp add`")
        return 0

    running = _running_servers(home)
    for name, entry in entries:
        summary = (
            " ".join([entry["command"], *entry.get("args", [])])
            if "command" in entry
            else entry.get("url", "")
        )
        state = ""
        if running is not None:
            state = "  [running]" if name in running else "  [stopped]"
        print(f"{name:<20} {summary}{state}")
    return 0


def _running_servers(home: Path | None) -> set[str] | None:
    """Names of Running MCP servers, or ``None`` when state is unknowable
    (dev run, workspace closed, or the supervisor dropped mid-read) — list
    still works, just stateless."""
    if home is None:
        return None
    from calfcord.supervisor import mcp_roster
    from calfcord.supervisor._workspace import resolve_client, workspace_is_up

    async def _read() -> set[str] | None:
        client = resolve_client(None, str(home))
        if not await workspace_is_up(client):
            return None
        try:
            return await mcp_roster.running_servers(client)
        except RuntimeError:
            # The workspace probe and this read race a dying supervisor; a
            # drop in the window degrades to stateless, same as "closed".
            return None

    return asyncio.run(_read())


def run_remove(
    prompter: Prompter,
    *,
    config_path: Path,
    server: str,
    force: bool,
    home: Path | None,
) -> int:
    """Delete ``server`` from mcp.json (confirm unless ``force``)."""
    if not force and not prompter.confirm(
        f"Remove MCP server {server!r} from {config_path}?", default=False
    ):
        print("nothing removed")
        return 1
    try:
        remove_server(config_path, server)
    except McpConfigError as exc:
        print(f"error: {exc}")
        return 1
    print(
        f"removed MCP server {server!r}. If it is running, `calfcord mcp stop {server}` "
        "stops it; the slot disappears on the next workspace reload."
    )
    return 0
