"""Shared agent-directory inspection for the management CLI commands.

Both ``calfcord init`` (which *reports* the agents an install would load) and
``calfcord agent tools`` (which *picks* one to edit) need the same answer to
"which ``.md`` files are live agents?". Factoring the detection here keeps that
one definition from drifting between the two callers — a mismatch would let
``init`` report an agent the editor can't open, or vice versa.

The skip rules mirror the loader's (:func:`calfcord.agents.loader.load_agents_dir`):
dot-prefixed files and ``*.template.md`` reference templates are not live agents,
so the names returned here match exactly what ``calfkit-agent`` would run.
"""

from __future__ import annotations

from pathlib import Path


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
