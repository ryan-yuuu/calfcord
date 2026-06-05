"""CLI entry point for the ``calfkit-mcp`` MCP bridge deployment.

Hosts every live :class:`~calfkit.mcp.McpServer` declared in the deployment's
``mcp.json`` (resolved by :func:`calfcord.mcp.config.load_mcp_servers`) on a
single calfkit :class:`~calfkit.worker.Worker`. This is the **bridge** process:
it owns the real MCP transport + credentials, consuming each tool's
``mcp.<server>.<tool>.input`` topic and publishing results to ``...output``.
Agents never run MCP locally — they publish ``Call`` messages onto those topics
and this worker services them.

This deployment is intentionally minimal compared to ``calfkit-tools``: it has
**no** Discord, persona, or A2A machinery. MCP tools are pure request/response
over calfkit topics; there is no Discord projection and no agent phonebook to
wire. The only resources are a calfkit :class:`~calfkit.client.Client` and the
worker hosting the MCP servers.

Run::

    uv run calfkit-mcp
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from calfkit.client import Client
from calfkit.mcp import McpServer
from calfkit.mcp.exceptions import McpConfigError
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfcord._provisioning import PROVISIONING, provision_and_start_broker
from calfcord._worker_runtime import run_worker_until_signal
from calfcord.mcp.config import load_mcp_servers, resolve_config_path

logger = logging.getLogger(__name__)

_REPLY_TOPIC = "calfkit.mcp.reply"
"""Named reply topic for the MCP bridge client. Distinct from the tools
runner's ``calfkit.tools.reply`` and the bridge's ``discord.outbox`` so
replies that this worker awaits route back to this process and are not
consumed by another deployment."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="calfkit-mcp",
        description="Run the calfkit MCP bridge worker (hosts MCP servers).",
    )
    return parser.parse_args(argv)


def _resolve_mcp_nodes(servers: Mapping[str, McpServer], config_path: Path) -> list[McpServer]:
    """Return the registry's servers, failing fast on an empty registry.

    Extracted from ``_amain`` so the empty-registry guard can be tested without
    standing up Kafka. A worker with zero nodes boots inert — subscribing to no
    topics while appearing healthy in production logs — so an empty ``mcp.json``
    is surfaced at boot rather than served silently.

    The former key/``name=`` mismatch guard is gone. ``McpServer.name`` is the
    *normalized* name (``.``/``-`` → ``_``), so ``from_file`` setting
    ``name=<config key>`` is not alone sufficient — the guarantee rests on
    :func:`load_mcp_servers` requiring every key to exist in ``MCP_CATALOG``,
    whose keys are constrained to ``[a-z0-9_]`` (the chars topic normalization
    leaves untouched). So ``server.name == key`` for every loadable server and
    the bridge's ``mcp.<name>.<tool>.*`` topics match the agents' schema-only
    nodes by construction.
    """
    nodes = list(servers.values())
    if not nodes:
        raise SystemExit(
            f"no MCP servers configured in {config_path}; nothing to host. "
            'Add a server under "mcpServers", or point CALFCORD_MCP_CONFIG at a populated file.'
        )
    return nodes


async def _amain() -> None:
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    # Load + validate the registry before connecting so a misconfig (missing
    # file, bad JSON, an unset $VAR, a server without a committed schema, or an
    # empty registry) fails fast without opening a broker connection.
    config_path = resolve_config_path()
    try:
        servers = load_mcp_servers(config_path)
    except McpConfigError as e:
        raise SystemExit(
            f"failed to load MCP servers from {config_path}: {e}\n"
            "Check the file path, JSON syntax, and that every referenced $VAR is set; "
            "if a server has no committed schema, run "
            "`uv run calfcord-mcp-codegen <server> --command ... (or --url ...)`."
        ) from e
    mcp_nodes = _resolve_mcp_nodes(servers, config_path)

    async with Client.connect(server_urls, reply_topic=_REPLY_TOPIC, provisioning=PROVISIONING) as client:
        # Provision the reply topic, then eagerly start the broker so the reply
        # dispatcher is live before any node awaits a reply. The worker's
        # MCP-bridge node topics are provisioned later by Worker.run()'s startup
        # hook (via run_worker_until_signal below).
        await provision_and_start_broker(client)

        worker = Worker(client, mcp_nodes)
        logger.info(
            "starting calfkit-mcp worker servers=%s broker=%s reply_topic=%s config=%s",
            sorted(servers),
            server_urls,
            _REPLY_TOPIC,
            config_path,
        )
        await run_worker_until_signal(worker, drain_label="mcp bridge worker")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    _parse_args()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("calfkit-mcp shutting down")


if __name__ == "__main__":
    main()
