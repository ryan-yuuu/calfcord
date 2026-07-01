"""The shared seam every supervisor surface builds on (DRY consolidation, Fix #14).

``lifecycle`` / ``roster`` / ``component`` and ``cli.doctor`` each grew their own
copy of four identical building blocks: resolve a per-home REST client, decide
whether the workspace REST server answers, the one not-running hint, and the
``{"data": [...]}``-vs-bare-list process-list normalizer. Four copies are four
chances to drift (a different hint string, a different wire-shape tolerance), so
they live here once and the surfaces re-export thin aliases for the names their
tests reference. This is a pure refactor — no behavior change.

Import-light like the rest of :mod:`calfcord.supervisor`, so every CLI-side
surface that consumes it stays cheap to import.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from calfcord.supervisor.client import ProcessComposeClient

# The single hint shown when an op needs a running workspace and there isn't one.
# Centralized so every lifecycle surface (substrate, agent roster, components)
# speaks with exactly one voice.
WORKSPACE_NOT_RUNNING_HINT = "workspace not running (start it with: disco start)"


def resolve_client(
    client: ProcessComposeClient | None, home: str | os.PathLike[str]
) -> ProcessComposeClient:
    """Resolve the REST client, defaulting to a per-home supervisor client.

    The port is derived from ``$CALFCORD_HOME`` (:func:`lifecycle.pc_port_for`) —
    the same port the ``up -p`` flag pinned — so a second install on one host talks
    to its own supervisor and does not collide. ``pc_port_for`` is imported lazily
    (it lives in :mod:`lifecycle`, which imports this leaf module) so the seam has
    no module-level dependency on ``lifecycle`` and the two cannot form an import
    cycle.
    """
    if client is not None:
        return client
    from calfcord.supervisor.lifecycle import pc_port_for

    return ProcessComposeClient(port=pc_port_for(home))


async def workspace_is_up(client: ProcessComposeClient) -> bool:
    """Whether the supervisor REST server answers — a successful ``project_state``.

    The client raises ``RuntimeError`` on a transport failure (server not up /
    wrong port), which is exactly "the workspace isn't open" here; any other error
    is a real bug and is left to propagate (it is not swallowed into "down").
    """
    try:
        await client.project_state()
    except RuntimeError:
        return False
    return True


def iter_process_dicts(payload: object) -> Iterator[dict]:
    """Yield the dict process entries from a ``list_processes`` payload.

    Process Compose returns either a bare list or ``{"data": [...]}`` depending on
    version (the wire shape wobbles across versions), so accept both, and skip
    non-dict entries defensively so a stray wire-shape wobble never crashes a
    caller (the status board / the ps physical view / the drift read).
    """
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    for item in items or []:
        if isinstance(item, dict):
            yield item
