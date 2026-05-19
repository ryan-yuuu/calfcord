"""CLI entry point for running calfkit agents.

Two modes, selected by the positional ``agent`` argument:

* ``calfkit-agent <name>`` — run one agent in its own process. One
  :class:`calfkit.Worker` with a single :class:`Agent` node subscribed to
  that agent's configured channels.
* ``calfkit-agent`` (no argument) — run *every* agent in ``agents/*.md`` on
  a single shared :class:`calfkit.Worker` in one process. Each agent is a
  separate calfkit node with its own Kafka consumer group (defaulting to
  the agent's ``node_id``) so co-tenant nodes do not contend for
  partitions.

The all-agents mode trades crash isolation for resource simplicity: one
Discord webhook client, one Kafka connection, one process to supervise.
Concurrent LLM calls across agents are not a problem — they're awaitable
network I/O and yield on the event loop. The real tradeoff is *failure
domain*: an unhandled exception in any agent's handler tears down the
whole Worker. For a small, well-tested fleet that's acceptable; if one
agent is materially less stable than the rest, run it as its own
process via ``calfkit-agent <name>``.

Both modes share the same per-agent bootstrap logic:

* Each agent has its own ``state/agents/<name>.json`` file (atomic writes,
  per-process lock).
* On first boot, the state file is seeded from
  ``CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS`` (comma-separated
  channel IDs); ``DISCORD_DEFAULT_CHANNEL_ID`` is the shared dev fallback.
* Once the state file exists the bootstrap env var is ignored (with a
  WARNING log) — the persisted state is canonical.

All-agents-mode bootstrap failures are *aggregated*: rather than exiting
on the first agent that's missing its bootstrap env var, the runner
collects every per-agent failure and exits with a single multi-line
message so operators see every misconfiguration in one pass. Single-
agent mode keeps the pre-all-mode behaviour: a bootstrap failure exits
with the bare per-agent message, fail-fast.

**Caveat:** the bootstrap env var is a *one-shot* seed. If the state file
is later deleted (intentionally or accidentally), a still-set bootstrap
env var will silently re-seed on next boot — possibly with stale channel
IDs. Clear the env var (or remove it from ``.env``) after first successful
boot to prevent accidental re-seeding.

**Co-tenancy gotcha:** the hand-coded ``agents/echo.py`` runtime registers
``group_id=echo`` for the same channels the factory-built ``echo`` node
would use in all-agents mode. Do *not* run ``python agents/echo.py`` and
``calfkit-agent`` (all-mode) at the same time — they would contend for
the same Kafka partitions. Pick one runtime model per environment.

Run::

    uv run calfkit-agent              # all agents in one process
    uv run calfkit-agent <agent_name> # one agent in one process
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from calfkit.client import Client
from calfkit.worker import Worker
from dotenv import load_dotenv

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.agents.loader import load_agents_dir
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore
from calfkit_organization.discord.persona import DiscordPersonaSender
from calfkit_organization.discord.settings import DiscordSettings

logger = logging.getLogger(__name__)

_AGENTS_DIR_ENV = "CALFKIT_AGENTS_DIR"
_STATE_DIR_ENV = "CALFKIT_STATE_DIR"
_AGENTS_DIR_DEFAULT = "agents"
_STATE_DIR_DEFAULT = "state/agents"
_DEFAULT_CHANNEL_ID_ENV = "DISCORD_DEFAULT_CHANNEL_ID"


class BootstrapError(RuntimeError):
    """A recoverable startup failure that produces a clean CLI exit.

    Raised when the runner cannot locate an agent definition, cannot load
    or seed its state file, or receives malformed bootstrap input. The
    top-level :func:`main` converts these into ``SystemExit(message)`` so
    the user sees the message on stderr without a traceback.
    """


def bootstrap_env_var(agent_id: str) -> str:
    """Return the env var name an agent uses for first-run channel seeding.

    Hyphens in the agent_id become underscores so the result is a valid
    POSIX env identifier.
    """
    return f"CALFKIT_AGENT_{agent_id.upper().replace('-', '_')}_BOOTSTRAP_CHANNELS"


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
    return parser.parse_args(argv)


def _resolve_definition(agent_name: str, agents_dir: Path) -> AgentDefinition:
    definitions = load_agents_dir(agents_dir)
    for d in definitions:
        if d.agent_id == agent_name:
            return d
    known = ", ".join(sorted(d.agent_id for d in definitions))
    raise BootstrapError(
        f"agent {agent_name!r} not found in {agents_dir}. Known: {known or '<none>'}"
    )


def _parse_channel_ids(raw: str, *, env_var: str) -> list[int]:
    """Parse a comma-separated string of channel IDs into a list of ints.

    Raises :class:`BootstrapError` with the offending token in the message
    if any non-blank entry is not a valid integer.
    """
    result: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            result.append(int(token))
        except ValueError as e:
            raise BootstrapError(
                f"{env_var} contains invalid channel id {token!r}; must be an integer"
            ) from e
    return result


async def _load_or_bootstrap_state(
    store: AgentStateStore,
    agent_id: str,
) -> AgentRuntimeState:
    """Load the state file or bootstrap it from an env var.

    Bootstrap source priority when the state file is absent:

        1. ``CALFKIT_AGENT_<NAME>_BOOTSTRAP_CHANNELS`` — the per-agent
           explicit-seed env var. Recommended for production where each
           agent's channels are intentional.
        2. ``DISCORD_DEFAULT_CHANNEL_ID`` — the shared example/dev channel
           env var (also used by ``examples/`` scripts and ``agents/echo.py``).
           Convenient for local smoke tests where a single channel is wired
           up for every agent.

    If both are unset and the state file does not exist, raises
    :class:`BootstrapError` with a hint pointing to either var.

    Any other I/O or parse failure reading the state file (permission
    denied, malformed JSON, schema mismatch) is also converted to
    :class:`BootstrapError` so the runner's aggregation loop can collect
    it alongside other per-agent failures rather than letting a single
    corrupt state file blow up the whole startup with a raw traceback.
    Likewise for write failures during bootstrap (disk full, parent dir
    not writable).
    """
    env_var = bootstrap_env_var(agent_id)
    raw_env = os.getenv(env_var)
    raw_default = os.getenv(_DEFAULT_CHANNEL_ID_ENV)

    try:
        state = await store.load()
    except FileNotFoundError as e:
        if raw_env:
            channels = _parse_channel_ids(raw_env, env_var=env_var)
            if not channels:
                raise BootstrapError(f"{env_var} is set but parsed to zero channels") from e
            source = env_var
            cleanup_hint = (
                f"clear {env_var} after first boot to prevent accidental re-seed "
                f"if the state file is later deleted"
            )
        elif raw_default:
            channels = _parse_channel_ids(raw_default, env_var=_DEFAULT_CHANNEL_ID_ENV)
            if not channels:
                raise BootstrapError(
                    f"{_DEFAULT_CHANNEL_ID_ENV} is set but parsed to zero channels"
                ) from e
            source = _DEFAULT_CHANNEL_ID_ENV
            cleanup_hint = (
                f"set {env_var}=<channel_ids> for explicit per-agent bootstrap; "
                f"{_DEFAULT_CHANNEL_ID_ENV} is a shared dev fallback"
            )
        else:
            raise BootstrapError(
                f"no state file at {store.path}; set {env_var}=<comma,separated,channel,ids> "
                f"or {_DEFAULT_CHANNEL_ID_ENV} to bootstrap."
            ) from e

        state = AgentRuntimeState(channels=channels)
        try:
            await store.save(state)
        except OSError as save_err:
            raise BootstrapError(
                f"failed to write bootstrap state to {store.path}: {save_err}"
            ) from save_err
        logger.warning(
            "bootstrapped state at %s with channels=%s from %s — %s",
            store.path,
            channels,
            source,
            cleanup_hint,
        )
        return state
    except (OSError, ValueError) as e:
        # FileNotFoundError is a subclass of OSError but is caught above.
        # This branch handles PermissionError, json.JSONDecodeError (a
        # ValueError subclass), pydantic.ValidationError (also a
        # ValueError subclass in pydantic v2), and any other I/O error
        # raised by AgentStateStore._read. Convert to BootstrapError so
        # the per-agent aggregation in _resolve_agent_specs can collect
        # it instead of letting a raw traceback escape main().
        raise BootstrapError(
            f"failed to read state file {store.path}: {e}"
        ) from e

    if raw_env:
        logger.warning(
            "ignoring %s — state file already exists at %s",
            env_var,
            store.path,
        )
    return state


AgentSpec = tuple[AgentDefinition, AgentRuntimeState, AgentStateStore]
"""One agent's runtime triple: the parsed definition, its (loaded or freshly
bootstrapped) state, and the store that owns its state file. A list of
these is the unified shape both runner modes consume."""


async def _resolve_agent_specs(
    agent_name: str | None,
    agents_dir: Path,
    state_dir: Path,
) -> list[AgentSpec]:
    """Resolve which agents to run and bootstrap each one's state.

    Single-agent mode (``agent_name`` set): returns a list of length 1.
    Bootstrap failure surfaces as a raised :class:`BootstrapError`
    immediately (fail-fast) with the underlying per-agent message
    unwrapped — operators invoking ``calfkit-agent <name>`` see the same
    actionable error they did before the all-mode change.

    All-agents mode (``agent_name`` is ``None``): returns one entry per
    ``agents/*.md`` file. Per-agent bootstrap failures are **aggregated**
    so the caller sees every misconfigured agent in a single error
    message rather than re-running N times. ``agents_dir`` errors
    (missing, not-a-directory, malformed ``.md``) are converted to
    :class:`BootstrapError` for the same clean-exit reason.

    Raises:
        BootstrapError: if a named agent is unknown, the agents directory
            cannot be loaded, or one-or-more agents fail to bootstrap.
    """
    if agent_name is not None:
        definitions = [_resolve_definition(agent_name, agents_dir)]
    else:
        try:
            definitions = load_agents_dir(agents_dir)
        except (FileNotFoundError, NotADirectoryError, ValueError) as e:
            raise BootstrapError(f"failed to load {agents_dir}: {e}") from e
        if not definitions:
            raise BootstrapError(f"no agent definitions found in {agents_dir}")

    specs: list[AgentSpec] = []
    failures: list[tuple[str, str]] = []
    for definition in definitions:
        store = AgentStateStore(state_dir / f"{definition.agent_id}.json")
        try:
            state = await _load_or_bootstrap_state(store, definition.agent_id)
        except BootstrapError as e:
            if agent_name is not None:
                # Single-mode: let the per-agent message propagate
                # unwrapped so operators see the same actionable error
                # they did before all-mode existed.
                raise
            failures.append((definition.agent_id, str(e)))
            continue
        specs.append((definition, state, store))

    if failures:
        header = f"bootstrap failed for {len(failures)} agent(s):"
        body = "\n".join(f"  - {agent_id}: {msg}" for agent_id, msg in failures)
        raise BootstrapError(f"{header}\n{body}")

    return specs


def _build_node_or_bootstrap_error(
    factory: AgentFactory,
    definition: AgentDefinition,
    state: AgentRuntimeState,
    store: AgentStateStore,
):
    """Wrap :meth:`AgentFactory.build_node` so any failure surfaces as a
    :class:`BootstrapError`.

    CLI bootstrap: any build failure is fatal. The bare-Exception catch is
    deliberate — pydantic_ai raises ``UserError(RuntimeError)`` for missing
    API keys and the model-client constructors can raise other types we
    don't want to enumerate. Convert them all to a clean stderr exit so
    operators don't see a traceback.
    """
    try:
        return factory.build_node(definition, state, store)
    except Exception as e:
        raise BootstrapError(
            f"agent {definition.agent_id!r} failed to construct: {e}"
        ) from e


async def _run_worker(worker: Worker, *, num_agents: int) -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Calfkit's :meth:`Worker.run` is a single awaitable regardless of node
    count, so the same shutdown pattern works for both single-agent and
    multi-agent workers. The drain log line names the node count so a
    Ctrl-C in all-mode doesn't look hung while N consumer groups close.

    If ``worker.run`` itself raises during runtime (e.g., a Kafka broker
    drop or an unhandled handler exception), the exception is logged and
    re-raised so the process exits non-zero. Without this, the trailing
    ``gather(..., return_exceptions=True)`` would silently consume the
    exception and the process would exit 0 — supervisors configured for
    ``Restart=on-failure`` would not restart, and operators would see a
    "phantom shutdown" with no diagnostic in logs.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    worker_task = asyncio.create_task(worker.run())
    stop_task = asyncio.create_task(stop.wait())
    worker_exc: BaseException | None = None
    try:
        await asyncio.wait(
            {worker_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task.done() and not stop_task.done():
            worker_exc = worker_task.exception()
            if worker_exc is not None:
                logger.error(
                    "worker crashed during runtime; exiting non-zero",
                    exc_info=worker_exc,
                )
            else:
                logger.warning("worker.run() returned without an exception; exiting")
        else:
            logger.info("shutdown signal received, draining %d agent(s)", num_agents)
    finally:
        for t in (worker_task, stop_task):
            if not t.done():
                t.cancel()
        # Drain cancellations so finally/__aexit__ blocks run before
        # the surrounding async-context managers tear down resources.
        await asyncio.gather(worker_task, stop_task, return_exceptions=True)

    if worker_exc is not None:
        raise worker_exc


async def _amain(args: argparse.Namespace) -> None:
    """Build and run the agent(s). Pure-ish: callers configure logging+env."""
    agents_dir = Path(os.getenv(_AGENTS_DIR_ENV, _AGENTS_DIR_DEFAULT))
    state_dir = Path(os.getenv(_STATE_DIR_ENV, _STATE_DIR_DEFAULT))

    specs = await _resolve_agent_specs(args.agent, agents_dir, state_dir)

    settings = DiscordSettings()  # type: ignore[call-arg]
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async with DiscordPersonaSender(settings) as persona_sender:
        async with Client.connect(server_urls) as calfkit_client:
            factory = AgentFactory(persona_sender, calfkit_client)
            nodes = [
                _build_node_or_bootstrap_error(factory, definition, state, store)
                for definition, state, store in specs
            ]
            worker = Worker(calfkit_client, nodes)
            logger.info(
                "starting worker with %d agent(s): %s",
                len(nodes),
                ", ".join(n.node_id for n in nodes),
            )
            await _run_worker(worker, num_agents=len(nodes))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    args = _parse_args()
    try:
        asyncio.run(_amain(args))
    except BootstrapError as e:
        raise SystemExit(str(e)) from None
    except KeyboardInterrupt:
        logger.info("agent shutting down")


if __name__ == "__main__":
    main()
