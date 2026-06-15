"""Render slim per-agent / per-tool Dockerfiles.

The shape mirrors the project's canonical ``Dockerfile`` exactly,
varying these five things:

1. The runtime stage's ``apt-get install`` list — narrowed by the
   per-tool OS-dep mapping (e.g. no ``ripgrep`` if ``search_files``
   isn't included).
2. The builder's ``COPY agents`` line — for per-agent images, COPYs
   only the selected ``agents/<name>.md`` files rather than the whole
   directory. For per-tool images, the line is OMITTED entirely (a
   tools-only worker has no use for agent definitions).
3. The runtime ``ENV`` block — bakes ``CALFCORD_TOOLS_INCLUDE`` for
   per-tool images so ``apply_deploy_filters`` narrows the registry to just
   those tools at boot. Optionally also bakes ``CALFCORD_TOOLS_ALIAS`` (per
   ``--rename`` on ``calfcord-package-tools``) so the same Python tool
   body registers under a different schema name in this image — used
   for multi-host deployments of the same tool. Agents images also bake
   ``OPENHANDS_SUPPRESS_BANNER=1`` (their codex provider imports the
   openhands SDK, whose ASCII banner would otherwise drown boot logs);
   tools images don't import openhands, so they omit it.
4. The default ``CMD`` — ``calfkit-tools`` for tools images and
   ``calfkit-agent`` for agents images. The canonical Dockerfile
   defaults to ``calfkit-bridge`` because it's the only entry point
   that makes sense for an all-in-one image; the slim images each
   default to the runner that matches their payload.
5. The generated banner comment at the top — names the inputs that
   produced the image so a copy on disk is self-describing without
   having to re-derive them from the ``ENV`` block.

If the canonical ``Dockerfile`` is ever restructured, the constants in
this module must be updated to match. Tests in
``tests/packaging/test_dockerfile.py`` assert structural properties
of the output so drift fails CI.

Pure Python, no external deps — the CLIs in this package can invoke
the templater without spinning up the full ``TOOL_REGISTRY``.
"""

from __future__ import annotations

from collections.abc import Iterable

# Always-on OS packages for TOOLS images. ``ca-certificates`` is needed
# by any tool that talks HTTPS (web_fetch, web_search, and any
# third-party tool an operator might add later). ``git`` is kept because
# the ``terminal`` tool's most common ergonomic use — agents asking for
# ``git status`` / ``git log`` — only works if the binary is present.
# Removing either to save image size would trade a few tens of MB for
# "works in dev, fails in prod" surprises.
_ALWAYS_ON_TOOL_OS_DEPS: tuple[str, ...] = ("ca-certificates", "git")

# Always-on OS packages for AGENTS images. Narrower than the tools set:
# agent containers don't host tool bodies, so ``git`` (plus its
# transitive Debian deps — perl, libs, ~30MB installed) is dead weight.
# Keep ``ca-certificates`` because the LLM-provider HTTP clients
# (anthropic, openai SDKs) need a trust store.
_ALWAYS_ON_AGENT_OS_DEPS: tuple[str, ...] = ("ca-certificates",)

# Per-tool OS-dep mapping. Tools not listed here imply no extra OS
# packages — they're pure Python. When adding a new tool that needs an
# OS binary, add it here AND to the canonical Dockerfile so the
# all-in-one image keeps working.
_TOOL_OS_DEPS: dict[str, tuple[str, ...]] = {
    # ``search_files`` is ripgrep-backed (falls back to grep, which is in
    # the base image, but ripgrep is what its output parser expects).
    "search_files": ("ripgrep",),
    # The hermes terminal uses bash + a PTY (no tmux); execute_code runs
    # Python — both rely only on base-image binaries.
    "terminal": (),
    "process": (),
    "execute_code": (),
    # web tools only need ca-certificates, which is always-on. Listed
    # explicitly so the table is exhaustive.
    "web_fetch": (),
    "web_search": (),
    "web_extract": (),
    # FS and in-memory tools have no extra OS deps.
    "read_file": (),
    "write_file": (),
    "patch": (),
    "todo": (),
    "private_chat": (),
}


def os_deps_for_tools(include_tools: Iterable[str]) -> list[str]:
    """Return the sorted union of OS package names needed for the given tools.

    Includes :data:`_ALWAYS_ON_TOOL_OS_DEPS` regardless of the tool
    list. Unknown tool names are silently skipped — validation happens
    at the CLI layer, not here, so the templater stays loose-coupled
    to the live ``TOOL_REGISTRY``.
    """
    deps: set[str] = set(_ALWAYS_ON_TOOL_OS_DEPS)
    for tool in include_tools:
        deps.update(_TOOL_OS_DEPS.get(tool, ()))
    return sorted(deps)


def _render_apt_install(packages: Iterable[str]) -> str:
    """Render the ``RUN apt-get install`` block with one package per line.

    Indentation matches the canonical Dockerfile (8 spaces) so the
    output diffs cleanly against the source-of-truth file.
    """
    lines = ["RUN apt-get update \\", " && apt-get install -y --no-install-recommends \\"]
    for pkg in packages:
        lines.append(f"        {pkg} \\")
    lines.append(" && rm -rf /var/lib/apt/lists/*")
    return "\n".join(lines)


def _generated_header(
    *,
    kind: str,
    names: Iterable[str],
    aliases: dict[str, str] | None = None,
) -> str:
    """Banner at the top of every generated Dockerfile.

    Tells the reader this file is build-tool output, names the inputs
    that produced it (so a copy on disk is self-describing), and
    points at the canonical source for any non-templated changes.

    When ``aliases`` is non-empty, the header surfaces the alias map
    so a reader inspecting the image (or a copy of the file pulled
    out for forensics) can see the rename without having to decode
    the ENV block.
    """
    name_list = ", ".join(sorted(names))
    alias_line = ""
    if aliases:
        alias_pairs = ", ".join(f"{src} → {dst}" for src, dst in sorted(aliases.items()))
        alias_line = f"# Renames: {alias_pairs}\n"
    return (
        f"# Generated by calfcord-package-{kind} for: {name_list}\n"
        f"{alias_line}"
        f"# DO NOT EDIT by hand — regenerate via the CLI. The canonical\n"
        f"# source is the project's top-level ``Dockerfile``; this file\n"
        f"# is templated from ``src/calfcord/packaging/dockerfile.py``.\n"
    )


def render_tools_dockerfile(
    *,
    include_tools: list[str],
    aliases: dict[str, str] | None = None,
) -> str:
    """Render a Dockerfile for an image hosting only ``include_tools``.

    Args:
        include_tools: Tool schema names to include (post-rename, if
            any aliases are passed). Validation against ``TOOL_REGISTRY``
            is the CLI's job; this function trusts its input.
        aliases: Optional ``{src: dst}`` map baked into the image as
            ``CALFCORD_TOOLS_ALIAS=src1=dst1,src2=dst2``.
            ``apply_deploy_filters`` at boot clones each ``src`` tool's
            ``ToolNodeDef`` under the ``dst`` name with all four name-bound
            fields rewritten. Pairs naturally with ``include_tools`` containing
            the ``dst`` names to give true rename behavior — the
            original ``src`` drops out of the filter, only the clone
            survives. Empty/``None`` produces no ``CALFCORD_TOOLS_ALIAS``
            line in the output.

    Returns:
        The complete Dockerfile content as a string, ready to write
        to a tempdir and feed to ``docker buildx build``.
    """
    apt_block = _render_apt_install(os_deps_for_tools(include_tools))
    include_csv = ",".join(sorted(include_tools))
    header = _generated_header(
        kind="tools", names=include_tools, aliases=aliases or None
    )
    # Sort aliases by src for deterministic output — same input dict
    # contents in different insertion orders must produce byte-identical
    # Dockerfiles so build caches hit reliably.
    alias_csv = (
        ",".join(f"{src}={dst}" for src, dst in sorted((aliases or {}).items()))
        if aliases
        else None
    )
    # The ALIAS env line is conditionally appended below the always-on
    # ENV block. Wrapping in a leading backslash-newline keeps the
    # final ``\\`` on the previous line valid when present, and
    # produces zero output when absent — no dangling backslash to
    # confuse the Dockerfile parser.
    alias_env_line = (
        f" \\\n    CALFCORD_TOOLS_ALIAS={alias_csv}" if alias_csv else ""
    )

    # ``# syntax=`` MUST be on line 1 of the Dockerfile for Docker's
    # frontend selector to honor it. The generated banner comment goes
    # below the directive — Docker still treats the rest of the file
    # correctly, but the syntax directive's "first non-blank,
    # non-directive line" rule means the banner can't be above it.
    return f"""# syntax=docker/dockerfile:1.7
{header}#
# Per-tool calfcord image. Hosts ONLY: {include_csv}
#
# Tools subscribe to ``tool.<name>.input`` topics; this image's
# ``calfkit-tools`` worker subscribes only to the listed names because
# the ``CALFCORD_TOOLS_INCLUDE`` env var (baked below) narrows the registry
# composed in ``tools/__init__.py`` (via ``tools/deploy_filters.py``).


# ── builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev


# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# OS packages narrowed to those the included tools actually use.
{apt_block}

ARG UID=1000
ARG GID=1000
RUN groupadd --gid ${{GID}} calfcord \\
 && useradd  --uid ${{UID}} --gid ${{GID}} --create-home --shell /bin/bash calfcord

WORKDIR /app

# No openhands banner suppression: the tools image hosts only the vendored
# calfkit-tools nodes and never imports that SDK (the agents image sets it
# because its codex provider does). See the module docstring.
ENV PATH=/app/.venv/bin:$PATH \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    CALFCORD_TOOLS_INCLUDE={include_csv}{alias_env_line}

COPY --from=builder --chown=calfcord:calfcord /app /app

USER calfcord

# Per-tool images default to the tools entry point. The bridge / agent
# / router entry points are still on PATH (same image, same venv) but
# don't make sense here — running ``calfkit-bridge`` in a tools-only
# image would try to load ``agents/*.md`` which isn't in this image.
CMD ["calfkit-tools"]
"""


def render_agents_dockerfile(
    *,
    include_agents: list[str],
) -> str:
    """Render a Dockerfile for an image hosting only ``include_agents``.

    Args:
        include_agents: Agent names (filename stems, e.g. ``["scribe",
            "conan"]``). Validation against on-disk ``agents/<name>.md``
            files is the CLI's job.

    Returns:
        Complete Dockerfile content. Differs from
        :func:`render_tools_dockerfile`:

        * No ``CALFCORD_TOOLS_INCLUDE`` (agent images don't host tools).
        * No ``tmux`` / ``ripgrep`` / ``git`` (agent containers don't
          execute tool bodies).
        * The ``COPY agents`` line names individual files instead of
          the whole directory, so the image's ``/app/agents/`` only
          contains the selected agents.
    """
    # Agent images don't host tools, so no tool-driven OS deps.
    apt_block = _render_apt_install(_ALWAYS_ON_AGENT_OS_DEPS)
    header = _generated_header(kind="agents", names=include_agents)
    # COPY each agent's .md file individually. Sorted for deterministic
    # layer hashes — same inputs produce byte-identical Dockerfiles.
    # No explicit ``mkdir -p ./agents`` needed: Docker auto-creates
    # COPY destination directories.
    agent_copy_lines = "\n".join(
        f"COPY agents/{name}.md ./agents/{name}.md" for name in sorted(include_agents)
    )

    # ``# syntax=`` must be on line 1 — same constraint as the tools
    # render. The banner header goes below.
    return f"""# syntax=docker/dockerfile:1.7
{header}#
# Per-agent calfcord image. Hosts ONLY: {", ".join(sorted(include_agents))}
#
# The image's ``/app/agents/`` contains only the listed agent .md
# files. ``calfkit-agent`` loads whatever's present, so no runtime
# filter is needed — the filesystem IS the filter.


# ── builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \\
    uv sync --frozen --no-dev

# Per-agent COPY: only the .md files explicitly named in the build
# command end up in the image. Agents not in the list literally
# don't exist inside this container.
{agent_copy_lines}


# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# OS packages: agent images don't run tool bodies, so no tmux/ripgrep/git.
{apt_block}

ARG UID=1000
ARG GID=1000
RUN groupadd --gid ${{GID}} calfcord \\
 && useradd  --uid ${{UID}} --gid ${{GID}} --create-home --shell /bin/bash calfcord

WORKDIR /app

# OPENHANDS_SUPPRESS_BANNER silences the openhands SDK's ASCII boot
# banner. Same rationale as the tools image — boot-log signal/noise.
ENV PATH=/app/.venv/bin:$PATH \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    OPENHANDS_SUPPRESS_BANNER=1

COPY --from=builder --chown=calfcord:calfcord /app /app

USER calfcord

CMD ["calfkit-agent"]
"""
