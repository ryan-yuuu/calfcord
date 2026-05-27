"""Build the built-in :class:`AgentDefinition` for the routing agent.

The router definition is constructed in code rather than parsed from an
``agents/*.md`` file because:

* It is project infrastructure, not a user-customizable persona. A
  user-editable ``agents/_router.md`` would invite operators to remove
  it or tweak its slash/display_name, both of which would break the
  registry's "exactly one router" invariant.
* Its ``slash``/``display_name`` are reserved (``/_router`` / ``Router``)
  and not user-overridable. Embedding the constants here pins the
  contract.
* Provider/model/thinking_effort are environment-driven so operators
  can swap out the LLM without editing source — same lever the rest of
  the project uses for tunable runtime params.

The bridge registry (:class:`AgentRegistry`) appends this definition
automatically in :meth:`AgentRegistry.from_agents_dir` so user-defined
agents and the router co-exist in a single roster.
"""

from __future__ import annotations

import logging
import os

from calfkit_organization.agents.definition import AgentDefinition, Provider, ThinkingEffort
from calfkit_organization.router.config import RouterConfig, load_router_config
from calfkit_organization.router.prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

ROUTER_AGENT_ID = "_router"
"""Canonical agent id for the singleton built-in router. Imported by
other modules that need to defensively self-filter (the fan-out
consumer skips this id when republishing) or to look the router up in
the registry (:meth:`AgentRegistry.router`)."""

_ROUTER_SLASH = "/_router"
"""Schema-satisfying slash command for the router definition. Never
registered with Discord — the bridge does not invoke
``CommandTree.add_command`` for the router (it is not a user-invocable
agent). Reserved here so user-defined agents cannot accidentally
collide via their own ``.md`` ``slash:`` field."""

_ROUTER_DISPLAY_NAME = "Router"
"""Reserved display_name for the singleton router. User-defined agents
that try to use the same name fail at :meth:`AgentRegistry._index`
duplicate-detection time — the operator must rename their agent."""

_ROUTER_DESCRIPTION = "Internal routing agent (not user-invocable)"

_ROUTER_PUBLISH_TOPIC = "routing.decisions"
"""Kafka topic the router publishes :class:`RoutingDecision`s to. The
fan-out consumer subscribes here. Hardcoded rather than env-driven
because the project's topology contract is fixed; an operator changing
this topic would also need to coordinate the fan-out consumer's
subscription, which is also constant."""

_PROVIDER_ENV = "CALFKIT_ROUTER_PROVIDER"
_MODEL_ENV = "CALFKIT_ROUTER_MODEL"
_THINKING_EFFORT_ENV = "CALFKIT_ROUTER_THINKING_EFFORT"
_HISTORY_TURNS_ENV = "CALFKIT_ROUTER_HISTORY_TURNS"

_DEFAULT_PROVIDER: Provider = "openai"
_DEFAULT_MODEL = "gpt-5-nano"
_DEFAULT_THINKING_EFFORT: ThinkingEffort = "none"
"""The router runs a tightly-bounded structured-output task (pick agents
from a small roster, account for ongoing conversation continuity in rule
3 of the prompt). ``"minimal"`` is the lightest non-zero reasoning tier
— it gives the router enough budget to weigh continuity vs. topic match
without paying the latency/cost of a heavier tier on every ambient
message. Overridable via ``CALFKIT_ROUTER_THINKING_EFFORT`` for
operators who want to disable reasoning entirely (``"none"``) or trade
latency for better edge-case handling (``"low"`` or higher)."""
_DEFAULT_HISTORY_TURNS = 10
"""Default number of recent channel messages projected into the router's
``message_history``. Smaller than the assistant default (30) because:

* The router runs on every ambient message in every channel — the
  per-invocation cost adds up, and the routing decision is bounded
  by ``RoutingDecision``'s small structured output, so the marginal
  value of more context is modest.
* The router doesn't need to *carry* the conversation; it only
  needs enough context to recognize follow-ups vs. fresh topics.
* The router never appears as ``self`` in the projection (it has
  no Discord persona / no prior turns), so every record contributes
  one ``ModelRequest`` — no merging benefit from a larger window.

Overridable via ``CALFKIT_ROUTER_HISTORY_TURNS``."""


def build_router_definition() -> AgentDefinition:
    """Construct the singleton :class:`AgentDefinition` for the router.

    Configuration precedence (highest wins):
        1. ``router.yml`` field (see
           :mod:`calfkit_organization.router.config`).
        2. ``CALFKIT_ROUTER_*`` environment variable.
        3. In-code default (the ``_DEFAULT_*`` constants below).

    The YAML file is optional — when absent, the loader returns ``None``
    and this function falls back to the env-var + code-default path
    that earlier versions used exclusively.

    Defaults are tuned for fast/cheap classification — the router runs
    on every ambient message in every channel, so a 1ms latency adds up.

    Environment variables:
        - ``CALFKIT_ROUTER_PROVIDER`` (default ``"openai"``)
        - ``CALFKIT_ROUTER_MODEL`` (default ``"gpt-5-nano"``)
        - ``CALFKIT_ROUTER_THINKING_EFFORT`` (default ``"none"``)
        - ``CALFKIT_ROUTER_HISTORY_TURNS`` (default ``10``)
        - ``CALFKIT_ROUTER_CONFIG_PATH`` (default ``./router.yml``) —
          path to the optional YAML config file.

    The returned definition satisfies the router invariants enforced by
    :class:`AgentDefinition`'s validators: ``role="router"``, empty
    ``tools``, non-empty ``publish_topic``, and the strict
    name/slash/display_name format constraints.

    Returns:
        A frozen :class:`AgentDefinition` ready to be appended to the
        registry alongside user-defined agents. ``source_path`` is
        ``None`` (no on-disk ``.md`` — the router is constructed in
        code; the optional ``router.yml`` provides config overrides
        only, not a persona definition).
    """
    config = load_router_config()

    # We let pydantic raise on an invalid Provider / ThinkingEffort tag
    # rather than second-guessing here — same surface area as user-
    # defined ``.md`` parsing.
    provider_raw = (
        (config.provider if config else None)
        or os.getenv(_PROVIDER_ENV)
        or _DEFAULT_PROVIDER
    )
    model = (
        (config.model if config else None)
        or os.getenv(_MODEL_ENV)
        or _DEFAULT_MODEL
    )
    thinking_effort_raw = (
        (config.thinking_effort if config else None)
        or os.getenv(_THINKING_EFFORT_ENV)
        or _DEFAULT_THINKING_EFFORT
    )
    history_turns = _resolve_history_turns(config)

    return AgentDefinition(
        agent_id=ROUTER_AGENT_ID,
        slash=_ROUTER_SLASH,
        display_name=_ROUTER_DISPLAY_NAME,
        description=_ROUTER_DESCRIPTION,
        avatar_url=None,
        provider=provider_raw,  # type: ignore[arg-type]
        model=model,
        tools=(),
        thinking_effort=thinking_effort_raw,  # type: ignore[arg-type]
        role="router",
        publish_topic=_ROUTER_PUBLISH_TOPIC,
        history_turns=history_turns,
        system_prompt=SYSTEM_PROMPT,
        source_path=None,
    )


def _resolve_history_turns(config: RouterConfig | None) -> int:
    """Pick history_turns from config > env > default.

    Splits out from the inline resolver pattern used for the other
    fields because the env-var tier has its own validation +
    fallback-with-warn behavior (see :func:`_read_history_turns_env`)
    that we want to keep intact when the config doesn't pin the value.
    """
    if config is not None and config.history_turns is not None:
        return config.history_turns
    return _read_history_turns_env()


def _read_history_turns_env() -> int:
    """Read and validate ``CALFKIT_ROUTER_HISTORY_TURNS``.

    Falls back to :data:`_DEFAULT_HISTORY_TURNS` on any of:

    * env var unset
    * env var set to a non-integer
    * env var set to a value outside the 0..100 schema bounds

    Logs a warning on invalid values rather than raising, so the
    router still starts with a safe default if an operator mis-types
    the env var. The bridge can't usefully start without a router,
    so a strict fail-fast here would brick the whole deployment over
    one misconfigured env var.

    **Asymmetry with the other ``CALFKIT_ROUTER_*`` env vars** (provider,
    model, thinking_effort): those are validated lazily by pydantic
    inside :class:`AgentDefinition`'s field validators and fail fast at
    router build time. ``history_turns`` is fall-back-with-warn because
    it's a quality-degrading knob — wrong values produce smaller / no
    history but the agent still functions. A wrong provider or model
    name, by contrast, would produce no valid LLM responses at all, so
    fail-fast surfaces the misconfiguration before it produces user-
    visible silence.
    """
    raw = os.getenv(_HISTORY_TURNS_ENV)
    if raw is None:
        return _DEFAULT_HISTORY_TURNS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to default %d",
            _HISTORY_TURNS_ENV,
            raw,
            _DEFAULT_HISTORY_TURNS,
        )
        return _DEFAULT_HISTORY_TURNS
    if not 0 <= value <= 100:
        logger.warning(
            "%s=%d is outside the 0..100 schema bounds; falling back to default %d",
            _HISTORY_TURNS_ENV,
            value,
            _DEFAULT_HISTORY_TURNS,
        )
        return _DEFAULT_HISTORY_TURNS
    return value
