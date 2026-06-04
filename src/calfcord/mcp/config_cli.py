"""``calfcord-mcp-add`` — add (or update) one MCP server's entry in ``mcp.json``.

The bridge-side companion to ``calfcord-mcp-codegen``. Codegen writes the
agent-side *schema* module (``schemas/<server>.py``); this command writes the
bridge-side *transport* entry in ``mcp.json``. Both are keyed by the same server
name, and a working server needs both (codegen for the schema, this for the
transport).

Unlike codegen, **this command never connects to the MCP server.** The entry is
built purely from the supplied flags, so it is offline, instant, and touches
nothing but the one file it writes.

Three things this command owns so the operator can't get them wrong:

* **The server name** (first positional) is validated against the selector
  grammar (:func:`~calfcord.mcp.selector.is_valid_server_name`) before anything
  else — it is the ``mcp.json`` key, the schema-module name, and the
  ``<server>`` segment agents type in their ``tools:`` selectors, so a typo here
  surfaces as a boot-time ``unknown server`` far from its cause.
* **Secrets are never written as literals.** ``mcp.json`` is committed, so every
  ``--env`` / ``--header`` value must carry a ``$VAR`` reference that calfkit
  expands from the *bridge's* environment at load time (see
  ``docs/mcp-tools.md`` §5/§6). A value with no ``$VAR`` is refused unless
  ``--allow-literal`` is passed for a genuinely non-secret value (e.g. a
  ``Content-Type`` header). The check accepts exactly the values calfkit will
  expand (escapes like ``$$`` and malformed refs like ``${VAR`` are refused), so
  the common slip — pasting a raw token — can't reach the committed file.
* **The on-disk shape stays valid.** The constructed entry is validated against
  calfkit's reference schema (:func:`calfkit.mcp.mcp_json_schema`, new in 0.4.1)
  before the file is touched. The schema validates the *un-expanded* file, so the
  check needs none of the ``$VAR`` secrets to be set in the authoring shell.

After writing, the command cross-checks ``MCP_CATALOG`` and warns (does not
fail) when no schema module is committed for the server yet — the inverse of
codegen's post-write verify, closing the loop between the two commands.

Run::

    uv run calfcord-mcp-add gmail --command "npx -y @some-org/gmail-mcp-server" --env GMAIL_OAUTH_TOKEN
    uv run calfcord-mcp-add drive --url https://mcp.example.com/drive --header "Authorization=Bearer $DRIVE_MCP_TOKEN"
    uv run calfcord-mcp-add gmail --command "..." --dry-run   # print merged file, write nothing
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from calfkit.mcp import mcp_json_schema
from jsonschema import Draft202012Validator

from calfcord.mcp.catalog import MCP_CATALOG
from calfcord.mcp.config import resolve_config_path
from calfcord.mcp.selector import is_valid_server_name

# calfkit's ``$VAR`` expansion grammar, replicated so this command accepts a
# secret reference iff calfkit will actually expand it. Three alternatives, with
# the ``$$`` escape matched FIRST (calfkit turns it into a literal ``$`` — NOT a
# reference) so an escaped dollar can't masquerade as a secret, and a balanced
# ``${VAR}`` required (a bare ``${VAR`` matches nothing here, exactly as in
# calfkit, so it would ship literal and is refused). Written locally rather than
# imported because calfkit's pattern is private; kept in lockstep on purpose.
_VAR_PATTERN = re.compile(r"\$\$|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")


def _references_var(value: str) -> bool:
    """True iff ``value`` contains a real ``$VAR`` / ``${VAR}`` reference.

    Mirrors calfkit's expander: a match counts as a reference unless it is the
    ``$$`` escape (which calfkit collapses to a literal ``$``). So this returns
    True for exactly the values calfkit will expand to a secret, and False for
    plain tokens, ``$$``-escaped literals, and malformed refs like ``${VAR``
    that calfkit leaves verbatim — the cases a naive "contains a $" check would
    wrongly wave through.
    """
    return any(m.group(0) != "$$" for m in _VAR_PATTERN.finditer(value))


# A bare ``--env NAME`` shorthand must be a valid env var name so the ``$NAME``
# reference it expands to actually resolves.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and cross-validate the command's arguments.

    Raises:
        SystemExit: (exit code 2, via :meth:`argparse.ArgumentParser.error`) for
            a bad server name, a missing/duplicated transport, or an ``--env`` /
            ``--header`` used with the wrong transport.
    """
    parser = argparse.ArgumentParser(
        prog="calfcord-mcp-add",
        description=(
            "Add or update one MCP server's transport entry in mcp.json (the "
            "bridge-side companion to calfcord-mcp-codegen). Never connects to the "
            "server; the entry is built from the flags below."
        ),
        epilog=(
            "Secrets must be $VAR references, not literals: mcp.json is committed and "
            "calfkit expands $VAR from the bridge's environment at load. Run "
            "calfcord-mcp-codegen for the same server name to generate its schema module."
        ),
    )
    parser.add_argument(
        "server",
        help="MCP server name: the mcp.json key, the schema-module name, and the "
        "<server> selector segment. Must match [a-z0-9_]{1,64}.",
    )
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument(
        "--command",
        metavar="CMD",
        help="Shell command to launch a stdio server, e.g. 'npx -y @org/server'. "
        "Split into command + args. Mutually exclusive with --url.",
    )
    transport.add_argument(
        "--url",
        help="Streamable-HTTP endpoint of the server. Mutually exclusive with --command.",
    )
    parser.add_argument(
        "--env",
        action="append",
        metavar="NAME|KEY=VALUE",
        default=[],
        help="stdio only. 'NAME' is shorthand for NAME=$NAME; 'KEY=VALUE' sets an "
        "explicit entry. VALUE must contain a $VAR reference (see --allow-literal). "
        "Repeatable.",
    )
    parser.add_argument(
        "--header",
        action="append",
        metavar="KEY=VALUE",
        default=[],
        help="HTTP only. Request header, e.g. 'Authorization=Bearer $TOKEN'. VALUE "
        "must contain a $VAR reference (see --allow-literal). Repeatable.",
    )
    parser.add_argument(
        "--allow-literal",
        action="store_true",
        help="Permit an --env/--header VALUE with no $VAR reference (use only for a "
        "genuinely non-secret value such as 'Content-Type=application/json').",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the server if it already exists in mcp.json (default: refuse).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resulting mcp.json to stdout and exit without writing.",
    )

    args = parser.parse_args(argv)

    if not is_valid_server_name(args.server):
        parser.error(
            f"invalid server name {args.server!r}; must match [a-z0-9_]{{1,64}} "
            "(lowercase letters, digits, underscore). It is the mcp.json key, the "
            "schema-module name, and the `mcp/<server>` selector segment."
        )
    # env is a stdio concept, headers an HTTP concept — reject the cross pairing
    # so a misplaced secret fails here, not as a silently-ignored field at boot.
    if args.url is not None and args.env:
        parser.error("--env applies to stdio servers (--command); HTTP servers carry secrets in --header")
    if args.command is not None and args.header:
        parser.error("--header applies to HTTP servers (--url); stdio servers carry secrets in --env")
    return args


def _kv_pairs(
    raw_items: list[str],
    *,
    flag: str,
    allow_shorthand: bool,
    allow_literal: bool,
) -> dict[str, str]:
    """Parse repeated ``KEY=VALUE`` (and, for --env, bare ``NAME``) into a dict.

    Enforces the reference-only secret policy: a VALUE with no ``$VAR`` reference
    is rejected — keeping secrets out of the committed file is the whole reason
    this command exists — unless ``allow_literal`` is set. A duplicate key is an
    error rather than a silent last-wins, since repeating a key is almost always
    a mistake.

    Raises:
        SystemExit: malformed item, empty/duplicate key, or a literal value where
            a ``$VAR`` reference is required.
    """
    out: dict[str, str] = {}
    for raw in raw_items:
        key, sep, value = raw.partition("=")
        if sep:
            key = key.strip()
        else:
            if not allow_shorthand:
                raise SystemExit(f"error: {flag} {raw!r} must be KEY=VALUE")
            key = raw.strip()
            if not _ENV_NAME.match(key):
                raise SystemExit(
                    f"error: {flag} shorthand {raw!r} must be a valid env var name "
                    "[A-Za-z_][A-Za-z0-9_]* (it becomes the reference $name); use "
                    "KEY=VALUE for anything else."
                )
            value = f"${key}"
        if not key:
            raise SystemExit(f"error: {flag} {raw!r} has an empty key")
        if key in out:
            raise SystemExit(f"error: {flag} sets {key!r} more than once")
        if not allow_literal and not _references_var(value):
            raise SystemExit(
                f"error: {flag} value for {key!r} has no $VAR reference: {value!r}. "
                "mcp.json is committed, so secrets must be $VAR references expanded "
                f"from the bridge env (e.g. {key}=$SECRET_NAME). Pass --allow-literal "
                "for a genuinely non-secret value."
            )
        out[key] = value
    return out


def _build_entry(args: argparse.Namespace) -> dict[str, Any]:
    """Build the mcp.json server spec from the parsed flags.

    stdio: ``--command "npx -y x"`` → ``{"command": "npx", "args": ["-y", "x"]}``
    (shlex-split into the discrete ``command`` + ``args`` fields the mcp.json
    parser reads already-split). HTTP: ``--url`` → an explicit
    ``{"type": "http", "url": ...}`` (the documented form; the explicit ``type``
    removes any command/url ambiguity). ``args`` / ``env`` / ``headers`` are
    emitted only when non-empty.

    Raises:
        SystemExit: an ``--command`` that is empty or not valid shell syntax, or
            a secret-policy violation surfaced by :func:`_kv_pairs`.
    """
    if args.command is not None:
        try:
            parts = shlex.split(args.command)
        except ValueError as e:
            raise SystemExit(
                f"error: --command is not valid shell syntax ({e}): {args.command!r}. Check your quoting."
            ) from e
        if not parts:
            raise SystemExit(f"error: --command is empty after parsing: {args.command!r}")
        entry: dict[str, Any] = {"command": parts[0]}
        if parts[1:]:
            entry["args"] = parts[1:]
        env = _kv_pairs(args.env, flag="--env", allow_shorthand=True, allow_literal=args.allow_literal)
        if env:
            entry["env"] = env
        return entry

    entry = {"type": "http", "url": args.url}
    headers = _kv_pairs(args.header, flag="--header", allow_shorthand=False, allow_literal=args.allow_literal)
    if headers:
        entry["headers"] = headers
    return entry


def _validate_entry(server: str, entry: dict[str, Any]) -> None:
    """Validate the constructed entry against calfkit's reference schema.

    Validates ``{"mcpServers": {server: entry}}`` in isolation (not the whole
    merged file) so an error names only the change being made and never trips on
    a pre-existing entry the operator isn't touching. The schema validates the
    *un-expanded* form, so no ``$VAR`` secret need be set in this shell.

    Raises:
        SystemExit: the entry violates the schema, with every offending location.
    """
    doc = {"mcpServers": {server: entry}}
    validator = Draft202012Validator(mcp_json_schema())
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if not errors:
        return
    detail = "\n".join(f"  at {'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors)
    raise SystemExit(f"error: generated entry for {server!r} failed mcp.json schema validation:\n{detail}")


def _load_config(path: Path) -> dict[str, Any]:
    """Read the existing mcp.json, or an empty skeleton if it does not exist.

    Standardises on the wrapped ``{"mcpServers": {...}}`` shape. A non-empty
    top-level object with no ``mcpServers`` key is the de-facto *bare* shape
    calfkit also accepts; rather than silently nest it (which would reinterpret
    every existing server as config metadata), refuse and ask for a wrap.

    Raises:
        SystemExit: unreadable/invalid JSON, a non-object top level or
            ``mcpServers``, or a non-empty bare-shape file.
    """
    if not path.exists():
        return {"mcpServers": {}}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        # A path that exists() can still fail to read — a directory, or a
        # permission-denied file. calfkit's own loader catches this; mirror it.
        raise SystemExit(f"error: cannot read {path}: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} top-level value must be an object, got {type(data).__name__}")
    servers = data.get("mcpServers")
    if servers is None:
        if data:
            raise SystemExit(
                f"error: {path} has no 'mcpServers' key but is non-empty (bare/legacy "
                'shape). Wrap your servers under {"mcpServers": {...}} and re-run.'
            )
        data["mcpServers"] = {}
    elif not isinstance(servers, dict):
        raise SystemExit(f"error: {path} 'mcpServers' must be an object, got {type(servers).__name__}")
    return data


def _write_config(path: Path, data: dict[str, Any]) -> None:
    """Write the merged config as 2-space JSON with a trailing newline.

    Dict insertion order is preserved, so existing servers keep their order and a
    new server is appended last. Strict JSON (no comments), matching calfkit's
    ``json.loads`` parser.
    """
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _warn_if_no_schema(server: str) -> None:
    """Warn (do not fail) when no committed schema module exists for ``server``.

    The mcp.json entry alone is not enough: ``load_mcp_servers`` attaches
    ``MCP_CATALOG[server]`` at bridge boot and fails the load if it's missing.
    Surface that here, at authoring time, with the exact fix — the inverse of
    codegen's post-write catalog verify.
    """
    if server in MCP_CATALOG:
        return
    print(
        f"WARNING: wrote mcp.json entry for {server!r} but no schema module is committed "
        f"in src/calfcord/mcp/schemas/. The bridge (calfkit-mcp) will reject this server "
        f"at boot until you generate it, e.g.:\n"
        f"  uv run calfcord-mcp-codegen {server} --command ...   # or --url ...",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    """Build the entry, validate it, merge it into mcp.json, and write.

    Validation runs before the file is read so a malformed entry fails fast
    regardless of file state. Exits non-zero (via ``SystemExit``) on any error;
    ``--dry-run`` prints the merged result and writes nothing.
    """
    args = _parse_args(argv)
    entry = _build_entry(args)
    _validate_entry(args.server, entry)

    path = resolve_config_path()
    data = _load_config(path)
    servers: dict[str, Any] = data["mcpServers"]

    existed = args.server in servers
    if existed and not args.force:
        raise SystemExit(f"error: server {args.server!r} already in {path}; pass --force to overwrite.")
    servers[args.server] = entry

    if args.dry_run:
        print(json.dumps(data, indent=2))
        print(f"(dry-run: {path} not modified)", file=sys.stderr)
        return

    try:
        _write_config(path, data)
    except OSError as e:
        # An operator-supplied CALFCORD_MCP_CONFIG can point at a missing parent
        # dir or an unwritable location — a clean error, not a raw traceback.
        raise SystemExit(f"error: cannot write {path}: {e}") from e
    print(f"{'updated' if existed else 'added'} server {args.server!r} in {path}")
    _warn_if_no_schema(args.server)


if __name__ == "__main__":
    main()
