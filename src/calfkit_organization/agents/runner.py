"""CLI entry point for running one agent process.

Wraps:
    - parsing argv to select an agent by name
    - loading its :class:`AgentDefinition` from ``agents/<name>.md``
    - loading or bootstrapping its :class:`AgentRuntimeState` from
      ``state/agents/<name>.json``
    - constructing :class:`DiscordPersonaSender`, calfkit :class:`Client`,
      and :class:`AgentFactory`
    - calling ``factory.build(definition, state, store)`` to produce a
      :class:`Worker`
    - running the worker under SIGINT/SIGTERM shutdown

Bootstrap: on first run, ``state/agents/<name>.json`` does not exist. The
runner reads :envvar:`CALFKIT_AGENT_<UPPER_NAME>_BOOTSTRAP_CHANNELS` (comma-
separated channel IDs), seeds the state file, and continues. If the env
var is unset on a fresh agent the runner exits with a clear pointer to it.

Once the state file exists the bootstrap env var is ignored with a WARNING
log; the persisted state is the canonical source of subscriptions.

**Caveat:** the bootstrap env var is a *one-shot* seed. If the state file
is later deleted (intentionally or accidentally), a still-set bootstrap
env var will silently re-seed on next boot — possibly with stale channel
IDs. Clear the env var (or remove it from ``.env``) after first successful
boot to prevent accidental re-seeding.

Run::

    uv run calfkit-agent <agent_name>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from calfkit.client import Client
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
        description="Run one calfkit agent process.",
    )
    parser.add_argument(
        "agent",
        help="Name of the agent to run (matches the agents/<name>.md filename stem).",
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
        await store.save(state)
        logger.warning(
            "bootstrapped state at %s with channels=%s from %s — %s",
            store.path,
            channels,
            source,
            cleanup_hint,
        )
        return state

    if raw_env:
        logger.warning(
            "ignoring %s — state file already exists at %s",
            env_var,
            store.path,
        )
    return state


async def _amain(args: argparse.Namespace) -> None:
    """Build and run the agent. Pure-ish: callers configure logging+env."""
    agents_dir = Path(os.getenv(_AGENTS_DIR_ENV, _AGENTS_DIR_DEFAULT))
    state_dir = Path(os.getenv(_STATE_DIR_ENV, _STATE_DIR_DEFAULT))

    definition = _resolve_definition(args.agent, agents_dir)
    store = AgentStateStore(state_dir / f"{definition.agent_id}.json")
    state = await _load_or_bootstrap_state(store, definition.agent_id)

    settings = DiscordSettings()  # type: ignore[call-arg]
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async with DiscordPersonaSender(settings) as persona_sender:
        async with Client.connect(server_urls) as calfkit_client:
            factory = AgentFactory(persona_sender, calfkit_client)
            worker = factory.build(definition, state, store)

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)

            worker_task = asyncio.create_task(worker.run())
            stop_task = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait(
                    {worker_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (worker_task, stop_task):
                    if not t.done():
                        t.cancel()
                # Drain cancellations so finally/__aexit__ blocks run before
                # the surrounding async-context managers tear down resources.
                await asyncio.gather(worker_task, stop_task, return_exceptions=True)


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
