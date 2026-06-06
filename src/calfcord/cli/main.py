"""``calfcord-cli`` argparse entry point — the management command dispatcher.

The native ``calfcord`` shim translates user-facing management subcommands
(``calfcord init``, ``calfcord agent ...``) into ``calfcord-cli <subcommand>``
and execs them through the same locked venv as the runners. ``prog="calfcord"``
so ``--help`` reads as the command the user actually types. Future verbs
register additional subparsers; the shim only needs to know the top-level verb
(``init`` / ``doctor`` / ``agent`` / ``router`` / ``tools``) to dispatch them here.
The ``run`` / ``auth`` verbs are translated to console scripts in the shim itself,
not here. ``mcp`` is SPLIT in the shim: its singleton-host *lifecycle*
(``mcp start|stop``) dispatches here like the other component verbs, while its
*config* verbs (``mcp add`` / ``mcp codegen``) stay separate console scripts —
keeping the lifecycle path off the bridge-only MCP-secrets loader.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from calfcord.cli import (
    agent_create,
    agent_edit,
    agent_inspect,
    agent_lifecycle,
    agent_tools,
    doctor,
    init,
    router_config,
)
from calfcord.cli._agents import detect_agents
from calfcord.cli._fields import FIELDS
from calfcord.cli._prompts import make_prompter
from calfcord.health.check import default_broker_probe, healthcheck
from calfcord.supervisor import component, lifecycle, roster


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calfcord",
        description="Manage a calfcord install (configure, inspect).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Guided first-run configuration of the install's .env.")

    doctor_p = sub.add_parser(
        "doctor",
        help="Preflight an install: config, broker, Discord token + app id, and agents.",
    )
    doctor_p.add_argument(
        "--offline",
        action="store_true",
        help="Skip the live Discord token check (no network).",
    )
    # doctor is intentionally read-only. ``--fix`` (safe auto-repairs: free-port
    # selection, missing dirs) is DEFERRED out of this phase (design §4.4), so the
    # flag is NOT registered — advertising it in --help while doctor.run can't act
    # on it would be a no-op that lies. Re-add it together with the auto-repair
    # plumbing in doctor.run.

    # ``agent`` is a verb group, not a leaf: ``required=True`` on its
    # sub-parsers makes a bare ``calfcord agent`` print help + exit non-zero
    # rather than silently no-op.
    agent_p = sub.add_parser("agent", help="Create, inspect, and edit agents.")
    agent_sub = agent_p.add_subparsers(dest="agent_command", required=True)

    create_p = agent_sub.add_parser("create", help="Create a new agent (guided wizard).")
    create_p.add_argument("name", nargs="?", help="Agent name (omit to be prompted).")

    list_p = agent_sub.add_parser("list", help="List all agents.")
    list_p.add_argument("--json", action="store_true", help="Emit a JSON array instead of a table.")

    show_p = agent_sub.add_parser("show", help="Show one agent's full config.")
    show_p.add_argument("name", help="Agent name.")
    show_p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    edit_p = agent_sub.add_parser("edit", help="Interactively edit any of an agent's config fields.")
    edit_p.add_argument("name", nargs="?", help="Agent name (omit to pick interactively).")

    set_p = agent_sub.add_parser("set", help="Set config fields non-interactively (scripting/CI).")
    set_p.add_argument("name", help="Agent name.")
    _add_set_flags(set_p)

    rename_p = agent_sub.add_parser("rename", help="Rename an agent (file, slash command, and state).")
    rename_p.add_argument("old", help="Current agent name.")
    rename_p.add_argument("new", help="New agent name.")

    delete_p = agent_sub.add_parser("delete", help="Delete an agent.")
    delete_p.add_argument("name", help="Agent name.")
    delete_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    delete_p.add_argument("--keep-state", action="store_true", help="Keep the agent's channel-subscription state file.")

    tools_p = agent_sub.add_parser("tools", help="Interactively edit an agent's tool list.")
    tools_p.add_argument("name", nargs="?", help="Agent name (omit to pick interactively).")

    # Roster lifecycle (design §2 / §3.4-§3.5): a teammate clocking in/out of the
    # *running* office. These are thin veneers over
    # :mod:`calfcord.supervisor.roster`; the duplicate guard, the workspace check,
    # and the §13.1 not-a-declared-slot steer live there. ``start``/``stop``/
    # ``restart`` take a required positional name; ``ps`` (the *running* roster, as
    # opposed to ``list``'s *defined* roster) takes none.
    start_p = agent_sub.add_parser("start", help="Bring an agent online: a teammate clocks into the live org.")
    start_p.add_argument("name", help="Agent name.")

    stop_p = agent_sub.add_parser("stop", help="Take an agent offline: a teammate clocks out.")
    stop_p.add_argument("name", help="Agent name.")

    restart_p = agent_sub.add_parser("restart", help="Reload a running agent after editing its .md.")
    restart_p.add_argument("name", help="Agent name.")

    agent_sub.add_parser("ps", help="Show RUNNING agents (vs. `agent list`, which shows DEFINED agents).")

    # ``router`` mirrors ``agent``: a verb group, not a leaf. ``required=True``
    # makes a bare ``calfcord router`` print help + exit non-zero so the group
    # can grow further commands later. The router holds an LLM connection like an
    # agent, so it gets a first-class, *editable* config surface (show/set/edit)
    # rather than a one-shot wizard, plus its own roster lifecycle (start/stop).
    router_p = sub.add_parser("router", help="Manage the ambient-message router.")
    router_sub = router_p.add_subparsers(dest="router_command", required=True)
    router_sub.add_parser("show", help="Show the router's configured provider/model.")
    router_set_p = router_sub.add_parser("set", help="Set the router's LLM provider/model.")
    router_set_p.add_argument("--provider")  # validated in router_config.set_config against the Provider literal
    router_set_p.add_argument("--model")
    router_sub.add_parser("edit", help="Configure the OPTIONAL ambient router interactively (provider, model).")
    router_sub.add_parser("start", help="Bring the router online (needs config).")
    router_sub.add_parser("stop", help="Take the router offline.")
    # ``setup`` is the pre-redesign wizard, now SUPERSEDED by ``edit``. Kept as a
    # back-compat alias so existing muscle memory / docs / scripts keep working;
    # it dispatches to the same one-shot ambient-router wizard it always has.
    router_sub.add_parser("setup", help="Deprecated alias of `router edit`.")

    # ``tools`` and ``mcp`` are SINGLETON roster components: one declared Process
    # Compose slot per role, clocking in/out of the running office. Unlike the
    # router they have NO config surface here (tools needs none; ``mcp add`` /
    # ``mcp codegen`` are separate console scripts routed by the shim, never
    # through this argparse entry point), so each is a ``start|stop`` group whose
    # whole veneer is the dispatch to the generic ``component_start/stop`` with the
    # slot name. ``required=True`` makes a bare ``calfcord tools`` / ``calfcord
    # mcp`` print help + exit non-zero rather than silently no-op, so the groups
    # can grow further verbs later.
    for _component in ("tools", "mcp"):
        component_p = sub.add_parser(_component, help=f"Manage the {_component} host.")
        component_sub = component_p.add_subparsers(dest=f"{_component}_command", required=True)
        component_sub.add_parser("start", help=f"Bring the {_component} host online.")
        component_sub.add_parser("stop", help=f"Take the {_component} host offline.")

    # Substrate lifecycle (design §2 / §13): bring the always-on office (broker +
    # bridge) up detached, close it, and glance at the org board. These are thin
    # veneers over :mod:`calfcord.supervisor.lifecycle`; the heavy contract (the
    # detached launch flags, the #494 priming reconcile, the readiness gate) lives
    # there. They take no flags today — the lifecycle entry points are nullary
    # beyond the resolved install paths.
    sub.add_parser("start", help="Open the workspace: broker + bridge, detached, health-gated.")
    sub.add_parser("stop", help="Close the workspace (stops the supervised substrate).")
    sub.add_parser("status", help="Show the org board: substrate + roster health.")

    # Hidden internal subcommand: the Process Compose readiness exec probe runs
    # ``calfcord _healthcheck <component>`` on the agent/tools hosts. No ``help=``
    # so it stays out of the user-facing command listing (design §4.2 / §13.2).
    health_p = sub.add_parser("_healthcheck")
    health_p.add_argument("component", help="The component to probe (broker, bridge, an agent id, ...).")
    return parser


def _resolve_home() -> Path | None:
    """Return the install home from ``$CALFCORD_HOME``, or ``None`` for dev runs.

    The shim exports ``CALFCORD_HOME`` so config + agents resolve under the
    install layout; a bare ``uv run calfcord-cli init`` (no shim) leaves it
    unset, which selects the project-local ``./.env`` / ``./agents`` defaults.
    An empty value is treated as unset so a stray ``CALFCORD_HOME=`` does not
    point config at ``"/config/.env"``.
    """
    home = os.environ.get("CALFCORD_HOME")
    return Path(home) if home else None


def _resolve_state_dir(home: Path | None) -> Path:
    """Per-agent state dir (channel-subscription JSON), needed by rename/delete.

    Mirrors the runner's resolution so the CLI moves/removes the exact file the
    agent reads: ``CALFKIT_STATE_DIR`` wins (the shim sets it to
    ``$H/state/agents``); otherwise ``$H/state/agents`` on a native install, or
    the dev ``./state/agents`` default.
    """
    override = os.environ.get("CALFKIT_STATE_DIR")
    if override:
        return Path(override)
    if home is not None:
        return home / "state" / "agents"
    return Path("state") / "agents"


def _add_set_flags(set_p: argparse.ArgumentParser) -> None:
    """Add one ``agent set`` flag per editable field, driven by the FIELDS registry.

    The single ``provider_model`` row becomes two flags (``--provider`` /
    ``--model``) so a provider switch can carry its model; every other field gets
    its declared flag with ``dest`` = the field key, so the dispatcher hands
    ``run_set`` a clean ``{key: value}`` dict with no second mapping to drift.
    """
    for field in FIELDS:
        if field.kind == "provider_model":
            continue
        suffix = f" ({field.int_min}-{field.int_max})" if field.kind == "int" else ""
        set_p.add_argument(field.flag, dest=field.key, default=None, help=field.label + suffix)
    set_p.add_argument("--provider", dest="provider", default=None, help="Model provider")
    set_p.add_argument("--model", dest="model", default=None, help="Model id")


def _collect_set_updates(args: argparse.Namespace) -> dict[str, str]:
    """Gather the provided ``agent set`` flags into a ``{field_key: value}`` dict.

    A ``--system-prompt @file`` value is expanded to the file's contents so an
    operator can script a multi-line prompt; every other value is the raw string.
    Raises OSError if an ``@file`` can't be read (the caller surfaces it cleanly).
    """
    updates: dict[str, str] = {}
    for field in FIELDS:
        if field.kind == "provider_model":
            continue
        value = getattr(args, field.key)
        if value is None:
            continue
        if field.key == "system_prompt" and value.startswith("@"):
            value = Path(value[1:]).read_text(encoding="utf-8")
        updates[field.key] = value
    for key in ("provider", "model"):
        value = getattr(args, key)
        if value is not None:
            updates[key] = value
    return updates


_ROSTER_COMMANDS = frozenset({"start", "stop", "restart", "ps"})


def _run_agent_roster(command: str, name: str | None) -> int:
    """Dispatch a roster verb (``agent start|stop|restart|ps``) — §3.4-§3.5.

    Like :func:`_run_lifecycle`, these drive the *install-scoped* Process Compose
    supervisor: its REST port is derived from ``$CALFCORD_HOME`` and the roster
    ops talk to it (and, for ``start``/``ps``, to the broker-wide control-plane
    probe). A dev run (no ``CALFCORD_HOME``) has no stable home for the supervisor,
    so these verbs refuse to run there with the same actionable native-install
    message rather than driving a half-built invocation against the project tree.

    ``server_urls`` comes from ``CALF_HOST_URL`` (defaulting to ``localhost``, the
    same default the runners, the broker healthcheck, and ``start`` use); it feeds
    the §3.5 duplicate guard (``start``) and the §3.4 logical-roster probe
    (``ps``). ``stop``/``restart`` need no probe. Each roster coroutine's POSIX
    exit code is propagated unchanged.
    """
    home = _resolve_home()
    if home is None:
        print(
            f"error: `calfcord agent {command}` needs a native install — set "
            "CALFCORD_HOME (or run the installer) so the supervisor has a stable home."
        )
        return 1

    if command == "stop":
        return asyncio.run(roster.agent_stop(home, name=name))
    if command == "restart":
        return asyncio.run(roster.agent_restart(home, name=name))

    # ``start`` and ``ps`` additionally consult the broker-wide control-plane
    # probe, so they need the broker URL.
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    if command == "ps":
        return asyncio.run(roster.agent_ps(home, server_urls=server_urls))
    return asyncio.run(roster.agent_start(home, name=name, server_urls=server_urls))


def _run_agent(args: argparse.Namespace) -> int:
    """Dispatch a ``calfcord agent <verb>`` command, resolving the install paths once."""
    # Roster verbs drive the supervisor, not the agents *files*, so they short-
    # circuit BEFORE the disk-path resolution the file verbs share — and keep the
    # *running* view (`ps`) distinct from the *defined* view (`list`).
    if args.agent_command in _ROSTER_COMMANDS:
        return _run_agent_roster(args.agent_command, getattr(args, "name", None))

    home = _resolve_home()
    env_path, agents_dir = init.resolve_paths(home)
    cmd = args.agent_command
    if cmd == "create":
        return agent_create.run(make_prompter(), agents_dir=agents_dir, env_path=env_path, name=args.name)
    if cmd == "list":
        return agent_inspect.run_list(agents_dir, as_json=args.json)
    if cmd == "show":
        return agent_inspect.run_show(agents_dir, args.name, as_json=args.json)
    if cmd == "edit":
        return agent_edit.run(make_prompter(), agents_dir=agents_dir, env_path=env_path, name=args.name)
    if cmd == "set":
        try:
            updates = _collect_set_updates(args)
        except OSError as e:
            print(f"error: {e}")
            return 1
        return agent_lifecycle.run_set(agents_dir, args.name, updates)
    if cmd == "rename":
        return agent_lifecycle.run_rename(agents_dir, _resolve_state_dir(home), args.old, args.new)
    if cmd == "delete":
        return agent_lifecycle.run_delete(
            make_prompter(), agents_dir, _resolve_state_dir(home), args.name,
            yes=args.yes, keep_state=args.keep_state,
        )
    # ``tools`` (and any unhandled verb — argparse ``required=True`` prevents the latter)
    return agent_tools.run(make_prompter(), agents_dir=agents_dir, name=args.name)


def _run_healthcheck(component: str) -> int:
    """Run the readiness probe for ``component`` and return its exit code.

    The Process Compose exec probe shells out to ``calfcord _healthcheck
    <component>`` on the agent/tools hosts (design §4.2 / §13.2). The broker probe
    is metadata reachability built from ``CALF_HOST_URL`` (same default the runners
    use); every other component is judged by heartbeat freshness under the resolved
    home's ``state/health/``. ``now`` is the real clock — freshness is wall-time
    here, injectable only in the unit-tested :func:`~calfcord.health.check.healthcheck`.
    """
    home = _resolve_home() or Path()
    # Only the broker path needs (and awaits) a broker probe; a heartbeat check
    # must not pay the admin-client cost, so the probe is built lazily and the
    # heartbeat path gets a stub that is never awaited.
    if component == "broker":
        server_urls = os.getenv("CALF_HOST_URL") or "localhost"
        broker_probe = default_broker_probe(server_urls)
    else:
        async def broker_probe() -> bool:
            raise AssertionError("broker probe awaited for a non-broker component")

    return asyncio.run(
        healthcheck(home, component, now=datetime.now(UTC), broker_probe=broker_probe)
    )


def _run_lifecycle(command: str) -> int:
    """Dispatch a substrate-lifecycle verb (``start`` / ``stop`` / ``status``).

    The Process Compose supervisor is *install-scoped*: its lock, derived REST
    port, generated project, and logs all live under ``$CALFCORD_HOME/state``,
    and ``start`` supervises processes by execing the install's shim. A dev run
    (no ``CALFCORD_HOME``) has neither a shim nor a stable home, so these verbs
    refuse to run there with an actionable message rather than launching a
    half-built supervisor against the project-local dev tree.

    ``server_urls`` comes from ``CALF_HOST_URL`` (defaulting to ``localhost``,
    the same default the runners and the broker healthcheck use); the roster is
    the install's defined agents (:func:`detect_agents`, the seam ``agent list``
    consumes) so the generated project declares one disabled slot per ``.md``.
    The lifecycle coroutine's POSIX exit code is propagated unchanged.
    """
    home = _resolve_home()
    if home is None:
        print(
            f"error: `calfcord {command}` needs a native install — set CALFCORD_HOME "
            "(or run the installer) so the supervisor has a stable home and shim."
        )
        return 1

    if command == "stop":
        return asyncio.run(lifecycle.stop(home))
    if command == "status":
        return asyncio.run(lifecycle.status(home))

    # ``start`` additionally needs the shim launcher every supervised process
    # execs under, the broker URL, and the roster to declare.
    _, agents_dir = init.resolve_paths(home)
    launcher = str(home / "shims" / "calfcord")
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    return asyncio.run(
        lifecycle.start(
            home,
            server_urls=server_urls,
            launcher=launcher,
            agent_ids=detect_agents(agents_dir),
        )
    )


def _run_router(args: argparse.Namespace) -> int:
    """Dispatch a ``calfcord router <verb>`` command (design §4.3 / §12.0).

    The router holds an LLM connection like an agent, so it has a first-class,
    editable *config* surface (``show`` / ``set`` / ``edit``) plus its own roster
    *lifecycle* (``start`` / ``stop``). The two halves resolve different seams:

    * **Config** verbs only need ``env_path`` — they read/write the two
      ``CALFKIT_ROUTER_*`` vars in the same ``config/.env`` (dev: ``./.env``) the
      runner reads, so a change is picked up on the router's next (re)start.
    * **Lifecycle** verbs are async and drive the *install-scoped* Process Compose
      supervisor, whose REST port is derived from the ``$CALFCORD_HOME`` dir (via
      :func:`~calfcord.supervisor.lifecycle.pc_port_for`). So they pass that home
      dir itself — the SAME value ``agent start/stop`` and the substrate lifecycle
      pass — never ``env_path``'s parent, or the port would disagree. ``start``
      additionally needs ``env_path`` for its fail-fast unconfigured precondition.
      No broker URL is consulted: component lifecycle does not probe the broker.

    Each handler's exit code is propagated unchanged.
    """
    env_path, _ = init.resolve_paths(_resolve_home())

    if args.router_command == "show":
        return router_config.show(env_path=env_path)
    if args.router_command == "set":
        return router_config.set_config(env_path=env_path, provider=args.provider, model=args.model)
    if args.router_command in ("edit", "setup"):
        if args.router_command == "setup":
            # ``setup`` is a deprecated alias kept only for muscle memory / old
            # docs; there is now ONE wizard. Steer the operator at the new verb,
            # then dispatch to the SAME implementation so the two can never drift.
            print("note: `router setup` is deprecated; use `router edit`.")
        return router_config.edit(make_prompter(), env_path=env_path)

    # Lifecycle (async): the home dir is the supervisor key, identical to what
    # agent start/stop and the substrate lifecycle pass, so pc_port_for agrees. A
    # dev run (no CALFCORD_HOME) has no stable home for the supervisor, so these
    # verbs refuse with the same actionable native-install message every other
    # lifecycle surface uses — rather than crashing in os.fspath(None) downstream.
    home = _resolve_home()
    if home is None:
        print(
            f"error: `calfcord router {args.router_command}` needs a native install — set "
            "CALFCORD_HOME (or run the installer) so the supervisor has a stable home."
        )
        return 1
    if args.router_command == "start":
        return asyncio.run(router_config.router_start(home, env_path=env_path))
    return asyncio.run(router_config.router_stop(home))


def _run_component(name: str, verb: str) -> int:
    """Dispatch a singleton-component lifecycle verb (``tools|mcp start|stop``).

    ``tools`` and ``mcp`` are SINGLETON roster components, so — unlike the router,
    which carries an editable LLM config — their lifecycle is the *entire* surface
    and a thin veneer over the generic
    :func:`calfcord.supervisor.component.component_start` /
    :func:`~calfcord.supervisor.component.component_stop`. The slot ``name`` is the
    component's declared Process Compose process (see
    :func:`calfcord.supervisor.compose.build_compose_project`); ``verb`` selects
    start vs stop.

    Like every other lifecycle surface (substrate, agent roster, router), this
    drives the *install-scoped* supervisor whose REST port is derived from
    ``$CALFCORD_HOME`` (:func:`~calfcord.supervisor.lifecycle.pc_port_for`), so it
    passes that home dir itself — the SAME value those siblings pass. A dev run (no
    ``CALFCORD_HOME``) has no stable home for the supervisor, so it refuses with the
    same actionable native-install message rather than crashing in
    ``os.fspath(None)`` downstream. No broker URL is consulted: component lifecycle
    does not probe the broker. The component coroutine's exit code is propagated
    unchanged.

    ``mcp start`` deliberately runs NO config pre-check: the only ``mcp.json``
    readers are the bridge-only secrets loader
    (:func:`calfcord.mcp.config.load_mcp_servers`, forbidden on the CLI path by the
    decoupling invariant) and the ``mcp add`` writer's private parser (which pulls
    in the whole add machinery), so a light, clean reuse is not available — per
    design §12.4 the veneer is just ``component_start``.
    """
    home = _resolve_home()
    if home is None:
        print(
            f"error: `calfcord {name} {verb}` needs a native install — set "
            "CALFCORD_HOME (or run the installer) so the supervisor has a stable home."
        )
        return 1

    if verb == "start":
        return asyncio.run(component.component_start(home, name=name))
    return asyncio.run(component.component_stop(home, name=name))


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Route a parsed command to its handler (the interactive, prompt-driven part)."""
    if args.command in ("start", "stop", "status"):
        return _run_lifecycle(args.command)

    if args.command == "init":
        # Pass the install ``home`` and broker URL so the wizard's live finish can
        # orchestrate the install-scoped supervisor (it degrades to manual
        # next-steps when ``home`` is None / a dev run). ``server_urls`` mirrors
        # the same ``CALF_HOST_URL`` default the runners and ``start`` use.
        home = _resolve_home()
        env_path, agents_dir = init.resolve_paths(home)
        return init.run(
            make_prompter(),
            env_path=env_path,
            agents_dir=agents_dir,
            home=home,
            server_urls=os.getenv("CALF_HOST_URL") or "localhost",
        )

    if args.command == "doctor":
        # Preflight the same config/.env + agents/ the runners load. Passing the
        # resolved install ``home`` (None in dev mode) activates doctor's runtime
        # daemon-health section when the workspace is open; it stays correctly
        # skipped on a dev run with no install heartbeats to read.
        env_path, agents_dir = init.resolve_paths(_resolve_home())
        return doctor.run(
            env_path=env_path, agents_dir=agents_dir, offline=args.offline, home=_resolve_home()
        )

    if args.command == "agent":
        return _run_agent(args)

    if args.command == "_healthcheck":
        return _run_healthcheck(args.component)

    if args.command == "router":
        return _run_router(args)

    # ``tools`` / ``mcp`` are singleton-component verb groups; the per-group dest
    # (``tools_command`` / ``mcp_command``) carries the start/stop verb.
    if args.command in ("tools", "mcp"):
        return _run_component(args.command, getattr(args, f"{args.command}_command"))

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # The dispatch drives interactive prompts; trap the two ways an operator/host
    # ends one abruptly so the management CLI exits cleanly instead of dumping a
    # traceback (matching every other calfcord entry point). The run_* handlers
    # already map their own filesystem errors to exit codes, so an interrupt or a
    # raw-mode failure is all that should escape to here.
    try:
        return _dispatch(parser, args)
    except KeyboardInterrupt:
        print("\naborted.")
        return 130
    except EOFError:
        print("error: this command needs an interactive terminal (stdin reached EOF).")
        return 1
    except OSError:
        # InquirerPy/prompt_toolkit raises OSError (EINVAL) when it can't put a
        # non-TTY stdin (piped / CI) into raw mode. Surface that cleanly, but only
        # when stdin genuinely isn't a TTY — re-raise anything else rather than
        # masking a real bug behind a friendly message.
        if not sys.stdin.isatty():
            print("error: this command needs an interactive terminal (stdin is not a TTY).")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
