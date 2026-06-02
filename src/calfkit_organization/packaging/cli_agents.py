"""``calfcord-package-agents`` — build a slim image hosting only specific agents.

Mirrors :mod:`cli_tools` but:

* Validates names against on-disk ``agents/<name>.md`` files via
  :func:`agents.loader.load_agents_dir`, not against ``TOOL_REGISTRY``.
  An invalid frontmatter in a selected agent fails the build early.
* The generated Dockerfile COPYies only the selected ``.md`` files
  into the image, rather than the entire ``agents/`` directory. The
  agent runner reads "whatever's in the agents dir" with no code
  change — the filesystem is the filter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Same banner-suppression rationale as in cli_tools.py — build-time CLI,
# operators don't care about openhands' banner during a build.
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from calfkit_organization.packaging._build import make_parser, repo_root, run_build
from calfkit_organization.packaging.dockerfile import render_agents_dockerfile


def _validate_agent_names(names: list[str], agents_dir: Path) -> list[str]:
    """Check each name corresponds to a parseable ``agents/<name>.md`` file.

    Uses :func:`load_agents_dir` rather than just checking file
    existence so an agent definition with bad frontmatter is caught
    HERE (build-time) rather than at first ``calfkit-agent`` boot of
    the resulting image. The error message names every unparseable
    agent in one pass so a multi-typo .md surfaces all issues at
    once.
    """
    # Local import keeps ``--help`` cheap.
    from calfkit_organization.agents.loader import load_agents_dir

    # Narrow the catch: ``load_agents_dir`` raises on missing dir
    # (FileNotFoundError / NotADirectoryError) and on bad frontmatter
    # (ValueError via parse_agent_md). Letting everything else
    # propagate as a real traceback preserves the project's "infra bugs
    # are loud" contract — a future loader change that newly raises
    # AttributeError shouldn't be silently re-labelled "cannot load
    # agents."
    try:
        defs = load_agents_dir(agents_dir)
    except (FileNotFoundError, NotADirectoryError, ValueError) as e:
        sys.stderr.write(f"error: cannot load agents from {agents_dir}: {e}\n")
        sys.exit(2)

    known_by_id = {d.agent_id: d for d in defs}
    known = sorted(known_by_id)
    unknown = [name for name in names if name not in known_by_id]
    if unknown:
        sys.stderr.write(
            f"error: unknown agent(s) {unknown!r} in {agents_dir}\n"
            f"  Known agents: {', '.join(known) or '<none>'}\n"
        )
        sys.exit(2)
    # De-dupe order-preservingly so the generated Dockerfile's COPY
    # list is stable.
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = make_parser(
        prog="calfcord-package-agents",
        item_kind="agent",
        item_examples="codex_demo",
    )
    args = parser.parse_args(argv)

    context = args.context if args.context is not None else repo_root()
    agents_dir = context / "agents"
    if not agents_dir.is_dir():
        sys.stderr.write(
            f"error: agents directory not found at {agents_dir}; "
            f"use --context to point at your calfcord checkout root.\n"
        )
        sys.exit(2)

    include_agents = _validate_agent_names(args.names, agents_dir)
    dockerfile_content = render_agents_dockerfile(include_agents=include_agents)

    return run_build(
        dockerfile_content=dockerfile_content,
        tag=args.tag,
        context=context,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
