"""CLI entry point for the ``calfkit-mcp`` MCP bridge deployment.

Hosts every live :class:`~calfkit.mcp.McpServer` registered in
:data:`calfcord.mcp.servers.MCP_SERVERS` on a single calfkit
:class:`~calfkit.worker.Worker`. This is the **bridge** process: it owns
the real MCP transport + credentials, consuming each tool's
``mcp.<server>.<tool>.input`` topic and publishing results to
``...output``. Agents never run MCP locally — they publish ``Call``
messages onto those topics and this worker services them.

This deployment is intentionally minimal compared to ``calfkit-tools``: it
has **no** Discord, persona, or A2A machinery. MCP tools are pure
request/response over calfkit topics; there is no Discord projection and
no agent phonebook to wire. The only resources are a calfkit
:class:`~calfkit.client.Client` and the worker hosting the MCP servers.

Run::

    uv run calfkit-mcp
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from calfkit.client import Client
from calfkit.mcp import McpServer
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfcord._worker_runtime import run_worker_until_signal
from calfcord.mcp.servers import MCP_SERVERS

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


def _resolve_mcp_nodes(servers: dict[str, McpServer]) -> list[McpServer]:
    """Validate the MCP server registry and return its values.

    Extracted from ``_amain`` so the registry guards can be tested without
    standing up Kafka. Two failure modes are caught here:

    * **Empty registry** — the worker would boot inert, subscribing to no
      topics while appearing healthy in production logs, serving nothing.
    * **Key/``name=`` mismatch** — calfkit derives a server's wire topics
      (``mcp.<name>.<tool>.*``) from its :attr:`~calfkit.mcp.McpServer.name`
      (the *normalized* name), but agents derive the *same* topics from the
      ``<server>`` selector segment, which equals the schema-module name =
      the registry key. If a registration's ``name=`` (or an inferred name)
      does not equal its key, the bridge subscribes to one set of topics
      while every agent publishes to another, so every call to that server
      hangs forever with no error. We detect the mismatch at boot and fail
      fast with the offending ``key != name`` pair(s).

    Args:
        servers: The ``server name -> McpServer`` registry (typically
            :data:`calfcord.mcp.servers.MCP_SERVERS`).

    Returns:
        The registry's :class:`~calfkit.mcp.McpServer` values, suitable for
        passing to :class:`~calfkit.worker.Worker`.

    Raises:
        SystemExit: When ``servers`` is empty (nothing to host), or when any
            entry's :attr:`~calfkit.mcp.McpServer.name` differs from its
            registry key (topics would not match the agents' nodes).
    """
    nodes = list(servers.values())
    if not nodes:
        raise SystemExit(
            "no MCP servers configured in calfcord.mcp.servers.MCP_SERVERS; "
            "nothing to host"
        )
    # Each server's wire topics derive from ``name=`` (calfkit normalizes it),
    # but agents derive the same topics from the selector ``<server>`` segment,
    # which equals the registry key. A key != name registration makes the
    # bridge listen on different topics than the agents publish to, so every
    # call to that server hangs silently. Surface it at boot.
    mismatches = [(key, server.name) for key, server in servers.items() if server.name != key]
    if mismatches:
        detail = ", ".join(f"{key!r} (name={name!r})" for key, name in mismatches)
        raise SystemExit(
            f"MCP server registry key(s) do not match the server's name=: "
            f"{detail}. The wire topics ``mcp.<name>.<tool>.*`` derive from "
            f"name=, while agents derive them from the selector ``<server>`` "
            f"segment (= the registry key), so a mismatch makes every call to "
            f'that server hang. Pass name="<key>" explicitly so the topics '
            f"match the agents' schema-only nodes."
        )
    return nodes


async def _amain() -> None:
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    # Resolve nodes before connecting so an empty-registry misconfig fails
    # fast (SystemExit) without opening a broker connection.
    mcp_nodes = _resolve_mcp_nodes(MCP_SERVERS)

    async with Client.connect(server_urls, reply_topic=_REPLY_TOPIC) as client:
        # Eagerly start the broker so the reply dispatcher is live before
        # any node tries to await a reply — mirrors the tools runner's and
        # bridge's boot-time eager start. ``broker.running`` is faststream's
        # public state flag (defined on BrokerUsecase); avoid the private
        # ``broker._connection`` attribute which can change shape between
        # faststream releases.
        if not client.broker.running:
            await client.broker.start()

        worker = Worker(client, mcp_nodes)
        logger.info(
            "starting calfkit-mcp worker servers=%s broker=%s reply_topic=%s",
            sorted(MCP_SERVERS),
            server_urls,
            _REPLY_TOPIC,
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
