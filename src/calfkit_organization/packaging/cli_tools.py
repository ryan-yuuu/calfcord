"""``calfcord-package-tools`` — build a slim image hosting only specific tools.

Walkthrough:
    1. Parse args. Names are positional; ``--tag`` is required.
    2. Validate the names against the live ``TOOL_REGISTRY``. Unknown
       names fail-fast with the full known list (mirrors the
       agent-factory's tool-resolution error message).
    3. Generate the Dockerfile via the templater. The image bakes
       ``CALFCORD_TOOLS_INCLUDE`` so the auto-discovery loader
       narrows registration to just the listed names.
    4. Write the Dockerfile to a tempdir; invoke ``docker buildx build``.
    5. On success, print the local tag plus follow-up commands the
       operator typically wants. On failure, leave the Dockerfile in
       place and print its path.

No registry push: the CLI builds locally with ``--tag``; the operator
runs ``docker push`` afterward against whichever registry they own.
"""

from __future__ import annotations

import os
import sys

# Suppress the openhands SDK boot banner during ``--help`` and registry
# validation. The CLI is a build-time tool, not a runtime worker —
# operators don't care about openhands' banner here. Set BEFORE any
# import of ``calfkit_organization.tools`` (which transitively imports
# the openhands SDK). Respect an explicit user override so anyone
# wanting to see the banner can ``OPENHANDS_SUPPRESS_BANNER=0 …``.
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from calfkit_organization.packaging._build import make_parser, repo_root, run_build
from calfkit_organization.packaging.dockerfile import render_tools_dockerfile


def _validate_tool_names(names: list[str]) -> list[str]:
    """Check each name resolves to a real entry in ``TOOL_REGISTRY``.

    Returns the de-duplicated, order-preserving list on success.
    Calls :func:`sys.exit` with code 2 (the argparse-standard "usage
    error" code) if any name is unknown, after printing the full
    known list for forensic value.

    Importing :data:`TOOL_REGISTRY` here triggers the auto-discovery
    walk in ``tools/__init__.py``; that's fine — the CLI runs
    short-lived so the boot cost is paid once per build invocation.
    """
    # Local import keeps ``--help`` cheap (no openhands-tools / smolagents
    # imports just to display usage text).
    from calfkit_organization.tools import TOOL_REGISTRY

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


def main(argv: list[str] | None = None) -> int:
    parser = make_parser(
        prog="calfcord-package-tools",
        item_kind="tool",
        item_examples="shell grep",
    )
    args = parser.parse_args(argv)

    include_tools = _validate_tool_names(args.names)
    dockerfile_content = render_tools_dockerfile(include_tools=include_tools)

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
