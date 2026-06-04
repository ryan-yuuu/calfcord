"""Shared agent-directory inspection and ``.md`` write helpers for the CLI.

Both ``calfcord init`` (which *reports* the agents an install would load) and
``calfcord agent tools`` (which *picks* one to edit) need the same answer to
"which ``.md`` files are live agents?". Factoring :func:`detect_agents` here
keeps that one definition from drifting between the two callers — a mismatch
would let ``init`` report an agent the editor can't open, or vice versa.

This module also owns the agent-file *write* and *identity* helpers that both
``calfcord init`` (first-run setup) and ``calfcord agent create`` build on:
slugifying a typed name into a valid stem, deriving a default display name and
body, the create/update :func:`write_agent` path, and the tools-checkbox
builder :func:`pick_tools`. They live here rather than in ``init`` so the two
commands share one implementation and can't drift.

The skip rules in :func:`detect_agents` mirror the loader's
(:func:`calfcord.agents.loader.load_agents_dir`): dot-prefixed files and
``*.template.md`` reference templates are not live agents, so the names returned
here match exactly what ``calfkit-agent`` would run.

Decoupling invariant: this module must NOT pull ``calfcord.mcp.servers`` (MCP
transport + secrets) at import time — that wiring is bridge-only. (It does
transitively import a provider SDK via ``calfcord.agents``'s package init, so
the import graph is not provider-free; the invariant that holds and matters is
the ``mcp.servers`` one.) :func:`pick_tools` still defers its ``TOOL_REGISTRY``
/ ``discover_mcp_catalog`` imports into the function body so enumeration goes
through schema-only seams that never touch MCP transport or secrets.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from calfcord.agents import md_writer
from calfcord.agents.definition import AgentDefinition, parse_agent_md
from calfcord.agents.identifier import AGENT_ID_PATTERN

if TYPE_CHECKING:
    from calfcord.cli._prompts import Prompter

logger = logging.getLogger(__name__)

# The starter agent's name and the *exact* description the installer seeds it
# with. The prune-pristine check in :func:`write_agent` keys off this string:
# an ``assistant.md`` still carrying it is an untouched seed (safe to remove
# when the operator names a different agent); any other description means the
# operator customized it and it must be preserved.
STARTER_AGENT_NAME = "assistant"
DEFAULT_DESCRIPTION = "General-purpose AI teammate — answers questions and helps with tasks."

# Tools that grant shell / filesystem-write reach into the ``calfkit-tools``
# launch directory. Selecting any of them drives the one-line security caution,
# because anyone who can @mention the agent can then drive them.
_DANGEROUS_TOOLS = frozenset({"shell", "write_file", "edit_file"})

# Permitted characters for an agent-name *stem*. The on-disk identifier must
# satisfy ``AgentDefinition.agent_id`` (``[a-z0-9_-]{1,32}``); we slugify toward
# that here so a friendly typed name ("My Helper") becomes a valid stem
# ("my_helper") rather than failing validation at write time.
_STEM_INVALID = re.compile(r"[^a-z0-9_-]+")


def detect_agents(agents_dir: Path) -> list[str]:
    """Return the agent names (``.md`` stems) ``agents_dir`` would load, sorted.

    Returns an empty list when ``agents_dir`` is not an existing directory, so
    callers can treat "no dir" and "empty dir" identically — both mean "no
    agents to act on". Dotfiles and ``*.template.md`` templates are skipped to
    match the loader; the result is sorted for deterministic prompts/output.
    """
    if not agents_dir.is_dir():
        return []
    return sorted(
        p.stem
        for p in agents_dir.glob("*.md")
        if not p.name.startswith(".") and not p.name.endswith(".template.md")
    )


def slug_stem(raw: str) -> str:
    """Coerce a typed agent name into a safe ``.md`` filename stem.

    The frontmatter ``name`` (and thus the filename) must match
    ``[a-z0-9_-]{1,32}``; an operator typing "My Helper" should not hit a
    validation error, so we lowercase, turn runs of disallowed characters into
    single underscores, and trim the result. A name that slugifies to nothing
    (e.g. all punctuation) falls back to the starter name rather than producing
    an empty, invalid stem — keeping the wizard moving instead of aborting.
    """
    slug = _STEM_INVALID.sub("_", raw.strip().lower()).strip("_-")
    slug = slug[:32].strip("_-")
    result = slug or STARTER_AGENT_NAME
    # Postcondition: the stem is a valid agent id, so callers can write it as a
    # filename without a second validation. The fallback guarantees non-empty,
    # and slugification guarantees the charset/length, so this can only fire on
    # a bug in those rules — assert rather than silently emit an invalid stem.
    assert AGENT_ID_PATTERN.fullmatch(result), f"slugified stem {result!r} is not a valid agent id"
    return result


def existing_agent(agents_dir: Path, name: str) -> AgentDefinition | None:
    """Return the parsed agent at ``agents_dir/<name>.md`` if it parses, else ``None``.

    Used purely to pre-fill the description/model prompts with the operator's
    current values on a re-run that targets an existing agent. A missing or
    malformed file is not an error here — we simply offer the defaults — so the
    parse is guarded; the actual write path validates strictly.
    """
    target = agents_dir / f"{name}.md"
    if not target.is_file():
        return None
    try:
        return parse_agent_md(target)
    except (ValueError, OSError):
        return None


def display_name_for(name: str) -> str:
    """Derive a sane default ``display_name`` from the agent's slug stem.

    Title-casing the underscore/dash-split stem ("my_helper" → "My Helper")
    gives a human-friendly default the operator can refine later, without
    forcing a separate prompt during first-run setup.
    """
    return name.replace("_", " ").replace("-", " ").title()


def agent_body(display_name: str) -> str:
    """Render the generic system-prompt body for a brand-new agent.

    A minimal, generic prompt so the agent answers sensibly from the first boot
    without further editing. Kept separate from the frontmatter so the create
    path can serialize identity fields through ``frontmatter.dumps`` (which
    YAML-quotes free-text values safely) rather than string interpolation.
    """
    return (
        f"You are {display_name}, a helpful AI teammate in this Discord workspace. Answer\n"
        "questions and help with tasks clearly and concisely. If you don't know something,\n"
        "say so rather than guessing.\n"
    )


def atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via a same-dir tmp file + atomic rename.

    A partial agent file would make the next ``calfkit-agent`` boot fail to
    load the directory, so the create path must never leave a half-written file
    behind on error. ``path.parent`` is created first because a fresh install's
    ``agents/`` directory may not exist yet (unlike :mod:`md_writer`'s in-place
    rewrite, which can assume the file — and so the dir — already exists).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def is_pristine_seed(agents_dir: Path) -> bool:
    """True when ``agents_dir/assistant.md`` is the untouched seeded starter.

    "Untouched" is detected by the seed's two stable identity markers: its
    ``agent_id`` is still ``assistant`` and its description is still the exact
    seed default. If the operator customized the description, both halves no
    longer hold and the file must be preserved. A missing or malformed
    ``assistant.md`` is treated as "not a pristine seed" (nothing safe to
    prune), so the parse is guarded — a broken file is never deleted on a guess.
    """
    seed = agents_dir / f"{STARTER_AGENT_NAME}.md"
    if not seed.is_file():
        return False
    try:
        parsed = parse_agent_md(seed)
    except (ValueError, OSError):
        return False
    return parsed.agent_id == STARTER_AGENT_NAME and parsed.description == DEFAULT_DESCRIPTION


def write_agent(
    agents_dir: Path,
    *,
    name: str,
    description: str,
    provider: str,
    model: str,
    tools: list[str],
    prune_seed: bool = False,
) -> Path:
    """Create or update ``agents_dir/<name>.md`` for the wizard's agent.

    Two paths, both validate-before-write so a bad value never lands on disk:

    * **Target exists** — update the agent in place, preserving its body and
      ``display_name``: rewrite ``description``/``provider``/``model`` via
      :func:`md_writer._update_fields`, then the tool list via
      :func:`md_writer.update_tools`. Both are validated-atomic, so a bad value
      leaves the file untouched.
    * **Target missing** — build the frontmatter as a mapping and serialize it
      with :func:`frontmatter.dumps` (NOT string interpolation), which
      YAML-quotes free-text values so a description like ``"Calendar: book
      meetings"`` or one carrying quotes/``#``/leading punctuation can't corrupt
      the file. The synthetic :class:`AgentDefinition` is built *first* (mirroring
      :func:`md_writer._update_fields`), so an invalid value raises before any
      disk write. After the atomic write, when ``prune_seed`` is set and the
      operator named a *different* agent (``name != "assistant"``) on an install
      still carrying the *pristine* seeded ``assistant.md``, that seed is deleted
      so they end with one clean agent. ``init`` opts in for its first-run setup;
      ``agent create`` leaves it off so a second agent never removes the starter.
      A *customized* ``assistant.md`` (or naming the agent ``assistant`` itself)
      is never deleted.

    Raises:
        ValueError: a field value fails :class:`AgentDefinition` validation
            (create path) or the existing ``.md``/new value is invalid (update
            path). No partial file is written.
        OSError: a filesystem error during the atomic write. No partial file is
            written.
    """
    target = agents_dir / f"{name}.md"

    if target.exists():
        md_writer._update_fields(target, {"description": description, "provider": provider, "model": model})
        md_writer.update_tools(target, tools)
        return target

    display_name = display_name_for(name)
    body = agent_body(display_name)
    metadata = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "provider": provider,
        "model": model,
        "tools": list(tools),
    }
    # Validate the full definition in memory FIRST (mirrors
    # md_writer._update_fields): a bad free-text value raises here, before any
    # bytes touch disk, so the create path can never leave an unloadable file.
    AgentDefinition(**{**metadata, "system_prompt": body, "source_path": target})

    payload = frontmatter.dumps(frontmatter.Post(body, **metadata))
    if not payload.endswith("\n"):
        payload += "\n"
    atomic_write(target, payload)

    # Prune the pristine starter only when the caller opted in (``init``'s
    # first-run "one clean agent" goal) and a *different* agent was created;
    # naming the agent ``assistant`` would have hit the update path above.
    # ``agent create`` leaves ``prune_seed`` False so adding a second agent
    # never deletes the operator's starter.
    if prune_seed and name != STARTER_AGENT_NAME and is_pristine_seed(agents_dir):
        (agents_dir / f"{STARTER_AGENT_NAME}.md").unlink(missing_ok=True)
        logger.info("pruned pristine seed assistant.md after creating %s", target)

    return target


def pick_tools(prompter: Prompter, name: str) -> list[str]:
    """Prompt for the agent's tools and return the selected tokens.

    Every builtin (sorted :data:`calfcord.tools.TOOL_REGISTRY`) is offered
    pre-checked so the default is the same "all builtins" set a frontmatter that
    omits ``tools:`` would expand to; MCP selectors discovered from the committed
    schemas are offered *un*-checked (opt-in). Enumeration uses only the
    schema-only seams — ``TOOL_REGISTRY`` and ``discover_mcp_catalog`` — and
    never touches ``calfcord.mcp.servers`` (transport/secrets), so this works on
    a host that holds no MCP credentials. If a write/shell tool ends up selected
    we print the security caution, because anyone who can @mention the agent can
    then drive it.
    """
    from calfcord.cli._prompts import Choice
    from calfcord.cli.agent_tools import first_line
    from calfcord.mcp import schemas as schemas_pkg
    from calfcord.mcp.discovery import discover_mcp_catalog
    from calfcord.tools import TOOL_REGISTRY

    choices: list[Choice] = []
    for tool_name in sorted(TOOL_REGISTRY):
        summary = first_line(TOOL_REGISTRY[tool_name].tool_schema.description)
        label = f"{tool_name} — {summary}" if summary else tool_name
        choices.append(Choice(tool_name, label, True))

    try:
        catalog = discover_mcp_catalog(schemas_pkg)
    except Exception as e:
        # A broken generated schema must not brick setup: degrade to builtins
        # only, loudly, so the operator sees the cause. Broad on purpose — a
        # corrupt generated module can raise SyntaxError / AttributeError too,
        # not just ImportError / ValueError, and none of them should abort setup.
        print(f"warning: MCP catalog failed to load, showing builtins only: {e}")
        catalog = {}

    for server in sorted(catalog):
        tools = catalog[server]
        all_selector = f"mcp/{server}"
        choices.append(Choice(all_selector, f"{all_selector} — all {len(tools)} tools", False))
        for tool in tools:
            selector = f"mcp/{server}/{tool.name}"
            summary = first_line(getattr(tool, "description", None))
            label = f"{selector} — {summary}" if summary else selector
            choices.append(Choice(selector, label, False))

    selected = prompter.checkbox(
        f"Tools for {name} (all selected — deselect any you don't want):",
        choices,
        instruction="space toggles, enter confirms",
    )

    if _DANGEROUS_TOOLS.intersection(selected):
        print(
            "note: these tools include shell + file write access in the calfkit-tools "
            "launch dir, drivable by anyone who can @mention this agent — keep the bot "
            "off public Discord (docs/security.md §3.4)."
        )

    return selected
