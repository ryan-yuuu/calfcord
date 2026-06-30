"""``calfcord-cli`` argparse entry point — the management command dispatcher.

The native ``calfcord`` shim translates user-facing management subcommands
(``calfcord init``, ``calfcord agent ...``) into ``calfcord-cli <subcommand>``
and execs them through the same locked venv as the runners. ``prog="calfcord"``
so ``--help`` reads as the command the user actually types. Future verbs
register additional subparsers; the shim only needs to know the top-level verb
(``init`` / ``doctor`` / ``agent`` / ``tools``) to dispatch them here.
The ``run`` / ``auth`` verbs are translated to console scripts in the shim itself,
not here.
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
    deploy,
    doctor,
    explain,
    init,
    logs,
    mcp_admin,
)
from calfcord.cli._agents import detect_agents
from calfcord.cli._fields import FIELDS
from calfcord.cli._mcp import configured_mcp_servers_or_none
from calfcord.cli._prompts import make_prompter
from calfcord.health.check import default_broker_probe, healthcheck
from calfcord.mcp.config import resolve_config_path
from calfcord.supervisor import component, lifecycle, mcp_roster, roster


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
    # ``restart`` take EITHER a positional name OR ``--all`` (exactly one — the
    # mutual-exclusion is enforced in the dispatcher, not by argparse, so it can
    # carry a clear domain message). ``--all`` is LOCAL-only: it sweeps THIS host's
    # supervisor (per-verb target set: ``start`` = every defined agent; ``stop`` /
    # ``restart`` = every running local agent), never the org over the wire. ``ps``
    # (the *running* roster, as opposed to ``list``'s *defined* roster) takes none.
    for _verb, _help in (
        ("start", "Bring an agent online: a teammate clocks into the live org."),
        ("stop", "Take an agent offline: a teammate clocks out."),
        ("restart", "Reload a running agent after editing its .md."),
    ):
        _p = agent_sub.add_parser(_verb, help=_help)
        _p.add_argument("name", nargs="?", help="Agent name (or pass --all).")
        _p.add_argument(
            "--all",
            dest="all",
            action="store_true",
            help="Act on every agent on this host (instead of one named agent).",
        )

    agent_sub.add_parser("ps", help="Show RUNNING agents (vs. `agent list`, which shows DEFINED agents).")

    # ``tools`` is a SINGLETON roster component: one declared Process Compose slot,
    # clocking in/out of the running office. It has NO config
    # surface here, so it is a ``start|stop|restart`` group whose whole veneer is
    # the dispatch to the generic ``component_start/stop`` with the slot name.
    # ``required=True`` makes a bare ``calfcord tools`` print help + exit non-zero
    # rather than silently no-op, so the group can grow further verbs later.
    # ``--all`` is a forward-compatible SYNONYM for the bare verb here: a singleton
    # is one process per host, so ``--all`` targets that one instance — it
    # dispatches to the same singular component handler, accepted only so the
    # roster verbs read uniformly.
    tools_p = sub.add_parser("tools", help="Manage the tools host.")
    tools_sub = tools_p.add_subparsers(dest="tools_command", required=True)
    for _verb, _help in (
        ("start", "Bring the tools host online."),
        ("stop", "Take the tools host offline."),
        ("restart", "Reload the running tools host."),
    ):
        _cp = tools_sub.add_parser(_verb, help=_help)
        _cp.add_argument(
            "--all",
            dest="all",
            action="store_true",
            help="Synonym for the bare verb (acts on this host's tools).",
        )

    # Tool aliases (CALFCORD_TOOLS_ALIAS) — install config the tools/agent
    # hosts read at boot; managed here, not by a launch flag (ADR-0007).
    alias_p = tools_sub.add_parser("alias", help="Manage tool aliases (CALFCORD_TOOLS_ALIAS).")
    alias_sub = alias_p.add_subparsers(dest="tools_alias_command", required=True)
    _restart_help = "Restart the tools host + agents to apply now (if a workspace is running)."
    _aadd = alias_sub.add_parser("add", help="Alias a tool under a new name (multi-host routing).")
    _aadd.add_argument("src", help="The tool to alias (e.g. terminal).")
    _aadd.add_argument("dst", help="The new name to expose it under (e.g. terminal_eu).")
    _aadd.add_argument("--restart", action="store_true", help=_restart_help)
    alias_sub.add_parser("list", help="List configured tool aliases.")
    _arm = alias_sub.add_parser("remove", help="Remove a tool alias by its new name.")
    _arm.add_argument("dst", help="The alias (new name) to remove.")
    _arm.add_argument("--restart", action="store_true", help=_restart_help)

    # MCP servers (design: docs/design/mcp-reintroduction.md). Each mcp.json
    # server is its own roster slot (mcp-<server>), so the lifecycle verbs take
    # a server name OR --all like the agent roster. `start --all` sweeps every
    # CONFIGURED server (the "re-pick up mcp.json" command); `stop`/`restart
    # --all` sweep the RUNNING mcp- slots on this host.
    mcp_p = sub.add_parser("mcp", help="Manage MCP servers (mcp.json).")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command", required=True)
    for _verb, _help in (
        ("start", "Bring an MCP server online (a running one is restarted in place)."),
        ("stop", "Take an MCP server offline."),
        ("restart", "Reload an MCP server after an mcp.json edit."),
    ):
        _mp = mcp_sub.add_parser(_verb, help=_help)
        _mp.add_argument("server", nargs="?", help="Server name (an mcpServers key in mcp.json).")
        _mp.add_argument(
            "--all",
            dest="all",
            action="store_true",
            help="Act on all servers (start: every configured; stop/restart: every running).",
        )

    add_p = mcp_sub.add_parser(
        "add",
        help="Add a server to mcp.json (interactive wizard when no flags are given).",
    )
    add_p.add_argument("server", nargs="?", help="Server name (omit to be prompted).")
    add_p.add_argument(
        "--command",
        dest="command_line",
        help="stdio launch line (quoted; shlex-split into command+args).",
    )
    add_p.add_argument("--url", help="Streamable-HTTP endpoint URL.")
    add_p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME[=VALUE]",
        help="stdio env entry; bare NAME passes $NAME through. Repeatable.",
    )
    add_p.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="HTTP header entry. Repeatable.",
    )
    add_p.add_argument("--cwd", help="stdio working directory.")
    add_p.add_argument("--force", action="store_true", help="Replace an existing entry.")
    add_p.add_argument("--dry-run", action="store_true", help="Print the entry; write nothing.")
    add_p.add_argument("--start", action="store_true", help="Start the server after writing.")
    mcp_sub.add_parser("list", help="List configured MCP servers (and their local state).")
    remove_p = mcp_sub.add_parser("remove", help="Remove a server from mcp.json.")
    remove_p.add_argument("server", help="Server name to remove.")
    remove_p.add_argument("--force", action="store_true", help="Skip the confirmation prompt.")

    # Substrate lifecycle (design §2 / §13): bring the always-on office (broker +
    # bridge) up detached, close it, and glance at the org board. These are thin
    # veneers over :mod:`calfcord.supervisor.lifecycle`; the heavy contract (the
    # detached launch flags, the #494 priming reconcile, the readiness gate) lives
    # there. They take no flags today — the lifecycle entry points are nullary
    # beyond the resolved install paths.
    sub.add_parser("start", help="Open the workspace: broker + bridge, detached, health-gated.")
    sub.add_parser("stop", help="Close the workspace (stops the supervised substrate).")
    sub.add_parser("status", help="Show the org board: substrate + roster health.")

    # Graduation-tier surfaces (design §2 / §11). ``explain`` is a verb group (like
    # ``agent``) so its teaching catalogue can grow; ``topology`` is the only topic
    # today. ``logs`` and ``deploy`` are leaves with their own args.
    explain_p = sub.add_parser("explain", help="Explain calfcord's runtime topology and why it splits.")
    explain_sub = explain_p.add_subparsers(dest="explain_command", required=True)
    explain_sub.add_parser("topology", help="One screen: how the pieces split, and why.")

    logs_p = sub.add_parser("logs", help="Tail unified or per-component supervisor logs.")
    logs_p.add_argument("component", nargs="?", help="Component to tail (omit for all).")
    logs_p.add_argument("-f", "--follow", action="store_true", help="Follow log output.")

    deploy_p = sub.add_parser("deploy", help="Generate systemd / k8s / Docker manifests (advanced).")
    deploy_p.add_argument("target", choices=("systemd", "k8s", "docker"), help="Manifest format to render.")
    deploy_p.add_argument("-o", "--output", help="Write the manifest to PATH instead of stdout.")

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


_SUPERVISOR_HOME_DETAIL = "the supervisor has a stable home."


def _require_home(command: str, *, detail: str = _SUPERVISOR_HOME_DETAIL) -> Path | None:
    """Resolve the install home, or print the native-install error and return ``None``.

    Every supervisor-scoped verb (substrate/agent/component lifecycle,
    ``logs``, ``deploy``) is install-scoped: its lock, REST port, logs, shim, and
    manifests all live under ``$CALFCORD_HOME``. A dev run (no ``CALFCORD_HOME``)
    has no stable home, so each refuses with the SAME actionable message rather
    than crashing in ``os.fspath(None)`` downstream. Centralizing the guard keeps
    that message — and the "what to do" steer — from drifting across the six
    call sites; ``command`` is the backticked verb the operator typed (e.g.
    ``"agent stop"``, ``"deploy"``) and ``detail`` the trailing rationale clause
    (most want the default; ``start``/``logs``/``deploy`` add shim/logs/manifest
    specifics). Returns the resolved :class:`~pathlib.Path` on a native install,
    or ``None`` (after printing) so the caller can ``return 1`` immediately.
    """
    home = _resolve_home()
    if home is None:
        print(
            f"error: `calfcord {command}` needs a native install — set CALFCORD_HOME (or run the installer) so {detail}"
        )
    return home


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


def _require_one_roster_target(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Enforce exactly one of ``<name>`` | ``--all`` for an agent roster verb.

    ``name`` is ``nargs="?"`` and ``--all`` is a flag, so argparse alone would
    accept BOTH (contradictory: one targets a single agent, the other every agent
    on this host) or NEITHER (a no-op). Both are operator errors, so this resolves
    them at the dispatcher with a clear domain message via :meth:`parser.error`
    (which prints usage and exits ``2``) rather than letting the bare verb silently
    act on nothing — mirroring how the singleton groups' ``required=True`` rejects a
    bare group. ``ps`` takes neither and never calls this.
    """
    if args.all and args.name is not None:
        parser.error("name and --all are mutually exclusive")
    if not args.all and args.name is None:
        parser.error("give an agent name or --all")


def _run_agent_roster(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Dispatch a roster verb (``agent start|stop|restart|ps``) — §3.4-§3.5.

    Like :func:`_run_lifecycle`, these drive the *install-scoped* Process Compose
    supervisor: its REST port is derived from ``$CALFCORD_HOME`` and the roster
    ops talk to it (and, for ``start``/``ps``, to the broker-wide control-plane
    probe). A dev run (no ``CALFCORD_HOME``) has no stable home for the supervisor,
    so these verbs refuse to run there with the same actionable native-install
    message rather than driving a half-built invocation against the project tree.

    ``start``/``stop``/``restart`` take EITHER a ``name`` OR ``--all`` (behavior
    #1, decision B — uniform surface; exactly-one enforced by
    :func:`_require_one_roster_target`). ``--all`` is LOCAL-only and per-verb:
    ``start --all`` sweeps every DEFINED agent (so it passes the detected ``.md``
    ids — the same :func:`detect_agents` seam ``start`` / ``agent list`` use, so
    roster.py needs no agents-dir read); ``stop --all`` / ``restart --all`` sweep
    every RUNNING local agent (the bulk fns read the supervisor themselves, so no
    ids and no broker URL are passed). ``ps`` takes no target.

    ``server_urls`` comes from ``CALF_HOST_URL`` (defaulting to ``localhost``, the
    same default the runners, the broker healthcheck, and ``start`` use); it feeds
    the §3.5 duplicate guard (``start`` / ``start --all``) and the §3.4
    logical-roster probe (``ps``). ``stop``/``restart`` (and their ``--all``
    sweeps) need no probe. Each roster coroutine's POSIX exit code is propagated
    unchanged.
    """
    command = args.agent_command

    # Argument validity comes BEFORE the native-install guard: an invalid invocation
    # (no target, or both a name and --all) is an operator error to flag with the
    # parser (exit 2) regardless of whether a home is configured — so a dev run's
    # bare `agent start` still errors at the parser, not the home check. ``ps`` takes
    # no target and skips this.
    if command != "ps":
        _require_one_roster_target(parser, args)

    home = _require_home(f"agent {command}")
    if home is None:
        return 1

    if command == "ps":
        # ``ps`` is the read-only running view; it consults the broker-wide probe.
        server_urls = os.getenv("CALF_HOST_URL") or "localhost"
        return asyncio.run(roster.agent_ps(home, server_urls=server_urls))

    if command == "stop":
        if args.all:
            return asyncio.run(roster.agent_stop_all(home))
        return asyncio.run(roster.agent_stop(home, name=args.name))
    if command == "restart":
        if args.all:
            return asyncio.run(roster.agent_restart_all(home))
        return asyncio.run(roster.agent_restart(home, name=args.name))

    # ``start`` (and ``start --all``) additionally consult the broker-wide
    # control-plane probe, so they need the broker URL. ``start --all`` targets
    # every DEFINED agent — the ids come from the agents dir here so roster.py
    # stays off the disk read.
    server_urls = os.getenv("CALF_HOST_URL") or "localhost"
    if args.all:
        _, agents_dir = init.resolve_paths(home)
        return asyncio.run(roster.agent_start_all(home, agent_ids=detect_agents(agents_dir), server_urls=server_urls))
    return asyncio.run(roster.agent_start(home, name=args.name, server_urls=server_urls))


def _run_agent(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Dispatch a ``calfcord agent <verb>`` command, resolving the install paths once."""
    # Roster verbs drive the supervisor, not the agents *files*, so they short-
    # circuit BEFORE the disk-path resolution the file verbs share — and keep the
    # *running* view (`ps`) distinct from the *defined* view (`list`). They take the
    # parser too so the exactly-one-of name|--all rule can `parser.error` cleanly.
    if args.agent_command in _ROSTER_COMMANDS:
        return _run_agent_roster(parser, args)

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
            make_prompter(),
            agents_dir,
            _resolve_state_dir(home),
            args.name,
            yes=args.yes,
            keep_state=args.keep_state,
        )
    # ``tools`` (and any unhandled verb — argparse ``required=True`` prevents the latter)
    return agent_tools.run(make_prompter(), agents_dir=agents_dir, name=args.name)


def _run_healthcheck(component: str) -> int:
    """Run the readiness probe for ``component`` and return its exit code.

    The Process Compose exec probe shells out to ``calfcord _healthcheck
    <component>`` on the substrate hosts (design §4.2 / §13.2). Only the two
    components that emit a real signal are probeable: the ``broker`` (metadata
    reachability built from ``CALF_HOST_URL``, the same default the runners use)
    and the ``bridge`` (heartbeat freshness + Discord-connected, under the resolved
    home's ``state/health/``). Any other component raises — the roster runners
    declare no readiness probe and write no heartbeat. ``now`` is the real clock —
    freshness is wall-time here, injectable only in the unit-tested
    :func:`~calfcord.health.check.healthcheck`.
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

    return asyncio.run(healthcheck(home, component, now=datetime.now(UTC), broker_probe=broker_probe))


def _run_mcp(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Dispatch a ``calfcord mcp <verb>`` lifecycle command.

    The same install-scoped supervisor surface as the agent roster
    (:func:`_run_agent_roster`), driving the per-server ``mcp-<server>``
    slots via :mod:`calfcord.supervisor.mcp_roster`. ``start``/``stop``/
    ``restart`` take EITHER a server name OR ``--all`` (uniform with the
    agent verbs): ``start --all`` sweeps every server *configured* in
    mcp.json (enumerated here through the no-secrets ``list_server_names``
    seam, so mcp_roster stays off the config read), while ``stop --all`` /
    ``restart --all`` sweep every *running* ``mcp-`` slot (the bulk fns read
    the supervisor themselves).
    """
    command = args.mcp_command

    # Config-edit verbs (add/list/remove) are home-optional: they target the
    # resolved mcp.json (dev runs edit ./mcp.json) and only *optionally* touch
    # the supervisor (add's start step, list's state column) when a home exists.
    if command in ("add", "list", "remove"):
        config_path = resolve_config_path()
        home = _resolve_home()
        if command == "add":
            return mcp_admin.run_add(
                make_prompter(),
                config_path=config_path,
                server=args.server,
                command=args.command_line,
                env=args.env,
                url=args.url,
                header=args.header,
                cwd=args.cwd,
                force=args.force,
                dry_run=args.dry_run,
                start=args.start,
                home=home,
            )
        if command == "list":
            return mcp_admin.run_list(config_path=config_path, home=home)
        return mcp_admin.run_remove(
            make_prompter(),
            config_path=config_path,
            server=args.server,
            force=args.force,
            home=home,
        )

    # Argument validity before the native-install guard, mirroring the agent
    # roster: a bad invocation is a parser error (exit 2) even on a dev run.
    if args.server is not None and args.all:
        parser.error("server name and --all are mutually exclusive")
    if not args.all and args.server is None:
        parser.error("give an MCP server name or --all")

    home = _require_home(f"mcp {command}")
    if home is None:
        return 1

    if command == "stop":
        if args.all:
            return asyncio.run(mcp_roster.mcp_stop_all(home))
        return asyncio.run(mcp_roster.mcp_stop(home, server=args.server))
    if command == "restart":
        if args.all:
            return asyncio.run(mcp_roster.mcp_restart_all(home))
        return asyncio.run(mcp_roster.mcp_restart(home, server=args.server))

    if args.all:
        servers = configured_mcp_servers_or_none()
        if servers is None:
            return 1
        return asyncio.run(mcp_roster.mcp_start_all(home, servers=servers))
    return asyncio.run(mcp_roster.mcp_start(home, server=args.server))


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
    home = _require_home(command, detail="the supervisor has a stable home and shim.")
    if home is None:
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
    # MCP servers are roster slots too: enumerate mcp.json (no-secrets reader)
    # so the generated project declares one disabled mcp-<server> slot each. An
    # invalid mcp.json fails the whole start actionably rather than rendering a
    # project that silently lacks the servers.
    mcp_servers = configured_mcp_servers_or_none()
    if mcp_servers is None:
        return 1
    return asyncio.run(
        lifecycle.start(
            home,
            server_urls=server_urls,
            launcher=launcher,
            agent_ids=detect_agents(agents_dir),
            mcp_servers=mcp_servers,
        )
    )


def _run_component(name: str, verb: str) -> int:
    """Dispatch a singleton-component lifecycle verb (``tools start|stop``).

    ``tools`` is a SINGLETON roster component, so its lifecycle is the *entire*
    surface and a thin veneer over the generic
    :func:`calfcord.supervisor.component.component_start` /
    :func:`~calfcord.supervisor.component.component_stop`. The slot ``name`` is the
    component's declared Process Compose process (see
    :func:`calfcord.supervisor.compose.build_compose_project`); ``verb`` selects
    start vs stop.

    Like every other lifecycle surface (substrate, agent roster), this
    drives the *install-scoped* supervisor whose REST port is derived from
    ``$CALFCORD_HOME`` (:func:`~calfcord.supervisor.lifecycle.pc_port_for`), so it
    passes that home dir itself — the SAME value those siblings pass. A dev run (no
    ``CALFCORD_HOME``) has no stable home for the supervisor, so it refuses with the
    same actionable native-install message rather than crashing in
    ``os.fspath(None)`` downstream. No broker URL is consulted: component lifecycle
    does not probe the broker. The component coroutine's exit code is propagated
    unchanged.

    ``verb`` is one of ``start|stop|restart``. ``--all`` (behavior #1, decision B)
    is a forward-compatible SYNONYM here: a singleton runs one process per host, so
    ``--all`` targets that one instance — the caller has already collapsed it onto
    the same singular verb, so it is honest, not a separate code path.
    """
    home = _require_home(f"{name} {verb}")
    if home is None:
        return 1

    if verb == "start":
        return asyncio.run(component.component_start(home, name=name))
    if verb == "restart":
        return asyncio.run(component.component_restart(home, name=name))
    return asyncio.run(component.component_stop(home, name=name))


def _run_logs(component: str | None, *, follow: bool) -> int:
    """Dispatch ``calfcord logs [component] [-f]`` to the log-tail module.

    The supervisor writes each process's stdout/stderr under
    ``$CALFCORD_HOME/state/logs/`` (the §13.2 ``log_location`` contract), so this
    is install-scoped: a dev run (no ``CALFCORD_HOME``) has no such dir, and refuses
    with the same actionable native-install message every supervisor-scoped verb
    uses rather than tailing a nonexistent project-local log dir. The agents dir is
    resolved from the same seam ``start``/``agent list`` use so the tail's known-name
    set equals the roster the supervisor declares. Synchronous (no broker, no REST):
    :func:`logs.tail` reads files straight off disk, and its ``-f`` follow loop exits
    cleanly on Ctrl-C (``main`` maps the interrupt to 130).
    """
    home = _require_home("logs", detail="the supervisor has a stable home and logs dir.")
    if home is None:
        return 1
    _, agents_dir = init.resolve_paths(home)
    return logs.tail(home, agents_dir=agents_dir, component=component, follow=follow)


def _run_deploy(target: str, *, output: str | None) -> int:
    """Dispatch ``calfcord deploy <target> [--output PATH]`` to the manifest module.

    The rendered manifests reference the install's shim launcher
    (``<home>/shims/calfcord``), home paths, and ``config/.env``, so this is
    install-scoped: a dev run (no ``CALFCORD_HOME``) has no shim to emit, and refuses
    with the same actionable native-install message rather than rendering a manifest
    that points at nothing. The roster + ``.env`` come off disk via the seams
    ``deploy.run`` consumes (:func:`detect_agents` / :func:`read_env`), and
    ``server_urls`` defaults to ``localhost`` — the same default the runners and
    ``start`` use. Synchronous (pure text rendering, no broker, no REST).
    """
    home = _require_home("deploy", detail="the manifest can reference a stable home and shim.")
    if home is None:
        return 1
    env_path, agents_dir = init.resolve_paths(home)
    return deploy.run(
        target,
        home=home,
        env_path=env_path,
        agents_dir=agents_dir,
        server_urls=os.getenv("CALF_HOST_URL") or "localhost",
        out_path=Path(output) if output is not None else None,
    )


def _run_tool_alias(args: argparse.Namespace) -> int:
    """Dispatch ``calfcord tools alias <add|list|remove>`` to its handlers.

    Edits the install ``.env`` (``CALFCORD_TOOLS_ALIAS``). Works in a native
    install or a dev tree — unlike ``deploy`` it only touches ``.env``, so it
    needs no ``$CALFCORD_HOME``. ``add`` resolves the canonical tool surface
    from ``ALL_TOOLS`` so the validator can check the source and its
    aliasability without the CLI hard-coding the tool list.
    """
    from calfcord.cli import tool_aliases

    env_path, _ = init.resolve_paths(_resolve_home())
    cmd = args.tools_alias_command
    if cmd == "list":
        return tool_aliases.run_alias_list(env_path=env_path)

    # ``--restart`` injects the workspace-gated actuation; without it the
    # handler prints the apply-by-restart hint.
    apply_restart = _apply_alias_restart if args.restart else None
    if cmd == "remove":
        return tool_aliases.run_alias_remove(env_path=env_path, dst=args.dst, apply_restart=apply_restart)

    from calfcord.tools import ALL_TOOLS
    from calfcord.tools.deploy_filters import is_aliasable

    tool_names = {n.tool_schema.name for n in ALL_TOOLS}
    aliasable_names = {n.tool_schema.name for n in ALL_TOOLS if is_aliasable(n)}
    return tool_aliases.run_alias_add(
        env_path=env_path,
        src=args.src,
        dst=args.dst,
        tool_names=tool_names,
        aliasable_names=aliasable_names,
        apply_restart=apply_restart,
    )


def _apply_alias_restart() -> None:
    """Restart the tools host + running agents so a just-written alias applies.

    The ``--restart`` actuation for ``calfcord tools alias add/remove`` (ADR-0007):
    gated on a running workspace, then restart both roles that read
    ``CALFCORD_TOOLS_ALIAS`` at boot. On a dev tree (no ``$CALFCORD_HOME``) or a
    closed workspace it just notes the change applies on next start — the
    ``.env`` is read at process boot, so there is nothing to actuate.
    """
    home = _resolve_home()
    if home is None:
        print("not a native install; the alias applies on the next `calfkit-tools` start.")
        return

    # This up-front probe is for UX, not necessity: ``_run_component`` and
    # ``agent_restart_all`` each re-gate on the workspace internally, but
    # letting them fail separately would print two confusing "not running"
    # hints for what is really a single no-op. Probing once lets us print one
    # clean line instead. (Don't "simplify" this away.)
    from calfcord.supervisor._workspace import resolve_client, workspace_is_up

    async def _up() -> bool:
        return await workspace_is_up(resolve_client(None, str(home)))

    if not asyncio.run(_up()):
        print("workspace not running; the alias applies on next start.")
        return

    # Best-effort: the .env write already succeeded; each restart path prints
    # its own result, so a restart failure is visible without changing the
    # add/remove exit code (the alias edit itself did not fail).
    print("restarting the tools host and agents to apply the alias…")
    _run_component("tools", "restart")
    asyncio.run(roster.agent_restart_all(home))


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
        return doctor.run(env_path=env_path, agents_dir=agents_dir, offline=args.offline, home=_resolve_home())

    if args.command == "agent":
        return _run_agent(parser, args)

    if args.command == "_healthcheck":
        return _run_healthcheck(args.component)

    # ``tools`` is a singleton-component verb group; ``tools_command`` carries
    # the start/stop/restart verb — except ``alias``, which is its own config
    # subgroup (see _run_tool_alias).
    if args.command == "tools":
        if args.tools_command == "alias":
            return _run_tool_alias(args)
        return _run_component("tools", args.tools_command)

    if args.command == "mcp":
        return _run_mcp(parser, args)

    if args.command == "explain":
        return explain.run(args.explain_command)

    if args.command == "logs":
        return _run_logs(args.component, follow=args.follow)

    if args.command == "deploy":
        return _run_deploy(args.target, output=args.output)

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
