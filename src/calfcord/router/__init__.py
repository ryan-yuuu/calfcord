"""Built-in routing-agent module.

The router is a calfkit agent that decides which assistants should respond
to each ambient (``kind="message"``) Discord message. It sits in front of
all assistant agents and the bridge routes ambient traffic to it via
``discord.ambient.in``. The router's LLM emits a
:class:`~calfcord.agents.routing.RoutingDecision` (via
pydantic-ai's ``ToolOutput`` pattern) on ``routing.decisions``, and the
:func:`~calfcord.router.fanout.build_fanout_consumer`-built
fan-out consumer republishes one synthesized ``kind="slash"`` wire per
chosen agent to ``bridge.synthesized.in``.

Public surface:
    - :func:`build_router_definition` — constructs the singleton
      :class:`AgentDefinition` for the router (used by
      :class:`AgentRegistry.from_agents_dir`).
    - :func:`build_fanout_consumer` — the fan-out
      :class:`ConsumerNode` the ``calfkit-router`` runner registers
      alongside the router agent on a single :class:`Worker`.
    - :func:`build_router_temp_instructions` — per-call agent roster
      injected as ``temp_instructions`` so the LLM sees the available
      respondents on every invocation.
    - :data:`ROUTER_AGENT_ID` — the canonical agent id (other modules
      reference this to defensively self-filter or to look up the
      router in the registry).
"""

from __future__ import annotations

from calfcord.router.definition import ROUTER_AGENT_ID, build_router_definition
from calfcord.router.fanout import build_fanout_consumer
from calfcord.router.roster import build_router_temp_instructions

__all__ = [
    "ROUTER_AGENT_ID",
    "build_fanout_consumer",
    "build_router_definition",
    "build_router_temp_instructions",
]
