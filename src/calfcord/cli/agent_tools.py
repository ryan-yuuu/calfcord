"""``calfcord agent tools [<name>]`` — interactive editor for an agent's tools.

Picks an agent ``.md`` (by name, or via a prompt over the install's agents
dir), shows a multi-select checkbox of every builtin tool, pre-checked from the
agent's current ``tools:`` declaration, and writes the operator's selection
back through the validated-atomic
:func:`calfcord.agents.md_writer.update_tools`.

It reads the RAW declaration, not the loader's expansion: it calls
:func:`calfcord.agents.definition.parse_agent_md` directly (not the loader's
default-resolving path) so it can distinguish ``tools:`` *omitted* (``None`` →
implicitly "all builtins") from ``tools: []`` (explicitly none). The
implicit-all case is converted into explicit checks here, and the write always
persists an explicit list, so on-disk state stops being ambiguous after the
first save. (MCP tools are not currently supported — calfkit dropped the
adaptor in 0.7.0 — so a ``.md`` still carrying an ``mcp/...`` entry fails to
parse and cannot be opened here until the entry is hand-removed.)

Tool edits take effect on the next ``calfcord calfkit-agent`` boot — the node
bakes its tool list at construction time (see the onboarding plan's "tools are
baked into the node at boot" finding), so the command tells the operator to
restart rather than implying a live reload.
"""

from __future__ import annotations

from pathlib import Path

from calfcord.agents.definition import parse_agent_md
from calfcord.agents.md_writer import update_tools
from calfcord.cli._agents import detect_agents
from calfcord.cli._prompts import Choice, Prompter


def first_line(desc: str | None) -> str:
    """Return a one-line, human-readable summary of a tool ``desc``.

    Tool descriptions are multi-line docstrings whose first line is wrapped in
    a ``<summary>...</summary>`` tag and sprinkled with reStructuredText
    double-backtick inline-literal markup; neither renders usefully in a
    single-line checkbox label. We take the first non-empty line, drop the
    summary wrapper, and collapse the double-backtick markup to plain text so
    the label is readable.
    """
    if not desc:
        return ""
    for raw in desc.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = line.removeprefix("<summary>").removesuffix("</summary>").strip()
        # ``\`\`x\`\``` (RST inline literal) -> ``x``: drop the double-backtick
        # markup so the label reads as plain prose.
        return line.replace("``", "")
    return ""


def _resolve_agent(prompter: Prompter, *, agents_dir: Path, name: str | None) -> Path | None:
    """Resolve the agent ``.md`` to edit, or ``None`` after printing why not.

    ``name`` given: require ``agents_dir/<name>.md`` to exist (an explicit
    request for a missing agent is an error, not a fallback to the picker).
    ``name`` omitted: list the detected agents and prompt; an empty dir is an
    error too — there is nothing to edit. Returning ``None`` (rather than
    raising) lets :func:`run` map every "can't proceed" case to exit code 1
    with a single, already-printed message.
    """
    if name is not None:
        md_path = agents_dir / f"{name}.md"
        if not md_path.is_file():
            print(f"error: no agent {name!r} in {agents_dir} (expected {md_path})")
            return None
        return md_path

    agents = detect_agents(agents_dir)
    if not agents:
        print(f"no agents in {agents_dir}")
        return None
    chosen = prompter.select(
        "Which agent's tools do you want to edit?",
        [Choice(a, a) for a in agents],
    )
    return agents_dir / f"{chosen}.md"


def _build_choices(current: set[str]) -> list[Choice]:
    """Build the checkbox :class:`Choice` rows from the builtin tool registry.

    Each builtin ``name`` is a row, checked iff it is in ``current``. Builtins
    are the only tool universe — anything the operator can tick is something
    :func:`calfcord.agents.md_writer.update_tools` will accept on write.
    Enumeration uses only the schema-only :data:`calfcord.tools.TOOL_REGISTRY`
    seam (no transport, no secrets).
    """
    from calfcord.tools import TOOL_REGISTRY

    choices: list[Choice] = []
    for name in sorted(TOOL_REGISTRY):
        summary = first_line(TOOL_REGISTRY[name].tool_schema.description)
        label = f"{name} — {summary}" if summary else name
        choices.append(Choice(name, label, name in current))
    return choices


def run(prompter: Prompter, *, agents_dir: Path, name: str | None) -> int:
    """Run the interactive tool editor and return an exit code.

    Resolves the agent, reads its RAW ``tools:`` declaration, shows the
    pre-checked multi-select, and writes the selection back. Returns 1 (with an
    already-printed ``error:`` line) when no agent can be resolved or the read /
    write fails; 0 after a successful write. Per the CLI error-handling
    convention, operator-recoverable problems (a malformed ``.md``, an
    unwritable file) print an actionable message and return non-zero rather than
    letting a raw traceback escape. All prompting goes through the injected
    :class:`Prompter`, so the flow is testable without a TTY.
    """
    md_path = _resolve_agent(prompter, agents_dir=agents_dir, name=name)
    if md_path is None:
        return 1
    agent_name = md_path.stem

    try:
        raw = parse_agent_md(md_path)
    except (ValueError, OSError) as e:
        # Malformed/empty frontmatter, a name≠stem mismatch, or an unreadable
        # file — operator-recoverable, so report it instead of crashing.
        print(f"error: cannot read agent {agent_name!r}: {e}")
        return 1

    if raw.tools is not None:
        current = set(raw.tools)
    else:
        # ``tools:`` omitted means "all builtins" — pre-check exactly the
        # builtins, matching the loader's default expansion.
        from calfcord.tools import TOOL_REGISTRY

        current = set(TOOL_REGISTRY)

    choices = _build_choices(current)

    selected = prompter.checkbox(
        f"Tools for {agent_name}",
        choices,
        instruction="space toggles, enter confirms",
    )

    try:
        update_tools(md_path, selected)
    except (ValueError, OSError) as e:
        # The write path: a read-only dir, ENOSPC, a concurrent delete, or a
        # token the writer rejects. Report and exit non-zero; the on-disk file
        # is left untouched by ``update_tools`` on failure.
        print(f"error: failed to update {agent_name!r}: {e}")
        return 1

    print(f"Updated {agent_name}: {len(selected)} tool(s). Restart `calfcord calfkit-agent` to apply.")
    return 0
