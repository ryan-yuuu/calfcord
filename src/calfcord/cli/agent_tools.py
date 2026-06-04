"""``calfcord agent tools [<name>]`` — interactive editor for an agent's tools.

Picks an agent ``.md`` (by name, or via a prompt over the install's agents
dir), shows a multi-select checkbox of every builtin tool plus every MCP
selector the committed schemas expose, pre-checked from the agent's current
``tools:`` declaration, and writes the operator's selection back through the
validated-atomic :func:`calfcord.agents.md_writer.update_tools`.

Two design constraints shape the flow:

* **It reads the RAW declaration, not the loader's expansion.** It calls
  :func:`calfcord.agents.definition.parse_agent_md` directly (not the loader's
  default-resolving path) so it can distinguish ``tools:`` *omitted* (``None``
  → implicitly "all builtins") from ``tools: []`` (explicitly none). The
  implicit-all case is converted into explicit checks here, and the write
  always persists an explicit list, so on-disk state stops being ambiguous
  after the first save.

* **It honours the decoupling invariant.** Enumeration goes through the
  schema-only seams — :data:`calfcord.tools.TOOL_REGISTRY`,
  :func:`calfcord.mcp.discovery.discover_mcp_catalog`, and the ``mcp/`` selector
  grammar — and never imports ``calfcord.mcp.servers`` (transport + secrets).
  A host with no MCP schemas shows builtins and a one-line hint, plus a
  pre-checked "kept" row for any configured token it can't enumerate so a
  selector the host can't see is preserved rather than silently dropped (see
  :func:`_build_choices`).

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


def _build_choices(current: set[str]) -> tuple[list[Choice], bool]:
    """Build the checkbox :class:`Choice` rows from the tool universe.

    Builtins come first (each ``name`` checked iff it is in ``current``), then,
    per MCP server, an ``mcp/<server>`` "all tools" row followed by one
    ``mcp/<server>/<tool>`` row per tool — exactly the selector grammar
    :func:`calfcord.agents.md_writer.update_tools` validates, so anything the
    operator can tick is something the editor can persist.

    Enumeration uses only the schema-only seams (``TOOL_REGISTRY`` +
    ``discover_mcp_catalog``); ``calfcord.mcp.servers`` (transport/secrets) is
    never imported, so this works on a host that holds no MCP credentials.

    Two degradations keep a partial environment usable rather than silently
    lossy:

    * **Empty / unenumerable MCP catalog.** When the host ships no MCP schemas
      (the common case — this repo commits none), ``discover_mcp_catalog``
      returns ``{}`` and only builtins are enumerated. A configured token like
      ``mcp/gmail`` therefore isn't among the enumerated rows; rather than drop
      it on save, every ``current`` token not covered by an enumerated row is
      appended as a **pre-checked** "kept" row (a trailing group) so confirming
      preserves it and the operator can still uncheck to remove it.
    * **Broken generated schema.** If ``discover_mcp_catalog`` itself raises
      (an ``ImportError``/``ValueError`` from a malformed generated module), we
      print a loud ``warning:`` and fall back to builtins-only instead of
      letting one bad schema brick the whole editor — the kept-token group then
      preserves any MCP selectors as above.

    Returns the rows plus a flag for whether the MCP catalog was empty (or
    failed to load), so :func:`run` can print the codegen hint without
    re-walking the catalog.
    """
    from calfcord.mcp import schemas as schemas_pkg
    from calfcord.mcp.discovery import discover_mcp_catalog
    from calfcord.tools import TOOL_REGISTRY

    choices: list[Choice] = []

    for name in sorted(TOOL_REGISTRY):
        summary = first_line(TOOL_REGISTRY[name].tool_schema.description)
        label = f"{name} — {summary}" if summary else name
        choices.append(Choice(name, label, name in current))

    try:
        catalog = discover_mcp_catalog(schemas_pkg)
    except (ImportError, ValueError) as e:
        # A broken generated schema module must not brick the editor: degrade to
        # builtins-only, loudly, so the operator sees the cause. Kept-token rows
        # below still preserve any configured MCP selectors.
        print(f"warning: MCP catalog failed to load, showing builtins only: {e}")
        catalog = {}

    for server in sorted(catalog):
        tools = catalog[server]
        all_selector = f"mcp/{server}"
        choices.append(Choice(all_selector, f"{all_selector} — all {len(tools)} tools", all_selector in current))
        for tool in tools:
            selector = f"mcp/{server}/{tool.name}"
            summary = first_line(getattr(tool, "description", None))
            label = f"{selector} — {summary}" if summary else selector
            choices.append(Choice(selector, label, selector in current))

    # Preserve anything the agent already has that we couldn't enumerate on this
    # host (typically MCP selectors when no schemas are present). Pre-checked so
    # confirming keeps them; the operator can uncheck to drop one.
    enumerated = {c.value for c in choices}
    for token in sorted(current - enumerated):
        choices.append(Choice(token, f"{token}  (kept — not available on this host)", True))

    return choices, not catalog


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
        # builtins (not MCP selectors), matching the loader's default expansion.
        from calfcord.tools import TOOL_REGISTRY

        current = set(TOOL_REGISTRY)

    choices, mcp_empty = _build_choices(current)
    if mcp_empty:
        print("(no MCP tools; run `calfcord-mcp-codegen <server>` to add some)")

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
