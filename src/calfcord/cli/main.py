"""``calfcord-cli`` argparse entry point — the management command dispatcher.

The native ``calfcord`` shim translates user-facing management subcommands
(``calfcord init``, ``calfcord agent ...``) into ``calfcord-cli <subcommand>``
and execs them through the same locked venv as the runners. ``prog="calfcord"``
so ``--help`` reads as the command the user actually types. Future verbs
register additional subparsers; the shim only needs to know the top-level verb
(``init`` / ``agent``) to dispatch them here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from calfcord.cli import agent_tools, init
from calfcord.cli._prompts import make_prompter


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calfcord",
        description="Manage a calfcord install (configure, inspect).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Guided first-run configuration of the install's .env.")

    # ``agent`` is a verb group, not a leaf: ``required=True`` on its
    # sub-parsers makes a bare ``calfcord agent`` print help + exit non-zero
    # rather than silently no-op, so the group can grow further commands later.
    agent_p = sub.add_parser("agent", help="Manage agents.")
    agent_sub = agent_p.add_subparsers(dest="agent_command", required=True)
    tools_p = agent_sub.add_parser("tools", help="Interactively edit an agent's tool list.")
    tools_p.add_argument("name", nargs="?", help="Agent name (omit to pick interactively).")
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        env_path, agents_dir = init.resolve_paths(_resolve_home())
        return init.run(make_prompter(), env_path=env_path, agents_dir=agents_dir)

    if args.command == "agent" and args.agent_command == "tools":
        # Reuse init's path resolution so the editor and the config flow agree
        # on where agents live (CALFKIT_AGENTS_DIR | $H/agents | ./agents).
        agents_dir = init.resolve_paths(_resolve_home())[1]
        return agent_tools.run(make_prompter(), agents_dir=agents_dir, name=args.name)

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    sys.exit(main())
