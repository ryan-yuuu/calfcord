"""Best-effort CLI read of the live MCP capability view.

The ``mcp.capabilities`` compacted topic is the org-wide source of truth for
which MCP tools exist *right now* — including servers hosted by other
machines that this host's ``mcp.json`` knows nothing about. The tools
editor reads it to offer per-tool ``mcp/<server>/<tool>`` rows.

Strictly best-effort: the CLI must work offline (broker down, workspace
closed, dev laptop on a plane), so every failure path degrades to an empty
snapshot — the editor then falls back to server-level rows from the local
``mcp.json``. The short catch-up timeout keeps the editor snappy; a partial
catch-up just means fewer rows, never an error.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

CAPABILITY_TOPIC = "mcp.capabilities"
# Bounded so the interactive editor stays snappy on a slow/filtered broker;
# a healthy local broker replays the tiny compacted topic well inside this.
_DEFAULT_TIMEOUT_SECONDS = 1.5


def snapshot_capability_tools(
    server_urls: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> dict[str, list[str]] | None:
    """``{server: [tool, ...]}`` from the live capability view, or ``None``.

    Replays the compacted topic with a ``timeout``-bounded catch-up and
    returns each advertised toolbox's tool names (sorted). Any failure —
    unreachable broker, missing topic, replay timeout — returns ``None``
    (NOT ``{}``): callers can tell "the view answered and is empty" apart
    from "the view was unreachable" and tell the operator which one
    happened.
    """
    try:
        return asyncio.run(_snapshot(server_urls, timeout))
    except Exception as exc:
        logger.debug("capability view unavailable (%s); offline rows only", exc)
        return None


async def _snapshot(server_urls: str, timeout: float) -> dict[str, list[str]]:
    from calfkit.models.capability import CapabilityRecord
    from ktables import KafkaTable

    table: KafkaTable[CapabilityRecord] = KafkaTable.json(
        bootstrap_servers=server_urls,
        topic=CAPABILITY_TOPIC,
        model=CapabilityRecord,
        catchup_timeout=timeout,
        # Never create the topic from a read-only CLI peek: a missing topic
        # means no toolbox has ever advertised, which IS the answer ({}).
        ensure_topic=False,
    )
    await table.start()
    try:
        records = table.snapshot()
    finally:
        await table.stop()
    return {
        toolbox_id: sorted(tool.name for tool in record.tools)
        for toolbox_id, record in records.items()
    }
