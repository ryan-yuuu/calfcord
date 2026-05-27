# syntax=docker/dockerfile:1.7
#
# Calfcord container image.
#
# One image, four entry points: the bridge / agent / router / tools
# commands defined in pyproject.toml's [project.scripts] block.
# ``docker-compose.yml`` selects the entry point per service via
# ``command:``.
#
# Build with ``docker compose build`` (recommended) or ``docker build``.
# On Linux hosts where ``id -u`` is not 1000, pass ``--build-arg UID=$(id -u)``
# so files the tools write to bind-mounted host dirs end up owned by the
# caller rather than by ``root``.


# ── builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Copy the static ``uv`` binary from the upstream image rather than
# ``curl | sh``; keeps the build hermetic and version-pinnable.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Disable bytecode-write so the image's site-packages stays smaller and
# free of host-Python-specific .pyc files. Set during the build, not in
# the runtime ENV, because uv reads it at sync time.
ENV PYTHONDONTWRITEBYTECODE=1

# Step 1: install dependencies WITHOUT the project source.
# Copying ``pyproject.toml`` + ``uv.lock`` + ``README.md`` (hatchling
# reads the README for package metadata) first means the dep-install
# layer caches as long as those three files are unchanged. ``src/``
# and ``agents/`` edits do NOT re-trigger this layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Step 2: install the project itself.
# Source files come BEFORE the agents/ copy below so ``agents/*.md``
# edits don't invalidate the ``uv sync`` layer. agents/ is plain data
# (not a Python package per pyproject.toml's
# ``[tool.hatch.build.targets.wheel].packages``); it's copied last so
# it sits in a small leaf layer that authors can edit cheaply.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Step 3: copy the live agent definitions. Compose mounts ``./agents``
# read-only over this path at runtime, so the baked copy is the
# fallback used only when the image is run without compose.
COPY agents ./agents


# ── runtime ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# OS packages the calfcord tools need at runtime. Strictly minimal — no
# build toolchain leaks past the builder stage.
#   tmux            — persistent shell sessions for the ``shell`` tool
#   ripgrep         — preferred backend for the ``grep`` tool
#   git             — agents commonly run ``git`` via the ``shell`` tool
#   ca-certificates — HTTPS trust store for ``web_fetch``
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tmux \
        ripgrep \
        git \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. UID/GID default to 1000 (the typical Linux desktop
# user); override at build time with ``--build-arg UID=$(id -u)`` on
# hosts whose primary user is not 1000. On macOS Docker Desktop the
# UID mapping is virtual so the default works regardless.
ARG UID=1000
ARG GID=1000
RUN groupadd --gid ${GID} calfcord \
 && useradd  --uid ${UID} --gid ${GID} --create-home --shell /bin/bash calfcord

# Pre-create the calfcord user's XDG-style dirs that the codex provider
# writes to (auth credentials + prompt cache). Without this, when a
# named volume is mounted onto either path Docker initializes it with
# root:root 0o755 ownership and the calfcord user inside the container
# cannot write — chmod fails (operation not permitted), filelock fails
# (permission denied). With the dirs pre-created as calfcord:calfcord
# 0o700, the named volume inherits the owner+perms on first mount.
# A host-bind-mounted dir on macOS still works regardless via Docker
# Desktop's UID translation.
RUN install -d -o calfcord -g calfcord -m 0700 /home/calfcord/.calfcord \
 && install -d -o calfcord -g calfcord -m 0700 /home/calfcord/.calfcord/auth \
 && install -d -o calfcord -g calfcord -m 0700 /home/calfcord/.calfcord/codex_prompts

WORKDIR /app

# PATH puts the venv's bin first so the entry-point scripts resolve
# without a wrapper. PYTHONUNBUFFERED makes ``docker compose logs`` show
# stdout/stderr in real time. OPENHANDS_SUPPRESS_BANNER silences the
# openhands SDK's ASCII boot banner — it's printed unconditionally on
# first import of the openhands package and pollutes both
# ``docker compose logs`` and ad-hoc ``docker run`` sessions that
# inspect the tool registry. Operators who want the banner can
# override with ``-e OPENHANDS_SUPPRESS_BANNER=0`` at run time.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENHANDS_SUPPRESS_BANNER=1

# Copy the venv + source from the builder. ``--chown`` sets ownership
# in one step rather than running ``chown -R`` post-copy (which would
# duplicate the layer's storage). ``--from`` references the stage by
# its AS-name.
COPY --from=builder --chown=calfcord:calfcord /app /app

USER calfcord

# Default to the bridge so ``docker run calfcord`` does something
# sensible without a ``--command`` override. Compose sets each service's
# ``command:`` explicitly.
CMD ["calfkit-bridge"]
