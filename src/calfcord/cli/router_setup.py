"""``calfcord router setup`` — the optional ambient-router configuration wizard.

The ambient router is what lets an agent answer a message that carries **no**
``@mention``: it inspects each un-addressed message in a watched channel and
decides which agent (if any) should reply. It is genuinely optional — without
it, ``@mentions`` still route directly to agents and un-addressed messages are
simply left unanswered — so this wizard lives behind its own subcommand rather
than in the main ``init`` flow, and an operator can skip it entirely.

Because the router runs an LLM call *once per ambient message*, a fast/cheap
model is the right default; the heavy lifting (provider menu, credential / Codex
OAuth, live model pick biased to the cheap tier) is delegated to
:func:`calfcord.cli._providers.configure_provider`. This module's only job is to
explain the trade-off and persist the operator's choice as the two
``CALFKIT_ROUTER_*`` env vars that :func:`calfcord.router.definition.build_router_definition`
already honours, so the running router picks them up with no code change.
"""

from __future__ import annotations

from pathlib import Path

from calfcord.cli import _providers
from calfcord.cli._envfile import read_env, upsert
from calfcord.cli._prompts import Prompter

_PROVIDER_VAR = "CALFKIT_ROUTER_PROVIDER"
_MODEL_VAR = "CALFKIT_ROUTER_MODEL"
# The provider the router defaults to when it has no prior choice of its own:
# the agents' default provider, so an operator who configured one provider in
# ``init`` isn't surprised by a second, unrelated default here.
_AGENT_DEFAULT_PROVIDER_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"


def run(prompter: Prompter, *, env_path: Path) -> int:
    """Guided setup for the OPTIONAL ambient router, writing ``CALFKIT_ROUTER_*``.

    Reads the current ``.env`` once so a re-run can pre-select the operator's
    existing provider/model and so the provider sub-flow can show/keep already
    set credentials. The provider + model are chosen by
    :func:`_providers.configure_provider` (``cheap=True`` biases the model
    default toward a fast tier, the right call for a per-message classifier),
    then persisted via the position-preserving :func:`upsert`. Returns ``0``.
    """
    current = read_env(env_path)

    print("calfcord router setup — configuring the OPTIONAL ambient router in", env_path)
    print()
    print("The router decides which agent answers a message that has NO @mention.")
    print("It runs an LLM call once per ambient message, so a fast/cheap model is")
    print("recommended. It is optional: without it, @mentions still route to agents")
    print("and un-@mentioned messages are simply left unanswered.")
    print()

    # Default the provider to the router's own prior choice, then the agents'
    # default provider, then anthropic — so a re-run keeps the operator's pick
    # and a first run inherits the provider they already set up in ``init``.
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
    print("Start it (with the other three processes) via: calfcord calfkit-router")
    print("It is optional — skip it and @mentions still work.")

    return 0
