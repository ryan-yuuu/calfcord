"""Resolve the calfcord install root for codex-local on-disk paths.

The codex auth store and prompt cache both live under the install home so
they sit beside ``config/.env``, the agents dir, and ``state/`` — and so they
move with the install when an operator relocates it (``CALFCORD_HOME``), runs
two installs on one host, or runs under systemd. Every other subsystem
(``cli/main.py``, ``mcp/config.py``, ``bridge/gateway.py``) roots its paths at
``$CALFCORD_HOME``; this is the codex package's copy of that resolution so the
two call sites can't drift on the empty-string guard.
"""

from __future__ import annotations

import os
from pathlib import Path


def calfcord_home() -> Path:
    """The install root: ``$CALFCORD_HOME``, else the shim's ``~/.calfcord`` default.

    An empty ``CALFCORD_HOME=`` counts as unset (so a stray assignment doesn't
    root paths at ``/``), matching the guard the CLI/mcp/bridge resolvers use.
    Resolved at call time, not import, so the env is read where the path is used.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else Path.home() / ".calfcord"
