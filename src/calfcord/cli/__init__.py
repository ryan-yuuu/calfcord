"""End-user management CLI for a native calfcord install.

This package backs the ``calfcord-cli`` console script (and, via the
``calfcord`` shim's ``init|agent|tools`` dispatch, the user-facing
``calfcord init`` / ``calfcord agent ...`` commands). It is deliberately
separate from the per-process runners (``calfkit-bridge`` etc.): those run
the system, while this package *configures* an install. Keeping it in its
own package lets the shim translate management subcommands to a single
argparse entry point without coupling them to any runner's import graph.
"""

from __future__ import annotations
