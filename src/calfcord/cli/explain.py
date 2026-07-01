"""``calfcord explain <topic>`` — read-only teaching screens for the runtime model.

This is the graduation-tier command that installs the mental model the rest of
calfcord assumes: the **substrate** (the always-on background office) versus the
**roster** (teammates that clock in and out), how those two layers map onto the
calfkit process types, and why the *same* config and commands graduate to a
distributed deployment without a rewrite (design §11.2; ``docs/architecture.md``).

It is deliberately **pure**: no supervisor, no broker, no install home. A dev run
(``uv run calfcord-cli explain topology``) and a native install must print the
*same* screen, so this module reads nothing from the environment and touches no
filesystem — there is no native-install guard. That purity is also why it stays
import-light (stdlib only).

The surface is a **topic dispatch** so the screen catalogue can grow: ``topology``
is the only topic that ships today, registered in :data:`TOPICS`. A future topic
adds a renderer and one registry entry; the CLI veneer and shim keep dispatching
``explain <topic>`` verbatim.
"""

from __future__ import annotations

from collections.abc import Callable

# An output sink — ``print`` in production, a list's ``append`` in tests — so the
# rendered content is assertable without capturing stdout.
OutputSink = Callable[[str], None]


def render_topology() -> str:
    """Return the ``explain topology`` teaching screen as a single string.

    Pure: no I/O, no environment reads. The body distils the substrate/roster
    runtime model and the distributed-graduation invariant into one screen the
    caller prints verbatim.
    """
    return _TOPOLOGY


def run_topology(*, out: OutputSink = print) -> int:
    """Render the topology screen to ``out`` and return ``0``.

    ``out`` is injected (defaults to ``print``) so a caller — or a test — can
    capture the rendered screen directly instead of through stdout.
    """
    out(render_topology())
    return 0


# Topic name → renderer-backed runner. The single seam future ``explain`` topics
# register against; iterating it also yields the catalogue for the not-found
# message, so adding a topic can never leave the error text stale.
TOPICS: dict[str, Callable[..., int]] = {
    "topology": run_topology,
}


def run(topic: str, *, out: OutputSink = print) -> int:
    """Dispatch ``explain <topic>`` to its renderer; ``1`` on an unknown topic.

    argparse normally constrains the topic to a known choice, but dispatching
    here too keeps the not-found message actionable (it names the offending topic
    and the topics that DO exist) and keeps the registry the single source of
    truth for what ``explain`` can teach.
    """
    runner = TOPICS.get(topic)
    if runner is None:
        known = ", ".join(sorted(TOPICS))
        out(f"error: unknown explain topic {topic!r} (known topics: {known})")
        return 1
    return runner(out=out)


# The screen itself. Kept as a module constant (not f-string-assembled) because it
# is static teaching copy — the wording is the contract, and a constant keeps the
# content tests pinned to one place. Wording follows design §11.2's "what you just
# built" teaching block and ``docs/architecture.md``'s four-process model.
_TOPOLOGY = """\
calfcord: how the pieces split, and why
=======================================

calfcord runs as two layers over a shared message bus (Kafka, via Tansu). You
manage both with the same `calfcord` command, on one machine or twenty.

The substrate — your always-on workspace
-----------------------------------------
`calfcord start` opens the substrate and nothing else:

  - broker   the Kafka message bus every other process talks through.
  - bridge   the single Discord gateway (calfkit-bridge): it turns Discord
             messages into bus events and posts agent replies back as personas.

The substrate is the office: it runs detached in the background and is the only
thing `start` brings up. Nothing else runs until you say so.

The roster — teammates that clock in and out
--------------------------------------------
Roster members join the *running* workspace on demand and leave when stopped:

  - agents   your AI teammates (calfkit-agent), one process per agent or many
             per process; each listens on its own private inbox.
  - tools    the tools host (calfkit-tools): filesystem, terminal, code
             execution, search, web, and todo.
  - mcp      MCP servers (calfkit-mcp), one process per server in mcp.json:
             each hosts one Model Context Protocol server and advertises its
             tools on the bus. The only processes holding MCP credentials —
             agents pick the tools up from the advertisement, never the config.

  `calfcord agent start <name>` brings a teammate online; `calfcord status`
  shows who is in the office; `calfcord stop` closes it.

The four process types
----------------------
Every box above is one of calfkit's independently deployable process types:

  - calfkit-bridge   the Discord gateway (substrate).
  - calfkit-agent    one or more agents (roster).
  - calfkit-tools    the tools host (roster).
  - calfkit-mcp      one MCP server's toolbox (roster), one per server.

They share nothing but the Kafka wire, the `.env` config, and the `agents/*.md`
definitions — never a function call, never a filesystem. That decoupling is what
makes the next part true.

Going distributed is a deployment change, never a rewrite
---------------------------------------------------------
Because the processes only ever meet on the bus, the same config and the same
commands run distributed across many hosts. Graduating to a remote broker is a
deployment change — point `CALF_HOST_URL` at a shared broker URL and run each
process where you want it — not a rewrite. One host or twenty, the wire contract
is identical.
"""
