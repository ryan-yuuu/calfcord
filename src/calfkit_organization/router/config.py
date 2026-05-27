"""Optional YAML config file for the router definition.

Lets operators tune the router's runtime knobs (``provider``,
``model``, ``thinking_effort``, ``history_turns``) by editing a single
``router.yml`` at the project root, instead of setting four
environment variables. Env vars still work as a runtime override
layer so an operator can stage a swap without editing files.

The file is intentionally narrow — model configuration only. The
router's identity (``agent_id``, ``slash``, ``display_name``, ``role``,
``publish_topic``, ``tools``, ``system_prompt``) is project
infrastructure and not operator-tunable; reserved fields appearing in
the YAML are rejected at load time via ``extra="forbid"`` so an
operator cannot accidentally break the singleton invariants enforced
by :class:`AgentRegistry`.

Discovery and precedence
------------------------
* Default path: ``./router.yml`` resolved against CWD.
* Override: ``CALFKIT_ROUTER_CONFIG_PATH``.
* Default-path missing: silent fallback to env-var + code defaults
  (backward compat — deploys with no ``router.yml`` are unchanged).
* Explicit-path missing: raises :class:`FileNotFoundError` so a typo'd
  env var fails loudly rather than silently degrading to defaults.

Precedence (highest wins): ``router.yml`` field > matching env var >
in-code default. Mirrors :func:`resolve_provider`'s
``definition.provider or env or default`` chain.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from calfkit_organization.agents.definition import Provider, ThinkingEffort

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV = "CALFKIT_ROUTER_CONFIG_PATH"
"""Env var an operator can set to point at a non-default ``router.yml``
location. When unset, the loader looks for ``./router.yml`` relative to
the process CWD."""

DEFAULT_CONFIG_PATH = "router.yml"
"""Default config file path, resolved against the process CWD. The
calfkit-router container's WORKDIR is ``/app``, so the docker-compose
mount ``./router.yml:/app/router.yml:ro`` lines up with this default
without any extra env-var plumbing."""


class RouterConfig(BaseModel):
    """Operator-tunable router runtime knobs parsed from ``router.yml``.

    All fields are optional — missing fields fall through to env vars
    (``CALFKIT_ROUTER_PROVIDER``, ``CALFKIT_ROUTER_MODEL``,
    ``CALFKIT_ROUTER_THINKING_EFFORT``, ``CALFKIT_ROUTER_HISTORY_TURNS``)
    and then to the in-code defaults in
    :mod:`calfkit_organization.router.definition`.

    ``extra="forbid"`` rejects any unknown key so a typo
    (``provder: openai``) or a reserved field (``slash:``,
    ``system_prompt:``) surfaces at boot rather than silently dropping
    on the floor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Provider | None = None
    model: str | None = None
    thinking_effort: ThinkingEffort | None = None
    history_turns: int | None = Field(default=None, ge=0, le=100)


def resolve_router_config_path() -> tuple[Path, bool]:
    """Return ``(path, explicit)`` for the router config file lookup.

    ``explicit`` is ``True`` when ``CALFKIT_ROUTER_CONFIG_PATH`` is set
    in the environment — a missing file at an explicit path is a fatal
    misconfiguration. ``False`` means the default path is in use and a
    missing file should fall back silently to env-var-only behavior
    (backward compat with deploys that have no ``router.yml``).
    """
    raw = os.getenv(CONFIG_PATH_ENV)
    if raw:
        return Path(raw), True
    return Path(DEFAULT_CONFIG_PATH), False


def load_router_config() -> RouterConfig | None:
    """Load and validate ``router.yml`` when present.

    Returns:
        - A :class:`RouterConfig` instance when the file exists and parses.
        - ``None`` when the default path is in use and the file is absent
          (backward-compat fallback to env-var + code defaults).

    Raises:
        FileNotFoundError: when ``CALFKIT_ROUTER_CONFIG_PATH`` is set
            but the file does not exist. An operator who explicitly
            points at a path expects that file to be loaded — silently
            degrading to defaults would hide a configuration typo.
        ValueError: when the file is empty, contains malformed YAML,
            does not parse to a mapping, or contains unknown / invalid
            fields. The file path is included in the message to make
            the error self-describing in container logs.
    """
    path, explicit = resolve_router_config_path()
    # ``is_file()`` (vs ``exists()``) also rejects directories. Matters
    # because Docker's bind-mount of a non-existent host file creates a
    # *directory* at the container path; a defensive is_file() check
    # degrades that to "no config present" instead of attempting to
    # read_text() a directory and raising IsADirectoryError.
    if not path.is_file():
        if explicit:
            raise FileNotFoundError(
                f"{CONFIG_PATH_ENV}={path} but no such file exists"
            )
        return None

    # Absolute path makes error messages and the boot log line self-
    # describing in container logs where CWD context isn't obvious.
    path = path.resolve()
    text = path.read_text()
    if not text.strip():
        # An empty file is almost always a mistake (operator created it
        # intending to edit, forgot, restarted the service). Fail loudly
        # rather than silently using all defaults — which would mask the
        # half-finished change.
        raise ValueError(f"{path}: router config file is empty")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: malformed YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top-level YAML must be a mapping, got "
            f"{type(data).__name__}"
        )

    try:
        config = RouterConfig(**data)
    except ValidationError as exc:
        # Wrap the pydantic error with the file path so operators see
        # which file the validation failed against. Pydantic's own
        # message lists each invalid field with its location.
        raise ValueError(f"{path}: invalid router config: {exc}") from exc

    logger.info(
        "loaded router config from %s: provider=%s model=%s "
        "thinking_effort=%s history_turns=%s",
        path,
        config.provider,
        config.model,
        config.thinking_effort,
        config.history_turns,
    )
    return config
