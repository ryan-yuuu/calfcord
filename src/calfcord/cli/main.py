"""``calfcord-cli`` argparse entry point — the management command dispatcher.

The native ``calfcord`` shim translates user-facing management subcommands
(``calfcord init`` today; ``calfcord agent ...`` in PR 4) into
``calfcord-cli <subcommand>`` and execs them through the same locked venv as
the runners. ``prog="calfcord"`` so ``--help`` reads as the command the user
actually types. Only ``init`` is wired here; future verbs register additional
subparsers without touching the shim.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from calfcord.cli import init
from calfcord.cli._prompts import make_prompter


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calfcord",
        description="Manage a calfcord install (configure, inspect).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Guided first-run configuration of the install's .env.")
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

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    sys.exit(main())
