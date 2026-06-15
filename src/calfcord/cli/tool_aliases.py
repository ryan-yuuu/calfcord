"""Handlers for ``calfcord tools alias`` — operator-managed tool aliases.

Aliasing (``CALFCORD_TOOLS_ALIAS``, e.g. ``terminal`` → ``terminal_eu`` for
multi-host routing) is install config: these handlers are a validated editor of
the ``CALFCORD_TOOLS_ALIAS`` line in the install ``.env``, which every role
reads at boot. There is no runtime change — the var is already consumed by
``apply_deploy_filters``. See ``docs/design/tool-alias-cli.md`` and
[ADR-0007](../../../docs/adr/0007-tool-alias-cli-config.md).

Mirrors the ``calfcord mcp add/list/remove`` idiom in
:mod:`calfcord.cli.mcp_admin`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from calfcord.cli._envfile import read_env, upsert
from calfcord.tools.deploy_filters import (
    parse_alias_csv,
    serialize_alias_map,
    validate_alias,
)

_ALIAS_ENV = "CALFCORD_TOOLS_ALIAS"
"""The install-``.env`` key these handlers manage. Mirrors
``calfcord.tools.deploy_filters._ALIAS_ENV`` (the var the runtime reads)."""


def _read_aliases(env_path: Path) -> dict[str, str]:
    """Parse the current ``CALFCORD_TOOLS_ALIAS`` value (raises on malformed)."""
    return parse_alias_csv(read_env(env_path).get(_ALIAS_ENV, ""))


def _write_aliases(env_path: Path, aliases: Mapping[str, str]) -> None:
    """Persist ``aliases`` to the install ``.env`` (empty value when none)."""
    upsert(env_path, {_ALIAS_ENV: serialize_alias_map(aliases)})


def _print_restart_hint() -> None:
    print(
        "restart the tools host and agents to apply: "
        "`calfcord tools restart` and `calfcord agent restart --all` "
        "(or `calfcord stop && calfcord start`)."
    )


def run_alias_add(
    *,
    env_path: Path,
    src: str,
    dst: str,
    tool_names: Collection[str],
    aliasable_names: Collection[str],
    apply_restart: Callable[[], None] | None = None,
) -> int:
    """Add an alias ``src`` → ``dst`` to the install ``.env`` (validated).

    Returns 0 on success (or an idempotent no-op), 1 on any validation/parse
    error (nothing written). On an actual change, ``apply_restart`` (when given,
    i.e. ``--restart``) is called to apply it; otherwise the restart hint is
    printed. Neither fires on a no-op or error.
    """
    try:
        aliases = _read_aliases(env_path)
    except ValueError as exc:
        print(f"error: existing {_ALIAS_ENV} is malformed: {exc}")
        return 1

    if aliases.get(src) == dst:
        print(f"alias {src!r} → {dst!r} already configured in {env_path}")
        return 0

    try:
        validate_alias(
            src,
            dst,
            tool_names=tool_names,
            aliasable_names=aliasable_names,
            existing=aliases,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    aliases[src] = dst
    _write_aliases(env_path, aliases)
    print(f"aliased {src!r} → {dst!r} in {env_path}")
    (apply_restart or _print_restart_hint)()
    return 0


def run_alias_list(*, env_path: Path) -> int:
    """Print configured aliases (``src → dst``), or a hint when none."""
    try:
        aliases = _read_aliases(env_path)
    except ValueError as exc:
        print(f"error: existing {_ALIAS_ENV} is malformed: {exc}")
        return 1
    if not aliases:
        print(
            f"no tool aliases configured in {env_path}; add one with "
            "`calfcord tools alias add <tool> <new-name>`"
        )
        return 0
    for src, dst in sorted(aliases.items()):
        print(f"{src}  →  {dst}")
    return 0


def run_alias_remove(
    *,
    env_path: Path,
    dst: str,
    apply_restart: Callable[[], None] | None = None,
) -> int:
    """Remove the alias whose target (new name) is ``dst``.

    Returns 0 on success, 1 when no alias has that target (or a parse error).
    On an actual change, ``apply_restart`` (when given) is called; otherwise the
    restart hint is printed.
    """
    try:
        aliases = _read_aliases(env_path)
    except ValueError as exc:
        print(f"error: existing {_ALIAS_ENV} is malformed: {exc}")
        return 1

    src = next((s for s, d in aliases.items() if d == dst), None)
    if src is None:
        configured = ", ".join(sorted(aliases.values())) or "(none)"
        print(f"error: no alias {dst!r} configured; current targets: {configured}")
        return 1

    del aliases[src]
    _write_aliases(env_path, aliases)
    print(f"removed alias {dst!r} (was {src!r} → {dst!r})")
    (apply_restart or _print_restart_hint)()
    return 0
