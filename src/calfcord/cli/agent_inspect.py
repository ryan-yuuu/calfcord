"""``disco agent list`` / ``disco agent show`` — read-only agent inspection.

These two commands let an operator survey the install ("which agents exist, on
what model, with how many tools?") and drill into one ("show me every field this
agent declares") without opening the ``.md`` by hand. They are deliberately
**non-interactive** (no :class:`~calfcord.cli._prompts.Prompter`): both are pure
functions of ``agents_dir`` + a name, so they compose into scripts and pipes and
stay trivially testable.

Both render through the shared :data:`calfcord.cli._fields.FIELDS` registry and
:func:`calfcord.cli._fields.render_value`, the same source the ``edit`` menu and
``set`` command consume. Routing the *display* of a field through the same
registry that *edits* it is what stops ``show`` from listing a field ``set``
can't write (or omitting one it can) — the two surfaces can't drift because
there is only one list.

The skip rules match :func:`calfcord.cli._agents.detect_agents` (dotfiles and
``*.template.md`` templates are not live agents), so ``list`` reports exactly the
set ``calfkit-agent`` would run. An individual ``.md`` that fails to parse is
*noted* rather than fatal: one malformed file must not blank the whole listing.

This module imports only the lightweight ``calfcord.agents`` / ``calfcord.cli``
seams (no provider SDK), so it stays importable from the argparse entry point.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from calfcord.agents.definition import parse_agent_md
from calfcord.cli._agents import detect_agents
from calfcord.cli._fields import FIELDS, render_value, truncate

if TYPE_CHECKING:
    from pathlib import Path

    from calfcord.agents.definition import AgentDefinition

# The DESCRIPTION column in the ``list`` table is truncated to this many
# characters so a near-100-char description (Discord's slash limit) can't wrap
# the row on a typical terminal. The full text is always available via
# ``show``/``--json``.
_DESCRIPTION_TRUNCATE = 48

# How many leading characters of the system-prompt body ``show`` previews in its
# human form. Long enough to recognize which prompt is loaded; the full body is
# in ``--json``.
_PROMPT_PREVIEW_LEN = 200


def _tools_summary(tools: tuple[str, ...] | None) -> str:
    """Summarize an agent's ``tools`` for the ``list`` table's TOOLS column.

    Mirrors the loader's omitted/empty/explicit semantics
    (:attr:`AgentDefinition.tools`): ``None`` (frontmatter ``tools:`` omitted)
    means "all builtins" → ``"all"``; an explicit empty tuple is the deliberate
    no-tools opt-out → ``"0"``; otherwise the count. A scalar the operator can
    scan beats a wrapping comma list in a table cell.
    """
    if tools is None:
        return "all"
    return str(len(tools))


def _provider_model(defn: AgentDefinition) -> str:
    """Render ``provider·model`` for the ``list`` table, or ``"(default)"``.

    An agent that declares neither provider nor model inherits the install
    default at build time; showing ``"(default)"`` rather than two empty cells
    makes that explicit. When only one half is set, the set half is shown and
    the other reads ``(default)`` so the operator sees exactly what's declared.
    """
    if defn.provider is None and defn.model is None:
        return "(default)"
    provider = defn.provider or "(default)"
    model = defn.model or "(default)"
    return f"{provider}·{model}"


def _list_row(defn: AgentDefinition) -> dict[str, Any]:
    """Build the per-agent JSON object for ``list --json``.

    ``tools`` is serialized as the concrete list (or ``null`` for the implicit
    "all builtins" default) rather than the table's summary string, so the JSON
    consumer gets the structured value while the human table gets the scannable
    count. This is the machine-readable counterpart of one table row.
    """
    return {
        "name": defn.agent_id,
        "provider": defn.provider,
        "model": defn.model,
        "tools": list(defn.tools) if defn.tools is not None else None,
        "description": defn.description,
    }


def _parse_all(agents_dir: Path) -> tuple[list[AgentDefinition], list[str]]:
    """Parse every detected agent, returning ``(parsed, failed_names)``.

    A single malformed ``.md`` must not blank the whole listing, so a parse
    failure is collected into ``failed_names`` (for a trailing note) rather than
    raised. The parsed list preserves :func:`detect_agents`' sorted order for
    deterministic output.
    """
    parsed: list[AgentDefinition] = []
    failed: list[str] = []
    for name in detect_agents(agents_dir):
        try:
            parsed.append(parse_agent_md(agents_dir / f"{name}.md"))
        except (ValueError, OSError):
            failed.append(name)
    return parsed, failed


def run_list(agents_dir: Path, *, as_json: bool = False) -> int:
    """``disco agent list``: survey every agent in ``agents_dir``.

    Parses each detected agent (skipping dotfiles / ``*.template.md`` templates,
    matching the loader) and prints either an aligned NAME / PROVIDER·MODEL /
    TOOLS / DESCRIPTION table (human) or a JSON array of agent objects
    (``--json``). A ``.md`` that fails to parse is noted on stderr-style trailing
    line in the human form (and simply omitted from the JSON array) so one broken
    file doesn't hide the rest. An empty directory prints a friendly line (human)
    or ``[]`` (JSON). Always returns 0 — listing is read-only and "no agents" is
    a valid state, not an error.
    """
    parsed, failed = _parse_all(agents_dir)

    if as_json:
        print(json.dumps([_list_row(d) for d in parsed], indent=2))
        return 0

    if not parsed:
        print(f"no agents in {agents_dir}")
        if failed:
            print(f"(skipped {len(failed)} unparseable file(s): {', '.join(failed)})")
        return 0

    _print_table(parsed)
    if failed:
        print(f"(skipped {len(failed)} unparseable file(s): {', '.join(failed)})")
    return 0


def _print_table(agents: list[AgentDefinition]) -> None:
    """Print the aligned ``list`` table for ``agents``.

    Column widths are computed from the data (and the headers) so the table
    aligns regardless of name/model lengths, then each row is left-justified into
    those widths. The DESCRIPTION column is truncated last (its value can be the
    longest) so it never forces a wide, wrapping table.
    """
    headers = ("NAME", "PROVIDER·MODEL", "TOOLS", "DESCRIPTION")
    rows = [
        (
            d.agent_id,
            _provider_model(d),
            _tools_summary(d.tools),
            truncate(d.description, _DESCRIPTION_TRUNCATE),
        )
        for d in agents
    ]

    widths = [
        max(len(headers[col]), *(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*row))


def run_show(agents_dir: Path, name: str, *, as_json: bool = False) -> int:
    """``disco agent show <name>``: print one agent's full configuration.

    Parses ``agents_dir/<name>.md``; a missing or unparseable file prints an
    ``error: ...`` line and returns 1 (operator-recoverable, per the CLI
    convention — no traceback escapes). In human form it prints the name + file
    path, then every :data:`FIELDS` row rendered through
    :func:`render_value` (the same renderer the edit menu uses, so ``show`` and
    ``edit`` can't disagree), plus a short system-prompt preview. ``--json``
    emits the full config object — name, every editable field, and the complete
    ``system_prompt`` body — so it round-trips into tooling without the human
    previews' truncation. Returns 0 on success.
    """
    md_path = agents_dir / f"{name}.md"
    try:
        defn = parse_agent_md(md_path)
    except (ValueError, OSError) as e:
        print(f"error: cannot read agent {name!r}: {e}")
        return 1

    if as_json:
        print(json.dumps(_show_object(defn), indent=2))
        return 0

    print(f"{defn.agent_id}  ({md_path})")
    label_width = max(len(field.label) for field in FIELDS)
    for field in FIELDS:
        print(f"  {field.label:<{label_width}}  {render_value(defn, field)}")
    print()
    print("System prompt:")
    print(f"  {truncate(defn.system_prompt, _PROMPT_PREVIEW_LEN)}")
    return 0


def _show_object(defn: AgentDefinition) -> dict[str, Any]:
    """Build the full-config JSON object for ``show --json``.

    Includes ``name`` plus every editable field and the complete
    ``system_prompt`` body (not the human preview) so the output is a faithful,
    machine-readable image of the agent's declaration. ``tools`` is the concrete
    list (or ``null`` for the implicit all-builtins default), preserving the
    omitted/empty distinction the table's summary string would lose.
    """
    return {
        "name": defn.agent_id,
        "description": defn.description,
        "provider": defn.provider,
        "model": defn.model,
        "tools": list(defn.tools) if defn.tools is not None else None,
        "thinking_effort": defn.thinking_effort,
        "memory": defn.memory,
        "system_prompt": defn.system_prompt,
    }
