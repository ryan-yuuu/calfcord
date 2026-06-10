"""CLI entry point for an ``mcp-<server>`` deployment: ``calfkit-mcp <server>``.

Hosts exactly **one** :class:`~calfkit.mcp.mcp_toolbox.MCPToolbox` from
``mcp.json`` on its own calfkit :class:`Worker`. One process per server is
deliberate: calfkit fails a toolbox's hosting worker at boot when the MCP
server is unreachable, and server entries are operator-supplied
commands/URLs — the config most likely to be wrong. Per-server processes
keep one bad entry from taking down every other MCP tool, let the
supervisor restart/back off each server independently, and make
``calfcord mcp restart <server>`` reload just that server's entry.

This is the **only** process type that reads ``mcp.json`` (transport +
credentials). On startup the toolbox connects to its MCP server, lists its
tools, and advertises them on the compacted ``mcp.capabilities`` topic;
agents resolve their ``mcp/...`` selectors against that advertisement per
turn — so agents never hold MCP secrets and never restart for tool changes.

Run::

    uv run calfkit-mcp <server>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from calfkit.client import Client
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfcord._provisioning import PROVISIONING
from calfcord._worker_runtime import run_worker_until_signal
from calfcord.mcp.config import McpConfigError, load_one_server, resolve_config_path

logger = logging.getLogger(__name__)

_REPLY_TOPIC = "calfkit.mcp.reply"
"""Named reply topic for the MCP client. Distinct from the bridge's
``discord.outbox`` and the tools runner's ``calfkit.tools.reply`` so nothing
this process emits is re-projected by another consumer."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="calfkit-mcp",
        description="Host one MCP server from mcp.json as a calfkit toolbox.",
    )
    parser.add_argument("server", help="server name (a key under mcpServers in mcp.json)")
    return parser.parse_args(argv)


async def _amain(server_name: str) -> None:
    config_path = resolve_config_path()
    try:
        # Expands only THIS server's $VAR references: a sibling entry's unset
        # secret must not fail an unrelated server's boot (per-server
        # isolation). Empty-registry and unknown-name failures carry their
        # own operator-grade messages from the loader.
        toolbox = load_one_server(config_path, server_name)
    except McpConfigError as exc:
        # Operator-recoverable config problems get a clean exit + message,
        # not a traceback — and no broker connection is ever attempted.
        raise SystemExit(f"failed to load MCP server {server_name!r}: {exc}") from exc

    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    async with Client.connect(
        server_urls, reply_topic=_REPLY_TOPIC, provisioning=PROVISIONING
    ) as client:
        worker = Worker(client, [toolbox])
        logger.info(
            "starting calfkit-mcp worker server=%s dispatch_topic=%s broker=%s",
            server_name,
            toolbox.subscribe_topics[0],
            server_urls,
        )
        await run_worker_until_signal(worker, drain_label=f"mcp server {server_name!r}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    args = _parse_args()
    try:
        asyncio.run(_amain(args.server))
    except KeyboardInterrupt:
        logger.info("calfkit-mcp shutting down")


if __name__ == "__main__":
    main()
