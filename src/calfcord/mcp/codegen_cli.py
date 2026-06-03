"""``calfcord-mcp-codegen`` — generate an MCP tool-schema module and drop it
into calfcord's discovery directory.

This is a thin *convention* wrapper over ``calfkit mcp codegen`` (the real
generator, which connects to a live MCP server, enumerates its tools, and
renders a Python module of :class:`~calfkit.mcp.McpToolDef` constants). The
raw calfkit command works, but it leaves the operator holding the three
footguns that make a generated schema silently unreachable:

* the output path is free-form, so a module written outside
  ``src/calfcord/mcp/schemas/`` is never seen by
  :func:`calfcord.mcp.discovery.discover_mcp_catalog`;
* the codegen *positional* (which only names the generated class) and the
  *filename* (the load-bearing catalog key) can diverge; and
* the server name is not checked against the selector grammar until catalog
  build, far from the typo that caused it.

This command closes all three by *owning* exactly two things and forwarding
everything else verbatim to calfkit:

* the **server name** (first positional) — validated against
  :func:`~calfcord.mcp.selector.is_valid_server_name` before anything spawns,
  and reused as both the calfkit positional and the filename so the two
  cannot diverge; and
* the **output path** — computed as ``<schemas-dir>/<server>.py`` and
  injected as ``-o``; a user-supplied ``-o/--output`` is rejected.

Every other flag (``--command``, ``--url``, ``--token``, ``--check``, and
any *future* calfkit codegen option) passes straight through, so this
wrapper does not have to change when calfkit's codegen surface grows. The
known value-taking flags are *declared* (not blindly forwarded) for one
concrete reason: argparse must know ``--command`` takes a value, or it could
mistake that value for the ``server`` positional when a flag precedes it.
Unknown/future flags ride through :meth:`~argparse.ArgumentParser.parse_known_args`'
extras list.

After a successful generation the command re-runs discovery against the
schemas package and reports whether the new server actually registered —
catching the empty-module and digit-leading-tool-name cases that would
otherwise surface only as an ``unknown server`` error at agent boot.

Assumes it is run from a source checkout (the normal editable install): the
schemas directory is located from the installed package, so generating
against a non-editable install would write into ``site-packages`` rather
than the repo. Codegen output is meant to be reviewed and committed, so run
this where ``src/calfcord/mcp/schemas/`` is the working tree.

Run::

    uv run calfcord-mcp-codegen gmail --command "npx -y @some-org/gmail-mcp-server"
    uv run calfcord-mcp-codegen drive --url https://mcp.example.com/drive --token "$TOK"
    uv run calfcord-mcp-codegen gmail --command "..." --check   # CI drift, no write
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from calfcord.mcp import discovery
from calfcord.mcp import schemas as _schemas_pkg
from calfcord.mcp.selector import is_valid_server_name

SCHEMAS_DIR = Path(_schemas_pkg.__file__).parent
"""Discovery directory: ``src/calfcord/mcp/schemas/`` in a source checkout.

Located from the installed ``calfcord.mcp.schemas`` package rather than a
hard-coded relative path, so the command writes to the right place
regardless of the caller's working directory. The committed schema modules
that :func:`calfcord.mcp.discovery.discover_mcp_catalog` walks live here, so
a module written anywhere else is invisible to the agent's catalog build."""


def _resolve_calfkit_executable() -> str:
    """Locate the ``calfkit`` console script for *this* environment.

    Prefers the script co-located with the running interpreter
    (``<venv>/bin/calfkit``) so the subprocess uses the same calfkit that
    calfcord is built against, rather than whatever ``PATH`` happens to
    surface first. Falls back to a ``PATH`` lookup, then fails with a
    remediation hint.

    Raises:
        SystemExit: if no ``calfkit`` executable can be found. The calfkit
            CLI ships with the ``mcp-codegen`` extra, already declared as
            ``calfkit[mcp-codegen]`` in ``pyproject.toml`` — ``uv sync``
            installs it.
    """
    colocated = Path(sys.executable).with_name("calfkit")
    if colocated.exists():
        return str(colocated)
    found = shutil.which("calfkit")
    if found is not None:
        return found
    raise SystemExit(
        "calfkit executable not found; the calfkit CLI ships with the "
        "'mcp-codegen' extra (declared as calfkit[mcp-codegen]). Run `uv sync`."
    )


def _parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    """Parse the wrapper's own args; return ``(namespace, forwarded_extras)``.

    Only the args this command *owns or must understand* are declared: the
    ``server`` positional, the value/flag options calfkit accepts today (so
    argparse parses them unambiguously regardless of order), and
    ``-o/--output`` (declared solely so it can be rejected — the output path
    is managed here). Everything else lands in ``extras`` and is forwarded to
    calfkit verbatim, so a future calfkit codegen flag needs no change here.

    Raises:
        SystemExit: (exit code 2, via :meth:`argparse.ArgumentParser.error`)
            if ``server`` violates the selector grammar or ``-o/--output``
            was supplied.
    """
    parser = argparse.ArgumentParser(
        prog="calfcord-mcp-codegen",
        description=(
            "Generate an MCP tool-schema module and place it in calfcord's "
            "discovery directory (src/calfcord/mcp/schemas/<server>.py). Thin "
            "wrapper over `calfkit mcp codegen`."
        ),
        epilog=(
            "Flags other than those listed are forwarded verbatim to `calfkit "
            "mcp codegen`, so new calfkit options work here unchanged. The "
            "server name and output path are managed by this command — pass the "
            "server name, never -o/--output."
        ),
    )
    parser.add_argument(
        "server",
        help=(
            "MCP server name: the schema filename (schemas/<server>.py), the "
            "catalog key, and the <server> segment of `mcp/<server>` selectors. "
            "Must match [a-z0-9_]{1,64}."
        ),
    )
    parser.add_argument(
        "--command",
        metavar="CMD",
        help="Shell command to spawn the MCP server over stdio, e.g. "
        "'npx -y @some-org/server'. Exclusive with --url (enforced by calfkit).",
    )
    parser.add_argument(
        "--url",
        help="Streamable HTTP URL of the MCP server. Exclusive with --command "
        "(enforced by calfkit).",
    )
    parser.add_argument(
        "--token",
        help="HTTP bearer token (only meaningful with --url).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI drift mode: exit non-zero if the on-disk module would differ; "
        "writes nothing. Forwarded to calfkit.",
    )
    # Declared ONLY to reject it: the output path is this command's reason to
    # exist. Hidden from --help (operators should never set it), and argparse
    # normalizes every spelling (-o X / --output X / --output=X) so the
    # rejection below catches them all.
    parser.add_argument("-o", "--output", help=argparse.SUPPRESS)

    args, extras = parser.parse_known_args(argv)

    if not is_valid_server_name(args.server):
        parser.error(
            f"invalid server name {args.server!r}; must match the MCP selector "
            "server grammar [a-z0-9_]{1,64} (lowercase letters, digits, "
            "underscore). It is the schema filename, the catalog key, and the "
            "`mcp/<server>` selector segment."
        )
    if args.output is not None:
        parser.error(
            "-o/--output is managed by calfcord-mcp-codegen; the module is "
            f"always written to schemas/{args.server}.py. Drop the flag."
        )
    return args, extras


def _build_calfkit_command(
    calfkit: str,
    args: argparse.Namespace,
    extras: list[str],
    out: Path,
) -> list[str]:
    """Assemble the ``calfkit mcp codegen`` argv.

    The ``server`` positional is passed as calfkit's positional (so the
    generated class name matches the filename), the known flags are
    re-emitted, any forwarded ``extras`` ride along, and the managed ``-o``
    output path is appended last.
    """
    cmd = [calfkit, "mcp", "codegen", args.server]
    if args.command is not None:
        cmd += ["--command", args.command]
    if args.url is not None:
        cmd += ["--url", args.url]
    if args.token is not None:
        cmd += ["--token", args.token]
    if args.check:
        cmd.append("--check")
    cmd += extras
    cmd += ["-o", str(out)]
    return cmd


def _verify_in_catalog(server: str, out: Path) -> None:
    """Re-run discovery and report whether ``server`` actually registered.

    A best-effort confirmation that what calfkit just wrote is reachable by
    the agent's catalog build. Walks the schemas package fresh (rather than
    the import-time-cached ``MCP_CATALOG``, which was built before this file
    existed). A missing server after a successful write means an empty/stale
    module or a digit-leading tool name (whose generated constant is
    underscore-prefixed and skipped by discovery) — surfaced here, loudly, at
    generation time instead of as ``unknown server`` at agent boot.
    """
    catalog = discovery.discover_mcp_catalog(_schemas_pkg)
    tools = catalog.get(server)
    if tools:
        names = [t.name for t in tools]
        print(f"verified: server {server!r} registered with {len(names)} tool(s): {names}")
        return
    print(
        f"WARNING: wrote {out} but server {server!r} is NOT in the discovered "
        "catalog. Likely an empty/stale module, or every tool name starts with "
        "a digit (its constant is underscore-prefixed and skipped). Agents will "
        f"fail `mcp/{server}` selectors with 'unknown server' until fixed.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    """Validate, delegate to ``calfkit mcp codegen``, then verify.

    Exits with calfkit's own return code (0 success / no drift, 1 drift in
    ``--check`` mode, 2 error), so this wrapper is drop-in for CI use.
    """
    args, extras = _parse_args(argv)
    out = SCHEMAS_DIR / f"{args.server}.py"
    calfkit = _resolve_calfkit_executable()
    cmd = _build_calfkit_command(calfkit, args, extras, out)

    result = subprocess.run(cmd)  # inherits stdio so calfkit's output stays live

    if result.returncode == 0 and out.exists():
        try:
            _verify_in_catalog(args.server, out)
        except Exception as exc:  # best-effort: the write already succeeded
            print(
                f"WARNING: wrote {out} but post-write catalog verification failed: {exc}",
                file=sys.stderr,
            )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
