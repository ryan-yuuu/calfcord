"""``calfcord-cli`` argparse entry point — the management command dispatcher.

The native ``calfcord`` shim translates user-facing management subcommands
(``calfcord init``, ``calfcord agent ...``) into ``calfcord-cli <subcommand>``
and execs them through the same locked venv as the runners. ``prog="calfcord"``
so ``--help`` reads as the command the user actually types. Future verbs
register additional subparsers; the shim only needs to know the top-level verb
(``init`` / ``agent`` / ``router``) to dispatch them here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from calfcord.cli import (
    agent_create,
    agent_edit,
    agent_inspect,
    agent_lifecycle,
    agent_tools,
    doctor,
    init,
    router_setup,
)
from calfcord.cli._fields import FIELDS
from calfcord.cli._prompts import make_prompter


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calfcord",
        description="Manage a calfcord install (configure, inspect).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Guided first-run configuration of the install's .env.")

    doctor_p = sub.add_parser(
        "doctor",
        help="Preflight an install: config, broker, Discord token, and agents.",
    )
    doctor_p.add_argument(
        "--offline",
        action="store_true",
        help="Skip the live Discord token check (no network).",
    )

    # ``agent`` is a verb group, not a leaf: ``required=True`` on its
    # sub-parsers makes a bare ``calfcord agent`` print help + exit non-zero
    # rather than silently no-op.
    agent_p = sub.add_parser("agent", help="Create, inspect, and edit agents.")
    agent_sub = agent_p.add_subparsers(dest="agent_command", required=True)

    create_p = agent_sub.add_parser("create", help="Create a new agent (guided wizard).")
    create_p.add_argument("name", nargs="?", help="Agent name (omit to be prompted).")

    list_p = agent_sub.add_parser("list", help="List all agents.")
    list_p.add_argument("--json", action="store_true", help="Emit a JSON array instead of a table.")

    show_p = agent_sub.add_parser("show", help="Show one agent's full config.")
    show_p.add_argument("name", help="Agent name.")
    show_p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    edit_p = agent_sub.add_parser("edit", help="Interactively edit any of an agent's config fields.")
    edit_p.add_argument("name", nargs="?", help="Agent name (omit to pick interactively).")

    set_p = agent_sub.add_parser("set", help="Set config fields non-interactively (scripting/CI).")
    set_p.add_argument("name", help="Agent name.")
    _add_set_flags(set_p)

    rename_p = agent_sub.add_parser("rename", help="Rename an agent (file, slash command, and state).")
    rename_p.add_argument("old", help="Current agent name.")
    rename_p.add_argument("new", help="New agent name.")

    delete_p = agent_sub.add_parser("delete", help="Delete an agent.")
    delete_p.add_argument("name", help="Agent name.")
    delete_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    delete_p.add_argument("--keep-state", action="store_true", help="Keep the agent's channel-subscription state file.")

    tools_p = agent_sub.add_parser("tools", help="Interactively edit an agent's tool list.")
    tools_p.add_argument("name", nargs="?", help="Agent name (omit to pick interactively).")

    # ``router`` mirrors ``agent``: a verb group, not a leaf. ``required=True``
    # makes a bare ``calfcord router`` print help + exit non-zero so the group
    # can grow further commands later.
    router_p = sub.add_parser("router", help="Manage the ambient-message router.")
    router_sub = router_p.add_subparsers(dest="router_command", required=True)
    router_sub.add_parser("setup", help="Configure the OPTIONAL ambient router (provider, model).")
    return parser


def _resolve_home() -> Path | None:
    """Return the install home from ``$CALFCORD_HOME``, or ``None`` for dev runs.

    The shim exports ``CALFCORD_HOME`` so config + agents resolve under the
    install layout; a bare ``uv run calfcord-cli init`` (no shim) leaves it
    unset, which selects the project-local ``./.env`` / ``./agents`` defaults.
    An empty value is treated as unset so a stray ``CALFCORD_HOME=`` does not
    point config at ``"/config/.env"``.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else None


def _resolve_state_dir(home: Path | None) -> Path:
    """Per-agent state dir (channel-subscription JSON), needed by rename/delete.

    Mirrors the runner's resolution so the CLI moves/removes the exact file the
    agent reads: ``CALFKIT_STATE_DIR`` wins (the shim sets it to
    ``$H/state/agents``); otherwise ``$H/state/agents`` on a native install, or
    the dev ``./state/agents`` default.
    """
    override = os.environ.get("CALFKIT_STATE_DIR")
    if override:
        return Path(override)
    if home is not None:
        return home / "state" / "agents"
    return Path("state") / "agents"


def _add_set_flags(set_p: argparse.ArgumentParser) -> None:
    """Add one ``agent set`` flag per editable field, driven by the FIELDS registry.

    The single ``provider_model`` row becomes two flags (``--provider`` /
    ``--model``) so a provider switch can carry its model; every other field gets
    its declared flag with ``dest`` = the field key, so the dispatcher hands
    ``run_set`` a clean ``{key: value}`` dict with no second mapping to drift.
    """
    for field in FIELDS:
        if field.kind == "provider_model":
            continue
        suffix = f" ({field.int_min}-{field.int_max})" if field.kind == "int" else ""
        set_p.add_argument(field.flag, dest=field.key, default=None, help=field.label + suffix)
    set_p.add_argument("--provider", dest="provider", default=None, help="Model provider")
    set_p.add_argument("--model", dest="model", default=None, help="Model id")


def _collect_set_updates(args: argparse.Namespace) -> dict[str, str]:
    """Gather the provided ``agent set`` flags into a ``{field_key: value}`` dict.

    A ``--system-prompt @file`` value is expanded to the file's contents so an
    operator can script a multi-line prompt; every other value is the raw string.
    Raises OSError if an ``@file`` can't be read (the caller surfaces it cleanly).
    """
    updates: dict[str, str] = {}
    for field in FIELDS:
        if field.kind == "provider_model":
            continue
        value = getattr(args, field.key)
        if value is None:
            continue
        if field.key == "system_prompt" and value.startswith("@"):
            value = Path(value[1:]).read_text(encoding="utf-8")
        updates[field.key] = value
    for key in ("provider", "model"):
        value = getattr(args, key)
        if value is not None:
            updates[key] = value
    return updates


def _run_agent(args: argparse.Namespace) -> int:
    """Dispatch a ``calfcord agent <verb>`` command, resolving the install paths once."""
    home = _resolve_home()
    env_path, agents_dir = init.resolve_paths(home)
    cmd = args.agent_command
    if cmd == "create":
        return agent_create.run(make_prompter(), agents_dir=agents_dir, env_path=env_path, name=args.name)
    if cmd == "list":
        return agent_inspect.run_list(agents_dir, as_json=args.json)
    if cmd == "show":
        return agent_inspect.run_show(agents_dir, args.name, as_json=args.json)
    if cmd == "edit":
        return agent_edit.run(make_prompter(), agents_dir=agents_dir, env_path=env_path, name=args.name)
    if cmd == "set":
        try:
            updates = _collect_set_updates(args)
        except OSError as e:
            print(f"error: {e}")
            return 1
        return agent_lifecycle.run_set(agents_dir, args.name, updates)
    if cmd == "rename":
        return agent_lifecycle.run_rename(agents_dir, _resolve_state_dir(home), args.old, args.new)
    if cmd == "delete":
        return agent_lifecycle.run_delete(
            make_prompter(), agents_dir, _resolve_state_dir(home), args.name,
            yes=args.yes, keep_state=args.keep_state,
        )
    # ``tools`` (and any unhandled verb — argparse ``required=True`` prevents the latter)
    return agent_tools.run(make_prompter(), agents_dir=agents_dir, name=args.name)


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Route a parsed command to its handler (the interactive, prompt-driven part)."""
    if args.command == "init":
        env_path, agents_dir = init.resolve_paths(_resolve_home())
        return init.run(make_prompter(), env_path=env_path, agents_dir=agents_dir)

    if args.command == "doctor":
        # Preflight the same config/.env + agents/ the runners load.
        env_path, agents_dir = init.resolve_paths(_resolve_home())
        return doctor.run(env_path=env_path, agents_dir=agents_dir, offline=args.offline)

    if args.command == "agent":
        return _run_agent(args)

    if args.command == "router" and args.router_command == "setup":
        # Reuse init's path resolution so the router wizard writes the same
        # config/.env the runners load (native: $H/config/.env; dev: ./.env).
        env_path, _ = init.resolve_paths(_resolve_home())
        return router_setup.run(make_prompter(), env_path=env_path)

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # The dispatch drives interactive prompts; trap the two ways an operator/host
    # ends one abruptly so the management CLI exits cleanly instead of dumping a
    # traceback (matching every other calfcord entry point). The run_* handlers
    # already map their own filesystem errors to exit codes, so an interrupt or a
    # raw-mode failure is all that should escape to here.
    try:
        return _dispatch(parser, args)
    except KeyboardInterrupt:
        print("\naborted.")
        return 130
    except EOFError:
        print("error: this command needs an interactive terminal (stdin reached EOF).")
        return 1
    except OSError:
        # InquirerPy/prompt_toolkit raises OSError (EINVAL) when it can't put a
        # non-TTY stdin (piped / CI) into raw mode. Surface that cleanly, but only
        # when stdin genuinely isn't a TTY — re-raise anything else rather than
        # masking a real bug behind a friendly message.
        if not sys.stdin.isatty():
            print("error: this command needs an interactive terminal (stdin is not a TTY).")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
