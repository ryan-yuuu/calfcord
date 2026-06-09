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
from pathlib import Path
from typing import TYPE_CHECKING

from calfkit.client import Client
from calfkit.worker import Worker
from calfkit.worker.lifecycle import LifecycleContext, ServingContext
from dotenv import load_dotenv

from calfcord._provisioning import PROVISIONING, agent_infra_topics
from calfcord._worker_runtime import run_worker_until_signal
from calfcord.agents.definition import AgentDefinition
from calfcord.agents.factory import AgentFactory, resolve_provider
from calfcord.agents.loader import load_agent_targets, load_agents_dir
from calfcord.agents.state import AgentRuntimeState, AgentStateStore

# NOTE: ``calfcord.control_plane.*`` modules are NOT imported
# at top level. ``control_plane.schema`` imports
# ``calfcord.agents.definition``, which triggers
# ``agents/__init__.py`` -- which itself imports ``bootstrap_env_var``
# from this very module. If a control-plane import here ran during
# ``agents/__init__.py``'s eager load, ``control_plane.builders`` would
# re-enter ``control_plane.schema`` mid-initialization and raise
# ``ImportError: cannot import name 'AgentStateEvent'``. Bridge code
# avoids this by accident (it loads ``agents.definition`` before
# ``control_plane.publish``, fully completing ``agents/__init__.py``
# first); the agent CLI path and test isolation both hit the cycle. We
# defer all control-plane imports to the function bodies that need
# them. ``TYPE_CHECKING`` covers the one type annotation that wants
# the symbol at parse time.
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.discord.settings import DiscordSettings

if TYPE_CHECKING:
    from calfcord.control_plane.definition_ref import AgentDefinitionRef

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
    parser.add_argument(
        "-t", "--target",
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
           env var. Convenient for local smoke tests where a single channel
           is wired up for every agent.

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
    *,
    targets: list[Path] | None = None,
) -> list[AgentSpec]:
    """Resolve which agents to run and bootstrap each one's state.

    Three modes, selected by precedence (``targets`` wins, then
    ``agent_name``, then the directory scan):

    Targets mode (``targets`` non-empty): returns one entry per agent
    resolved from the explicit ``--target`` file/directory paths via
    :func:`load_agent_targets` (which de-duplicates by ``agent_id``).
    Like all-agents mode, per-agent bootstrap failures are **aggregated**
    rather than fail-fast. Target-resolution errors (missing path, neither
    file nor directory, malformed ``.md``, duplicate ``agent_id``) are
    converted to :class:`BootstrapError` for a clean exit.

    Single-agent mode (``agent_name`` set, no targets): returns a list of
    length 1. Bootstrap failure surfaces as a raised :class:`BootstrapError`
    immediately (fail-fast) with the underlying per-agent message
    unwrapped — operators invoking ``calfkit-agent <name>`` see the same
    actionable error they did before the all-mode change.

    All-agents mode (no targets, ``agent_name`` is ``None``): returns one
    entry per ``agents/*.md`` file. Per-agent bootstrap failures are
    **aggregated** so the caller sees every misconfigured agent in a single
    error message rather than re-running N times. ``agents_dir`` errors
    (missing, not-a-directory, malformed ``.md``) are converted to
    :class:`BootstrapError` for the same clean-exit reason.

    Raises:
        BootstrapError: if a named agent is unknown, the agents directory
            or ``--target`` paths cannot be loaded, or one-or-more agents
            fail to bootstrap.
    """
    if targets:
        try:
            definitions = load_agent_targets(targets)
        except (FileNotFoundError, NotADirectoryError, ValueError) as e:
            raise BootstrapError(f"failed to load --target paths: {e}") from e
        if not definitions:
            joined = ", ".join(str(t) for t in targets)
            raise BootstrapError(f"no agent definitions found in --target paths: {joined}")
        fail_fast = False
    elif agent_name is not None:
        definitions = [_resolve_definition(agent_name, agents_dir)]
        fail_fast = True
    else:
        try:
            definitions = load_agents_dir(agents_dir)
        except (FileNotFoundError, NotADirectoryError, ValueError) as e:
            raise BootstrapError(f"failed to load {agents_dir}: {e}") from e
        if not definitions:
            raise BootstrapError(f"no agent definitions found in {agents_dir}")
        fail_fast = False

    specs: list[AgentSpec] = []
    failures: list[tuple[str, str]] = []
    for definition in definitions:
        store = AgentStateStore(state_dir / f"{definition.agent_id}.json")
        try:
            state = await _load_or_bootstrap_state(store, definition.agent_id)
        except BootstrapError as e:
            if fail_fast:
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


async def _run_worker(worker: Worker) -> None:
    """Run ``worker`` until SIGINT/SIGTERM, then drain cleanly.

    Delegates to the shared :func:`calfcord._worker_runtime.run_worker_until_signal`
    so the shutdown contract (signal-driven drain plus the "clean return
    without a signal is a crash" supervisor invariant) is defined in exactly
    one place across runners — mirroring :func:`calfcord.tools.runner._run_worker`
    and :func:`calfcord.router.runner._run_worker`. Kept as a thin local
    wrapper because the runner unit tests reference ``_run_worker`` by name.

    The agents runner used to hand-decompose the worker's run loop so it could
    publish presence/departure events at precise lifecycle points. Those are
    now :func:`Worker.after_startup` / :func:`Worker.on_shutdown` hooks (see
    :func:`_register_lifecycle_hooks`), so the agents runner joins the shared
    managed-lifecycle path used by tools/router — the embedded
    ``Worker.start()`` surface driven by :func:`run_worker_until_signal`.
    """
    await run_worker_until_signal(worker, drain_label="agents worker")


def _register_lifecycle_hooks(
    worker: Worker,
    client: Client,
    definition_refs: list[AgentDefinitionRef],
) -> None:
    """Wire the agents runner's three lifecycle hooks onto ``worker``.

    Separates worker LIFECYCLE (connect/provision/serve/drain, owned by
    :func:`run_worker_until_signal`) from DOMAIN concerns (blind-spot topic
    declaration, presence, departure), each expressed as a small named
    single-purpose hook. The ordering each hook depends on is guaranteed by
    calfkit's lifecycle engine, not by hand-sequencing in a boot loop:

    * ``on_startup`` (resource phase, BEFORE ``broker.start()``) declares the
      blind-spot topics into the client's startup ensurer so they are created
      in calfkit's single pre-start provisioning pass, before the raw control
      sinks consume / the presence publish fires.
    * ``after_startup`` (broker up, producer live) announces each hosted
      agent's initial state — :func:`publish_state_event` raises
      ``IncorrectState`` unless the producer is connected, which is exactly
      why presence must run here and not earlier.
    * ``on_shutdown`` (broker still up, before drain) publishes a departure
      for each agent, best-effort and bounded, so peers/bridge see us leave
      before the broker disconnects.
    """
    # Deferred import: see the NOTE on control_plane imports near the top.
    from calfcord.control_plane.builders import build_state_event
    from calfcord.control_plane.publish import publish_state_event

    agent_ids = [ref.current.agent_id for ref in definition_refs]

    @worker.on_startup
    async def _declare_blind_spot_topics(ctx: LifecycleContext[Worker]) -> None:
        """Fold the agents' blind-spot topics into calfkit's pre-start pass.

        ``topics_for_nodes`` (auto-declared by the managed worker's
        ``_on_startup`` hook, run on ``Worker.start()``) covers the node topics
        + the framework return
        inboxes, and the connect-hook auto-provisions the client reply topic.
        The control-plane topics the agent touches RAW — ``agent.state``
        (boot-time presence publish), ``bridge.discovery`` (raw control-sink
        subscriber), and one ``agent.{id}.control.in`` per hosted agent — are
        invisible to ``topics_for_nodes``, so we declare them here. This hook
        runs before ``broker.start()``, so the ensurer's pre-start pass creates
        them before any raw subscriber consumes or the presence publish fires.
        """
        client._startup_ensurer.declare(agent_infra_topics(agent_ids))

    @worker.after_startup
    async def _announce_presence(ctx: ServingContext[Worker]) -> None:
        """Publish each hosted agent's startup state once the producer is live.

        One process hosts many agents, so this single hook iterates every ref.
        Subscribers are now consuming, so a peer agent already running sees this
        and adds us to its roster, and the bridge's state-consumer projects us
        into its registry for slash-command re-registration.
        """
        for ref in definition_refs:
            await publish_state_event(
                client, build_state_event(ref.current, cause="startup"),
            )
            logger.info("announced startup for agent=%s", ref.current.agent_id)

    @worker.on_shutdown
    async def _publish_departures(ctx: ServingContext[Worker]) -> None:
        """Best-effort departure publish for each agent, before broker.stop().

        Runs while the broker producer is still live (the ``serving`` shutdown
        bracket), so the last-gasp departures actually reach Kafka. Bounded and
        log-never-raise inside :func:`_publish_departures_best_effort`; calfkit's
        own ``on_shutdown`` teardown is additionally log-never-raise, so a stuck
        producer cannot block process teardown.
        """
        await _publish_departures_best_effort(client, definition_refs)


async def _publish_departures_best_effort(
    client: Client,
    definition_refs: list[AgentDefinitionRef],
    *,
    timeout: float = 2.0,
) -> None:
    """Publish AgentDepartureEvent for every agent in parallel, best-effort.

    Each publish is bounded by ``timeout`` seconds so a stuck Kafka producer
    can't block process shutdown indefinitely. Failures are logged at WARNING
    (timeout) or ERROR (other) and swallowed — the bridge will see a stale
    entry for that agent until it next restarts, which is the same failure
    mode as a hard crash (SIGKILL, OOM, etc.).

    Parallel via ``asyncio.gather`` so total shutdown delay is capped at
    ``timeout`` seconds regardless of agent count.
    """
    # Deferred import: see the NOTE on control_plane imports near the top.
    from calfcord.control_plane.publish import publish_departure

    async def _one(aid: str) -> None:
        try:
            await asyncio.wait_for(publish_departure(client, aid), timeout=timeout)
            logger.info("published departure for agent=%s", aid)
        except TimeoutError:
            logger.warning(
                "departure publish timed out for agent=%s; bridge will see stale entry",
                aid,
            )
        except Exception:
            logger.exception(
                "departure publish failed for agent=%s; bridge will see stale entry",
                aid,
            )

    await asyncio.gather(
        *(_one(ref.current.agent_id) for ref in definition_refs),
    )


async def _prewarm_codex_if_needed(
    specs: list[tuple[AgentDefinition, AgentRuntimeState, AgentStateStore]],
) -> None:
    """If any spec uses the openai-codex provider, prewarm the prompt cache.

    Uses :func:`resolve_provider` rather than reading ``spec[0].provider``
    directly so the ``CALFKIT_AGENT_DEFAULT_PROVIDER`` env-var fallback is
    honoured — a bare attribute access would be ``None`` for any agent that
    omits ``provider:`` from its frontmatter, even if the operator selected
    openai-codex globally via env var. Missing that case would skip prewarm
    and crash mid-factory with an opaque ``RuntimeError`` instead of the
    actionable :class:`BootstrapError` raised here.

    Raises:
        BootstrapError: if the upstream prompt fetch fails AND no cache
            exists. Includes a hint pointing the operator at
            ``calfkit-auth codex refresh-prompts``.
    """
    needs_codex = any(resolve_provider(spec[0]) == "openai-codex" for spec in specs)
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
        raise BootstrapError(
            f"openai-codex agents declared but upstream Codex prompts "
            f"are unavailable: {exc}. Check internet connectivity, or "
            f"run once: uv run calfkit-auth codex refresh-prompts"
        ) from exc


async def _amain(args: argparse.Namespace) -> None:
    """Build and run the agent(s). Pure-ish: callers configure logging+env."""
    # Deferred imports: see the NOTE on control_plane imports near the top.
    from calfcord.control_plane.definition_ref import AgentDefinitionRef
    from calfcord.control_plane.sink import register_control_sink

    agents_dir = Path(os.getenv(_AGENTS_DIR_ENV, _AGENTS_DIR_DEFAULT))
    state_dir = Path(os.getenv(_STATE_DIR_ENV, _STATE_DIR_DEFAULT))

    targets = [Path(t) for t in args.targets] if args.targets else None
    specs = await _resolve_agent_specs(args.agent, agents_dir, state_dir, targets=targets)
    await _prewarm_codex_if_needed(specs)

    settings = DiscordSettings()  # type: ignore[call-arg]
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"

    async with (
        DiscordPersonaSender(settings) as persona_sender,
        Client.connect(server_urls, provisioning=PROVISIONING) as calfkit_client,
    ):
        factory = AgentFactory(persona_sender, calfkit_client)
        nodes = []
        definition_refs: list[AgentDefinitionRef] = []
        for definition, state, store in specs:
            node = _build_node_or_bootstrap_error(
                factory, definition, state, store,
            )
            nodes.append(node)
            ref = AgentDefinitionRef(current=definition)
            definition_refs.append(ref)
            # Raw control-plane subscriber: must register BEFORE the broker
            # starts (once FastStream is consuming, adding subscribers on the
            # same broker is unsupported) and BEFORE the discovery topic is
            # consumed. ``Worker.start()`` starts the broker inside _run_worker
            # below, so registering here — and declaring the topics it consumes
            # via the on_startup blind-spot hook — preserves register-before-serve.
            register_control_sink(calfkit_client, ref)

        worker = Worker(calfkit_client, nodes)
        # Managed lifecycle: ``Worker.start()`` (via _run_worker) auto-registers
        # handlers and auto-provisions the node topics + framework return inboxes
        # at broker start, and the connect-hook auto-provisions the client reply
        # topic — so the runner no longer hand-rolls register_handlers() /
        # topics_for_nodes() / a bare broker.start(). Presence, departure, and
        # the blind-spot topic declaration are the three lifecycle hooks wired
        # here; each runs at the precise broker-lifecycle moment it needs.
        _register_lifecycle_hooks(worker, calfkit_client, definition_refs)

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
    except BootstrapError as e:
        raise SystemExit(str(e)) from None
    except KeyboardInterrupt:
        logger.info("agent shutting down")


if __name__ == "__main__":
    main()
