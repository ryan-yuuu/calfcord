"""CLI entry point for running calfkit agents.

Two modes, selected by the positional ``agent`` argument:

* ``calfkit-agent <name>`` — run one agent in its own process. One
  :class:`calfkit.Worker` with a single :class:`Agent` node, reachable
  **by name** on its automatic private input topic.
* ``calfkit-agent`` (no argument) — run *every* agent in ``agents/*.md`` on
  a single shared :class:`calfkit.Worker` in one process. Each agent is a
  separate calfkit node with its own Kafka consumer group (defaulting to
  the agent's name) so co-tenant nodes do not contend for partitions.

The all-agents mode trades crash isolation for resource simplicity: one
Discord webhook client, one Kafka connection, one process to supervise.
Concurrent LLM calls across agents are not a problem — they're awaitable
network I/O and yield on the event loop. The real tradeoff is *failure
domain*: an unhandled exception in any agent's handler tears down the
whole Worker. For a small, well-tested fleet that's acceptable; if one
agent is materially less stable than the rest, run it as its own
process via ``calfkit-agent <name>``.

Agents are **name-addressed** (calfkit ADR-0017): there is no per-agent
channel state to seed and no addressing gate. The bridge reaches each agent
directly by name, and the managed Worker auto-provisions each node's private
input + return topics at broker start. Agents advertise their ``AgentCard``
on the native mesh automatically, so the runner wires no presence/departure
lifecycle hooks.

Run::

    uv run calfkit-agent              # all agents in one process
    uv run calfkit-agent <agent_name> # one agent in one process
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from calfkit.client import Client
from calfkit.nodes import Agent
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfcord._provisioning import PROVISIONING
from calfcord._worker_runtime import run_worker_until_signal
from calfcord.agents.definition import AgentDefinition
from calfcord.agents.factory import AgentFactory, resolve_provider
from calfcord.agents.loader import load_agent_targets, load_agents_dir
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

_AGENTS_DIR_ENV = "CALFKIT_AGENTS_DIR"
_AGENTS_DIR_DEFAULT = "agents"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="calfkit-agent",
        description="Run one or all calfkit agents.",
    )
    parser.add_argument(
        "agent",
        nargs="?",
        default=None,
        help=(
            "Name of the agent to run (matches the agents/<name>.md filename "
            "stem). If omitted, every agent in the agents directory is "
            "started on a single shared Worker in this process."
        ),
    )
    parser.add_argument(
        "-t",
        "--target",
        action="append",
        default=None,
        metavar="PATH",
        dest="targets",
        help=(
            "Path to an agent .md file OR a directory of agent .md files. "
            "Repeatable: pass -t/--target several times to deploy multiple "
            "files and/or directories together. A directory is scanned with the "
            "usual skip rules (dotfiles and *.template.md ignored); an "
            "explicitly named file is loaded literally. Mutually exclusive with "
            "the positional agent name; when given, overrides CALFKIT_AGENTS_DIR."
        ),
    )
    args = parser.parse_args(argv)
    if args.targets and args.agent is not None:
        parser.error(
            "argument agent: not allowed with --target — pass either a single "
            "agent name (resolved within CALFKIT_AGENTS_DIR) or one-or-more "
            "--target paths, not both"
        )
    return args


def _resolve_definition(agent_name: str, agents_dir: Path) -> AgentDefinition:
    definitions = load_agents_dir(agents_dir)
    for d in definitions:
        if d.agent_id == agent_name:
            return d
    known = ", ".join(sorted(d.agent_id for d in definitions))
    raise SystemExit(f"agent {agent_name!r} not found in {agents_dir}. Known: {known or '<none>'}")


def _resolve_definitions(
    agent_name: str | None,
    agents_dir: Path,
    *,
    targets: list[Path] | None = None,
) -> list[AgentDefinition]:
    """Resolve which agent definitions to run.

    Three modes, selected by precedence (``targets`` wins, then ``agent_name``,
    then the directory scan):

    Targets mode (``targets`` non-empty): one definition per agent resolved
    from the explicit ``--target`` file/directory paths via
    :func:`load_agent_targets` (which de-duplicates by ``agent_id``).

    Single-agent mode (``agent_name`` set, no targets): a list of length 1.

    All-agents mode (no targets, ``agent_name`` is ``None``): one definition
    per ``agents/*.md`` file.

    Name-addressing removed per-agent channel bootstrap, so resolution is pure
    ``.md`` loading with no I/O beyond the file read. Any load failure (missing
    path, neither file nor directory, malformed ``.md``, duplicate ``agent_id``,
    unknown name, empty result) raises :class:`SystemExit` so the CLI exits
    cleanly on stderr without a traceback.
    """
    if targets:
        try:
            definitions = load_agent_targets(targets)
        except (FileNotFoundError, NotADirectoryError, ValueError) as e:
            raise SystemExit(f"failed to load --target paths: {e}") from e
        if not definitions:
            joined = ", ".join(str(t) for t in targets)
            raise SystemExit(f"no agent definitions found in --target paths: {joined}")
        return definitions

    if agent_name is not None:
        return [_resolve_definition(agent_name, agents_dir)]

    try:
        definitions = load_agents_dir(agents_dir)
    except (FileNotFoundError, NotADirectoryError, ValueError) as e:
        raise SystemExit(f"failed to load {agents_dir}: {e}") from e
    if not definitions:
        raise SystemExit(f"no agent definitions found in {agents_dir}")
    return definitions


def _build_node_or_exit(factory: AgentFactory, definition: AgentDefinition) -> Agent:
    """Wrap :meth:`AgentFactory.build_node` so any failure exits cleanly.

    CLI build: any failure is fatal. The bare-Exception catch is deliberate —
    pydantic_ai raises ``UserError(RuntimeError)`` for missing API keys and the
    model-client constructors can raise other types we don't want to enumerate.
    Convert them all to a clean stderr :class:`SystemExit` so operators don't
    see a traceback.
    """
    try:
        return factory.build_node(definition)
    except Exception as e:
        raise SystemExit(f"agent {definition.agent_id!r} failed to construct: {e}") from e


async def _run_worker(worker: Worker) -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Delegates to the shared :func:`calfcord._worker_runtime.run_worker_until_signal`
    so the shutdown contract (signal-driven drain plus the "clean return
    without a signal is a crash" supervisor invariant) is defined in exactly
    one place across runners — mirroring :func:`calfcord.tools.runner._run_worker`.
    Kept as a thin local wrapper because the runner unit tests reference
    ``_run_worker`` by name.
    """
    await run_worker_until_signal(worker, drain_label="agents worker")


async def _prewarm_codex_if_needed(definitions: list[AgentDefinition]) -> None:
    """If any definition resolves to the openai-codex provider, prewarm the cache.

    Uses :func:`resolve_provider` rather than reading ``definition.provider``
    directly so the ``CALFKIT_AGENT_DEFAULT_PROVIDER`` env-var fallback is
    honoured — a bare attribute access would be ``None`` for any agent that
    omits ``provider:`` from its frontmatter, even if the operator selected
    openai-codex globally via env var. Missing that case would skip prewarm
    and crash mid-factory with an opaque ``RuntimeError`` instead of the
    actionable :class:`SystemExit` raised here.

    Raises:
        SystemExit: if the upstream prompt fetch fails AND no cache exists.
            Includes a hint pointing the operator at
            ``calfkit-auth codex refresh-prompts``.
    """
    needs_codex = any(resolve_provider(d) == "openai-codex" for d in definitions)
    if not needs_codex:
        return
    # Lazy import: keeps authlib + openhands-sdk auth machinery out of the
    # import graph for deployments that don't use Codex subscription.
    from calfcord.providers.codex import (
        CodexPromptsUnavailableError,
        prewarm_codex_prompts,
    )

    try:
        await prewarm_codex_prompts()
    except CodexPromptsUnavailableError as exc:
        raise SystemExit(
            f"openai-codex agents declared but upstream Codex prompts "
            f"are unavailable: {exc}. Check internet connectivity, or "
            f"run once: uv run calfkit-auth codex refresh-prompts"
        ) from exc


async def _amain(args: argparse.Namespace) -> None:
    """Build and run the agent(s). Pure-ish: callers configure logging+env."""
    agents_dir = Path(os.getenv(_AGENTS_DIR_ENV, _AGENTS_DIR_DEFAULT))

    targets = [Path(t) for t in args.targets] if args.targets else None
    definitions = _resolve_definitions(args.agent, agents_dir, targets=targets)
    await _prewarm_codex_if_needed(definitions)

    settings = DiscordSettings()  # type: ignore[call-arg]
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async with (
        DiscordPersonaSender(settings) as persona_sender,
        Client.connect(server_urls, provisioning=PROVISIONING) as calfkit_client,
    ):
        factory = AgentFactory(persona_sender, calfkit_client)
        nodes = [_build_node_or_exit(factory, definition) for definition in definitions]

        # Managed lifecycle: ``Worker.start()`` (via _run_worker) auto-registers
        # handlers and auto-provisions each agent's node topics — its private
        # input topic ``agent.{name}.private.input`` (name-addressing) and its
        # private return topic — at broker start, and the connect-hook
        # auto-provisions the client reply topic. Name-addressed agents carry no
        # channel subscriptions and advertise their AgentCard automatically, so
        # there are no domain lifecycle hooks to wire here.
        worker = Worker(calfkit_client, nodes)

        logger.info(
            "starting worker with %d agent(s): %s",
            len(nodes),
            ", ".join(n.node_id for n in nodes),
        )
        await _run_worker(worker)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    args = _parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        logger.info("agent shutting down")


if __name__ == "__main__":
    main()
