"""``calfcord-package-tools`` — build a slim image hosting only specific tools.

Walkthrough:
    1. Parse args. Names are positional; ``--tag`` is required.
       Optional repeatable ``--rename SRC=DST`` aliases a tool to a
       new schema name in the resulting image (multi-host deployment
       of the same tool — e.g. ``terminal`` running on a workstation
       AND an EU VM, with the EU host renamed to ``terminal_eu``).
    2. Validate the names against the live ``TOOL_REGISTRY``. Unknown
       names fail-fast with the full known list (mirrors the
       agent-factory's tool-resolution error message).
    3. Validate each ``--rename SRC=DST``: ``SRC`` must be a real tool,
       ``DST`` must match the tool-name regex and not collide with
       another tool or another rename target.
    4. Translate positional names through the alias map. An operator
       who typed ``terminal --rename terminal=terminal_eu`` means
       "host the terminal tool but expose it as terminal_eu" — the
       filter that lands in the image must be the POST-rename name,
       otherwise discovery would drop the alias clone and keep the
       original.
    5. Generate the Dockerfile via the templater. The image bakes
       ``CALFCORD_TOOLS_INCLUDE`` plus an optional
       ``CALFCORD_TOOLS_ALIAS`` so ``apply_deploy_filters`` narrows
       registration to just the listed names (and clones renamed
       tools under their new identity).
    6. Write the Dockerfile to a tempdir; invoke ``docker buildx build``.
    7. On success, print the local tag plus follow-up commands the
       operator typically wants. On failure, leave the Dockerfile in
       place and print its path.

No registry push: the CLI builds locally with ``--tag``; the operator
runs ``docker push`` afterward against whichever registry they own.
"""

from __future__ import annotations

import os
import sys

# Strip deploy-time runtime env vars from the operator's shell BEFORE
# anything imports ``calfcord.tools``. The CLI validates
# ``--rename`` targets against the canonical ``TOOL_REGISTRY``; if the
# operator already has ``CALFCORD_TOOLS_ALIAS`` or
# ``CALFCORD_TOOLS_INCLUDE`` set in their shell, the registry imported
# below would reflect the operator's env rather than the codebase's
# canonical surface, and validation would pass against a poisoned
# baseline (e.g. ``--rename anything=foo`` would succeed because the
# operator's env already cloned ``foo``). The image would then fail on
# a fresh host. ``pop`` here is correct: these env vars are
# DEPLOY-time runtime configuration that should never apply at
# BUILD-time validation.
os.environ.pop("CALFCORD_TOOLS_ALIAS", None)
os.environ.pop("CALFCORD_TOOLS_INCLUDE", None)

from calfcord.packaging._build import make_parser, repo_root, run_build
from calfcord.packaging.dockerfile import render_tools_dockerfile
from calfcord.tools.deploy_filters import TOOL_NAME_REGEX


def _validate_tool_names(names: list[str]) -> list[str]:
    """Check each name resolves to a real entry in ``TOOL_REGISTRY``.

    Returns the de-duplicated, order-preserving list on success.
    Calls :func:`sys.exit` with code 2 (the argparse-standard "usage
    error" code) if any name is unknown, after printing the full
    known list for forensic value.

    Importing :data:`TOOL_REGISTRY` here composes the registry in
    ``tools/__init__.py`` (importing the vendored ``calfkit-tools``
    nodes); that's fine — the CLI runs short-lived so the boot cost is
    paid once per build invocation.
    """
    # Local import keeps ``--help`` cheap (no vendored calfkit-tools node
    # imports just to display usage text).
    from calfcord.tools import TOOL_REGISTRY

    known = sorted(TOOL_REGISTRY)
    unknown = [name for name in names if name not in TOOL_REGISTRY]
    if unknown:
        sys.stderr.write(
            f"error: unknown tool(s) {unknown!r}\n"
            f"  Known tools: {', '.join(known)}\n"
        )
        sys.exit(2)
    # De-dupe while preserving caller's order (deterministic Dockerfile
    # output regardless of how the user typed the args).
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _parse_and_validate_renames(rename_args: list[str]) -> dict[str, str]:
    """Parse ``--rename SRC=DST`` flags into a ``{src: dst}`` dict.

    Each entry must:
      * Split cleanly on the first ``=`` into non-empty SRC and DST.
      * Have SRC in the live ``TOOL_REGISTRY`` (catches typos at
        build time rather than at deploy time).
      * Have DST matching :data:`TOOL_NAME_REGEX` (shared char set
        between Anthropic and OpenAI; the length bound is Anthropic's
        128 — operators targeting OpenAI-only deployments should keep
        targets ≤ 64 chars).
      * SRC != DST (no-op aliasing).
      * DST not colliding with another tool name in ``TOOL_REGISTRY``
        (would shadow the audited tool — refuse).
      * Each SRC and each DST appearing at most once across all
        ``--rename`` flags (v1 supports one alias per source and one
        source per target; multi-region from a single image is a
        future feature).

    Any failure prints an actionable message and exits 2. Returns the
    parsed map on success.
    """
    from calfcord.tools import TOOL_REGISTRY

    result: dict[str, str] = {}
    used_targets: set[str] = set()
    known = sorted(TOOL_REGISTRY)
    for raw in rename_args:
        if "=" not in raw:
            sys.stderr.write(
                f"error: --rename expects SRC=DST, got {raw!r}\n"
            )
            sys.exit(2)
        src, _, dst = raw.partition("=")
        src = src.strip()
        dst = dst.strip()
        if not src or not dst:
            sys.stderr.write(
                f"error: --rename {raw!r} has empty SRC or DST\n"
            )
            sys.exit(2)
        if not TOOL_NAME_REGEX.match(dst):
            sys.stderr.write(
                f"error: --rename target {dst!r} is not a valid tool name; "
                f"must match {TOOL_NAME_REGEX.pattern}\n"
            )
            sys.exit(2)
        if src not in TOOL_REGISTRY:
            sys.stderr.write(
                f"error: --rename source {src!r} is not a known tool\n"
                f"  Known tools: {', '.join(known)}\n"
            )
            sys.exit(2)
        if src == dst:
            sys.stderr.write(
                f"error: --rename {raw!r} aliases a tool to itself; "
                f"either drop the flag or pick a distinct target name\n"
            )
            sys.exit(2)
        if dst in TOOL_REGISTRY:
            sys.stderr.write(
                f"error: --rename target {dst!r} collides with an existing "
                f"tool name. Renaming over an audited tool would silently "
                f"shadow it; pick a unique target.\n"
            )
            sys.exit(2)
        if src in result:
            sys.stderr.write(
                f"error: --rename source {src!r} is aliased multiple times; "
                f"only one alias per source is supported\n"
                f"  All --rename args: {rename_args!r}\n"
            )
            sys.exit(2)
        if dst in used_targets:
            sys.stderr.write(
                f"error: --rename target {dst!r} is used by multiple "
                f"renames; only one source may alias to a given target\n"
                f"  All --rename args: {rename_args!r}\n"
            )
            sys.exit(2)
        result[src] = dst
        used_targets.add(dst)
    return result


def _check_aliases_referenced(
    aliases: dict[str, str], include_tools: list[str]
) -> None:
    """Refuse builds where a --rename source isn't in the positional list.

    The CLI's positional-name translation maps each include name
    through the alias map before baking ``CALFCORD_TOOLS_INCLUDE``.
    If the operator typed ``--rename terminal=terminal_eu`` but
    didn't add ``terminal`` to the positional list, the alias bakes
    into the image's ``CALFCORD_TOOLS_ALIAS`` env BUT the include
    filter doesn't reference ``terminal_eu`` (the post-rename name)
    — ``apply_deploy_filters`` at boot would add the clone, then
    immediately drop it via the filter. The image then has rename env doing nothing
    (visible in ``docker inspect`` as live config but inert at
    runtime) — silent dead config.

    Refusing at build time short-circuits the "deploy, debug, why
    didn't it work" cycle.
    """
    unused = sorted(set(aliases) - set(include_tools))
    if unused:
        sys.stderr.write(
            f"error: --rename source(s) {unused!r} not in positional tool "
            f"list {include_tools!r}.\n"
            f"  An alias whose source isn't in the include filter is dead "
            f"config — the clone gets registered then dropped at boot. Add "
            f"the source(s) to the positional list, or remove the unused "
            f"--rename flag(s).\n"
        )
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    parser = make_parser(
        prog="calfcord-package-tools",
        item_kind="tool",
        item_examples="terminal search_files",
    )
    # The ``--rename`` flag is specific to tool images — agent .md
    # files carry identity-bound fields (slash command, display name)
    # that don't safely rename at deploy time, so the shared
    # ``make_parser`` deliberately doesn't carry this flag.
    parser.add_argument(
        "--rename",
        action="append",
        default=[],
        metavar="SRC=DST",
        help=(
            "Rename a tool's schema name at deploy time. The same Python "
            "tool body registers under DST instead of (or in addition to) "
            "SRC; the Kafka topics it subscribes to are renamed in lockstep. "
            "Use for multi-host deployments of the same tool. Pair with the "
            "positional name list (which must reference DST, not SRC) for "
            "true rename behavior on the target host. Repeatable."
        ),
    )
    args = parser.parse_args(argv)

    aliases = _parse_and_validate_renames(args.rename)
    include_tools = _validate_tool_names(args.names)
    # Cross-validate: every alias src must appear in the positional
    # include list. Otherwise the alias env would bake into the
    # image but the include filter wouldn't reference the post-
    # rename name, leaving the clone dropped at boot — silent dead
    # config.
    _check_aliases_referenced(aliases, include_tools)
    # Translate the positional include list through the alias map.
    # An operator who types ``calfcord-package-tools terminal --rename
    # terminal=terminal_eu`` means "host the terminal tool body but
    # expose it as terminal_eu" — the filter env var baked into the
    # image must reference the POST-rename name, otherwise discovery
    # would drop the alias clone (filter excludes ``terminal_eu``)
    # and keep the original (filter includes ``terminal``) — the
    # opposite of intent. The dict.get fallback preserves names that
    # weren't renamed.
    include_tools = [aliases.get(n, n) for n in include_tools]
    dockerfile_content = render_tools_dockerfile(
        include_tools=include_tools,
        aliases=aliases or None,
    )

    context = args.context if args.context is not None else repo_root()
    return run_build(
        dockerfile_content=dockerfile_content,
        tag=args.tag,
        context=context,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
