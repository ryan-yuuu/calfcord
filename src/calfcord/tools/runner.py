"""CLI entry point for the ``calfkit-tools`` deployment.

Hosts every :class:`ToolNodeDef` registered in :data:`TOOL_REGISTRY` on a
single calfkit :class:`Worker`. Standalone process — separate from the
bridge and the agent runner — so the tool lifecycle is decoupled from
both, matching calfkit's tool-as-deployment model.

This deployment intentionally has no read access to ``agents/*.md``.
Agent identities (display name, avatar, description, tools) arrive at
the tool body via the phonebook the bridge places in ``deps`` on every
invocation.

The runner is **resource-light by design**: it connects the process-wide
:class:`calfkit.client.Client` (with a private reply topic distinct from the
bridge's ``discord.outbox``, so target-agent replies route here and are NOT
re-projected by the bridge's outbox poster) and exposes it as the worker-scoped
``a2a_client`` resource. Any *tool-specific* live resource is owned by the tool
itself via a node-scoped ``@resource`` bracket that calfkit builds only when
that tool is hosted — notably ``private_chat`` opens its own Discord connection
and enforces ``DISCORD_GUILD_ID`` there. So a host serving only non-A2A tools
(e.g. ``terminal``/``read_file``) needs no Discord credentials.

Run::

    uv run calfkit-tools
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from calfkit.client import Client
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfcord._provisioning import PROVISIONING
from calfcord._worker_runtime import run_worker_until_signal
from calfcord.tools import TOOL_REGISTRY
from calfcord.tools.private_chat import _RES_CLIENT as _A2A_CLIENT_RESOURCE

logger = logging.getLogger(__name__)

_REPLY_TOPIC = "calfkit.tools.reply"
"""Named reply topic for the tools client. Must differ from the bridge's
``discord.outbox`` so target-agent ReturnCalls route here, not to the
bridge's outbox consumer (which would project them to Discord twice)."""

_WORKSPACE_ENV = "CALFCORD_WORKSPACE_DIR"
_TERMINAL_CWD_ENV = "TERMINAL_CWD"
_DEFAULT_WORKSPACE = Path("state") / "workspace"


def _configure_tool_workspace() -> Path:
    """Point the vendored hermes terminal backend at the shared workspace.

    The hermes tools start each agent session's shell in ``TERMINAL_CWD``
    (falling back to the process cwd). Setting it to the calfcord workspace
    root gives every agent a consistent, writable base directory while the
    per-session ``task_id`` keying keeps one agent's shell state out of
    another's. The workspace-relative memory layout (``memory/<agent_id>/``,
    see :mod:`calfcord.agents.memory`) resolves against this root.

    An operator-set ``TERMINAL_CWD`` wins and is left untouched. Otherwise
    the root comes from ``CALFCORD_WORKSPACE_DIR`` (default
    ``<cwd>/state/workspace``) and is created on demand so a fresh checkout
    doesn't error before any tool has written to it.

    Returns:
        The resolved workspace root (the value ``TERMINAL_CWD`` now carries).
    """
    explicit = os.environ.get(_TERMINAL_CWD_ENV, "").strip()
    if explicit:
        return Path(explicit)

    raw = os.environ.get(_WORKSPACE_ENV)
    root = (
        Path(raw).expanduser().resolve()
        if raw
        else (Path.cwd() / _DEFAULT_WORKSPACE).resolve()
    )
    root.mkdir(parents=True, exist_ok=True)
    os.environ[_TERMINAL_CWD_ENV] = str(root)
    logger.info(
        "tools workspace root=%s (TERMINAL_CWD) from_env=%s",
        root,
        _WORKSPACE_ENV in os.environ,
    )
    return root

# The worker-resource key under which the process-wide ``Client`` is exposed to
# A2A tool bodies is imported from its owner/consumer (private_chat) so producer
# and consumer cannot drift — the same single-source-of-truth posture as the
# cross-process topic literals in ``calfcord.topics``. The runner already
# imports private_chat (it is part of the composed TOOL_REGISTRY surface), so
# this adds no new coupling across the (same-process) tools deployment.


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="calfkit-tools",
        description="Run the calfkit tools process (private_chat etc.).",
    )
    return parser.parse_args(argv)


def _resolve_tool_nodes(registry: dict[str, Any]) -> list[Any]:
    """Validate the tool registry has at least one tool and return its values.

    Extracted from ``_amain`` so the empty-registry guard can be tested
    without standing up Discord/Kafka. The guard prevents the worker from
    starting in an inert state where it subscribes to no topics — a
    failure mode that would be very confusing in production logs.

    Empty-registry is most commonly caused by a typo in
    ``CALFCORD_TOOLS_INCLUDE`` (per-tool images), so the SystemExit
    message includes the env var value to short-circuit the operator's
    "why is my registry empty" hunt. A complementary WARNING fires at
    composition time naming the specific typo'd entries (see
    :func:`calfcord.tools.deploy_filters.apply_deploy_filters`).
    """
    nodes = list(registry.values())
    if not nodes:
        include_filter = os.environ.get("CALFCORD_TOOLS_INCLUDE") or "<unset>"
        raise SystemExit(
            "TOOL_REGISTRY is empty; nothing to host "
            f"(CALFCORD_TOOLS_INCLUDE={include_filter})"
        )
    return nodes


async def _run_worker(worker: Worker) -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Delegates to the shared :func:`calfcord._worker_runtime.run_worker_until_signal`
    so the shutdown contract (signal-driven drain plus the
    "clean return without a signal is a crash" supervisor invariant) is
    defined in exactly one place across runners. Kept as a thin local
    wrapper because existing tests reference ``_run_worker`` by name.
    """
    await run_worker_until_signal(worker, drain_label="tools worker")


async def _amain() -> None:
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async with Client.connect(
        server_urls, reply_topic=_REPLY_TOPIC, provisioning=PROVISIONING
    ) as client:
        # No manual provisioning: this runner uses the managed Worker (started
        # via _run_worker below), whose _on_startup hook + the connect-time
        # pre-start hook auto-provision the worker's tool-node topics AND the
        # client reply topic at broker start. Tools only ``execute`` while
        # consuming a message — which can only happen after ``Worker.start()`` has
        # started the broker — so no eager start is needed for the dispatcher.
        tool_nodes = _resolve_tool_nodes(TOOL_REGISTRY)

        worker = Worker(client, tool_nodes)
        # Expose the process-wide client as a worker-scoped resource so A2A tool
        # bodies (private_chat) reach it via ``ctx.resources`` — calfkit merges
        # worker resources under each node's own. The client's lifecycle is owned
        # by the ``async with`` above; this only publishes the live reference.
        # Any Discord connection a tool needs is built by that tool's own
        # node-scoped ``@resource`` bracket, so nothing Discord-related is
        # constructed here.
        worker.resources[_A2A_CLIENT_RESOURCE] = client
        logger.info(
            "starting calfkit-tools worker tools=%s broker=%s reply_topic=%s include_filter=%s",
            sorted(TOOL_REGISTRY),
            server_urls,
            _REPLY_TOPIC,
            os.environ.get("CALFCORD_TOOLS_INCLUDE") or "<unset>",
        )
        await _run_worker(worker)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    _parse_args()
    # Pin the hermes terminal/file tools to the shared workspace before the
    # worker starts handling calls (it reads TERMINAL_CWD per call).
    _configure_tool_workspace()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("calfkit-tools shutting down")


if __name__ == "__main__":
    main()
