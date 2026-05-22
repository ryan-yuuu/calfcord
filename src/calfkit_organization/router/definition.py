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

import os

from calfkit_organization.agents.definition import AgentDefinition, Provider, ThinkingEffort
from calfkit_organization.router.prompt import SYSTEM_PROMPT

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

_DEFAULT_PROVIDER: Provider = "openai"
_DEFAULT_MODEL = "gpt-5-nano"
_DEFAULT_THINKING_EFFORT: ThinkingEffort = "none"


def build_router_definition() -> AgentDefinition:
    """Construct the singleton :class:`AgentDefinition` for the router.

    Reads provider/model/thinking_effort from environment variables so
    operators can swap LLMs without editing source. Defaults are tuned
    for fast/cheap classification — the router runs on every ambient
    message in every channel, so a 1ms latency adds up.

    Environment variables:
        - ``CALFKIT_ROUTER_PROVIDER`` (default ``"openai"``)
        - ``CALFKIT_ROUTER_MODEL`` (default ``"gpt-5-nano"``)
        - ``CALFKIT_ROUTER_THINKING_EFFORT`` (default ``"none"``)

    The returned definition satisfies the router invariants enforced by
    :class:`AgentDefinition`'s validators: ``role="router"``, empty
    ``tools``, non-empty ``publish_topic``, and the strict
    name/slash/display_name format constraints.

    Returns:
        A frozen :class:`AgentDefinition` ready to be appended to the
        registry alongside user-defined agents. ``source_path`` is
        ``None`` (no on-disk ``.md``).
    """
    provider_raw = os.getenv(_PROVIDER_ENV, _DEFAULT_PROVIDER)
    # We let pydantic raise on an invalid Provider tag rather than
    # second-guessing here — same surface area as user-defined
    # ``.md`` parsing.
    model = os.getenv(_MODEL_ENV, _DEFAULT_MODEL)
    thinking_effort_raw = os.getenv(_THINKING_EFFORT_ENV, _DEFAULT_THINKING_EFFORT)

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
        system_prompt=SYSTEM_PROMPT,
        source_path=None,
    )
