"""``calfcord router show|set|edit`` + ``router start|stop`` — the router's
first-class, editable config surface and its lifecycle (design §12.0).

The router holds an LLM connection like an agent, so — unlike a one-shot wizard —
it gets an *editable* config surface that mirrors the agent config ergonomics:

* ``show`` renders the current provider/model (never any secret), like
  ``agent show`` renders a definition's fields;
* ``set`` writes provider/model non-interactively, validated the same way an
  agent's provider is (an unknown provider is rejected, nothing is persisted);
* ``edit`` runs the interactive provider sub-flow shared by every wizard
  (:func:`calfcord.cli._providers.configure_provider`).

This module **reconciles the old one-shot ``router setup`` into ONE editable
path**: ``edit`` is the wizard ``router setup`` used to be (same explanation, same
``configure_provider`` sub-flow, same ``cheap=True`` bias for a per-message
classifier), and ``show`` / ``set`` are the non-interactive surfaces it lacked.

Config persists to the two ``CALFKIT_ROUTER_*`` env vars the router runner already
reads (:func:`calfcord.router.definition.build_router_definition`), so a running
router picks up a change on its next (re)start with no code change. The var names
are imported from :mod:`calfcord.router.definition` so producer and consumer
cannot drift.

Lifecycle (``router start|stop``) is built on the generic
:func:`calfcord.supervisor.component.component_start` /
:func:`~calfcord.supervisor.component.component_stop` (DRY with tools/mcp), with
one router-specific rule: **``router start`` FAILS FAST when unconfigured** — it
refuses to launch a router with no provider/model *before* any supervisor call, so
the operator gets an actionable "configure it first" message instead of a process
that boots and immediately dies on a missing LLM target.

This deliberately avoids the bridge-only ``calfcord.mcp.servers`` (transport +
``$VAR`` secrets), keeping the agent-side decoupling invariant. It is **not**,
however, SDK-light: ``from calfcord.agents.definition import Provider`` triggers
the ``calfcord.agents`` package ``__init__``, which eagerly imports
``agents.factory`` and through it the anthropic/openai provider SDKs. We accept
that transitive cost — the entry point already pays it for the agent verbs — and
note it here so nobody mistakes this module for a lazy-import seam it is not.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import get_args

from calfcord.agents.definition import Provider
from calfcord.cli import _providers
from calfcord.cli._envfile import read_env, upsert
from calfcord.cli._prompts import Prompter
from calfcord.router.definition import _MODEL_ENV as _MODEL_VAR
from calfcord.router.definition import _PROVIDER_ENV as _PROVIDER_VAR
from calfcord.supervisor.component import component_restart, component_start, component_stop

# The agents' default provider, used to seed ``edit`` when the router has no prior
# choice of its own — so an operator who configured one provider in ``init`` isn't
# surprised by a second, unrelated default here. Mirrors the old ``router setup``.
_AGENT_DEFAULT_PROVIDER_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"

# The router's declared Process Compose slot name (see
# :func:`calfcord.supervisor.compose.render_compose`). Kept as a named constant so
# the lifecycle wrappers and the generated project cannot drift on the literal.
_ROUTER_PROCESS_NAME = "router"

# The valid provider tags, from the single source of truth — the
# :data:`~calfcord.agents.definition.Provider` Literal. Computing the set from the
# type (rather than re-listing it) means ``set``'s validation can never drift from
# what the runner's ``AgentDefinition`` will actually accept.
_VALID_PROVIDERS: tuple[str, ...] = get_args(Provider)


def _router_provider_model(env: dict[str, str]) -> tuple[str | None, str | None]:
    """Read the configured ``(provider, model)`` from a parsed ``.env``.

    Empty strings are normalized to ``None`` so this agrees with the runner's
    ``or`` chain (:func:`build_router_definition`), which treats an empty env var
    as "unset" and falls through to the bundled ``router.md`` / in-code default.
    """
    provider = env.get(_PROVIDER_VAR) or None
    model = env.get(_MODEL_VAR) or None
    return provider, model


def show(*, env_path: Path) -> int:
    """``calfcord router show``: print the router's current provider/model.

    Reads only the two ``CALFKIT_ROUTER_*`` vars and renders them — never any API
    key that shares the file, so this is safe to run anywhere. An unconfigured
    router prints an explicit "(not configured)" line (and how to fix it) rather
    than two blanks. Always returns ``0`` — ``show`` is read-only and
    "not configured yet" is a valid state, not an error.
    """
    provider, model = _router_provider_model(read_env(env_path))

    if provider is None and model is None:
        print("router: (not configured)")
        print("Configure it with: calfcord router set --provider <p> --model <m>")
        print("                or: calfcord router edit   (interactive)")
        return 0

    print(f"router provider: {provider or '(not set)'}")
    print(f"router model:    {model or '(not set)'}")
    return 0


def set_config(*, env_path: Path, provider: str | None, model: str | None) -> int:
    """``calfcord router set [--provider P] [--model M]``: write the router LLM config.

    A partial set is honoured (write only the flag(s) given; leave the other var
    untouched), so an operator can retarget the model without restating the
    provider. The provider is validated against the same
    :data:`~calfcord.agents.definition.Provider` tags the runner accepts — an
    unknown provider is rejected and **nothing** is persisted (no half-write); a
    blank model is likewise rejected. ``set`` with neither flag is a usage error,
    not a silent no-op.

    Returns ``0`` on a successful write, ``1`` on a validation/usage error (with an
    actionable message; the file is untouched on any rejection).
    """
    if provider is None and model is None:
        print("error: nothing to set; pass --provider and/or --model.")
        return 1

    updates: dict[str, str] = {}

    if provider is not None:
        if provider not in _VALID_PROVIDERS:
            print(
                f"error: unknown provider {provider!r}; "
                f"choose one of {', '.join(_VALID_PROVIDERS)}."
            )
            return 1
        updates[_PROVIDER_VAR] = provider

    if model is not None:
        # A model is a free-form slug the provider accepts, but an *empty* one
        # would persist a meaningless value the runner reads as "unset" — reject
        # it loudly rather than write a blank.
        if not model.strip():
            print("error: --model must be a non-empty model id.")
            return 1
        updates[_MODEL_VAR] = model

    upsert(env_path, updates)
    print("router config updated:")
    if _PROVIDER_VAR in updates:
        print(f"  provider -> {updates[_PROVIDER_VAR]}")
    if _MODEL_VAR in updates:
        print(f"  model    -> {updates[_MODEL_VAR]}")
    # The terse next-step block (behavior #3): a sentence ending in a colon, a
    # blank line, the two-space-indented command. The router bakes its config at
    # construction, so a config change takes effect via the roster `restart` verb.
    print("\nRestart the router to apply:\n\n  calfcord router restart")
    return 0


def edit(prompter: Prompter, *, env_path: Path) -> int:
    """``calfcord router edit``: the interactive router-config flow.

    This is the path the old one-shot ``calfcord router setup`` wizard became — it
    explains the (optional, ambient) router, runs the shared provider sub-flow
    (:func:`_providers.configure_provider` with ``cheap=True`` biasing the model
    default toward a fast tier — the right call for a per-message classifier), and
    persists the choice as the two ``CALFKIT_ROUTER_*`` vars. Reads the current
    ``.env`` once so a re-run pre-selects the operator's existing provider/model
    and the provider sub-flow can show/keep already-set credentials. Returns ``0``.

    The provider sub-flow owns all credential prompting and live model fetching; we
    persist only the resulting provider/model (never a secret) via the
    position-preserving :func:`upsert`.
    """
    current = read_env(env_path)

    print("calfcord router edit — configuring the OPTIONAL ambient router in", env_path)
    print()
    print("The router decides which agent answers a message that has NO @mention.")
    print("It runs an LLM call once per ambient message, so a fast/cheap model is")
    print("recommended. It is optional: without it, @mentions still route to agents")
    print("and un-@mentioned messages are simply left unanswered.")
    print()

    # Default the provider to the router's own prior choice, then the agents'
    # default provider, then anthropic — so a re-run keeps the operator's pick and
    # a first run inherits the provider they already set up in ``init``.
    default_provider = (
        current.get(_PROVIDER_VAR) or current.get(_AGENT_DEFAULT_PROVIDER_VAR) or "anthropic"
    )
    provider, model = _providers.configure_provider(
        prompter,
        env_path=env_path,
        current=current,
        default_provider=default_provider,
        cheap=True,
        current_model=current.get(_MODEL_VAR),
    )
    upsert(env_path, {_PROVIDER_VAR: provider, _MODEL_VAR: model})

    print()
    print(f"Router will use {provider}/{model}.")
    print("It is optional — skip it and @mentions still work.")
    # The terse next-step block (behavior #3): a sentence ending in a colon, a
    # blank line, the two-space-indented command. The router bakes its config at
    # construction, so a config change takes effect via the roster `restart` verb
    # (which also brings a stopped router up — so it is the right steer whether or
    # not the router was already running).
    print("\nRestart the router to apply:\n\n  calfcord router restart")
    return 0


def _is_configured(env_path: Path) -> bool:
    """Whether BOTH router vars are set (non-empty) in ``env_path``.

    The fail-fast precondition for ``router start``: the runner needs both a
    provider and a model (a half-configured router boots and dies), and an empty
    string counts as unset to match the runner's ``or`` chain.
    """
    provider, model = _router_provider_model(read_env(env_path))
    return provider is not None and model is not None


async def router_start(
    home: str | os.PathLike[str],
    *,
    env_path: Path,
    client=None,
) -> int:
    """``calfcord router start``: bring the router online — but only if configured.

    **Fail-fast precondition (design §12.0):** if the router has no provider/model
    the runner would boot and immediately die, so refuse here — *before any
    supervisor call* — and point the operator at ``router set`` / ``router edit``.
    Only once configured do we delegate to the generic
    :func:`calfcord.supervisor.component.component_start` (the same DRY base
    tools/mcp use), which does the workspace check and starts the ``router`` slot.

    ``client`` is injected for testing; production defaults it (in
    ``component_start``) to a per-home REST client. Returns ``0`` on a successful
    start, ``1`` when unconfigured or the workspace is down.
    """
    if not _is_configured(env_path):
        print(
            "error: the router is not configured (needs a provider and model). "
            "Set it first: calfcord router set --provider <p> --model <m> "
            "(or run: calfcord router edit)."
        )
        return 1

    return await component_start(home, name=_ROUTER_PROCESS_NAME, client=client)


async def router_stop(
    home: str | os.PathLike[str],
    *,
    client=None,
) -> int:
    """``calfcord router stop``: take the router offline.

    No config check — clocking a component out is always valid (and config-
    agnostic). Delegates straight to
    :func:`calfcord.supervisor.component.component_stop`. ``client`` is injected
    for testing. Returns ``0`` on success, ``1`` when the workspace is down.
    """
    return await component_stop(home, name=_ROUTER_PROCESS_NAME, client=client)


async def router_restart(
    home: str | os.PathLike[str],
    *,
    client=None,
) -> int:
    """``calfcord router restart``: reload the running router after a config change.

    The apply mechanism behind ``router set`` / ``router edit``'s next-step hint:
    the runner bakes its provider/model at construction, so a restart is how a
    config edit takes effect on a live router. Like ``stop`` it runs NO config
    precheck — a running router already had valid config, and ``component_restart``
    issues the REST restart unconditionally — so it delegates straight to
    :func:`calfcord.supervisor.component.component_restart` for the ``router`` slot.
    ``client`` is injected for testing. Returns ``0`` on success, ``1`` when the
    workspace is down.
    """
    return await component_restart(home, name=_ROUTER_PROCESS_NAME, client=client)
