"""Pydantic protocol types for the bridge<->agent control plane.

Two flows:
- Bridge -> agent: AgentControlEnvelope wraps an AgentControlCommand (discriminated
  union of operations). Targeted commands ride agent.<id>.control.in topics; the
  DiscoveryPingOp rides the broadcast bridge.discovery topic.
- Agent -> bridge: AgentStateMessage (discriminated union of AgentStateEvent and
  AgentDepartureEvent) rides the shared agent.state topic.

All payloads carry a schema_version field. The bridge and agent each log + skip
messages whose schema_version doesn't match CONTROL_PLANE_SCHEMA_VERSION.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from calfkit_organization.agents.definition import AgentRole, Provider, ThinkingEffort

CONTROL_PLANE_SCHEMA_VERSION = 2


# --- Bridge -> agent commands ---

class SetThinkingEffortOp(BaseModel):
    op: Literal["set_thinking_effort"] = "set_thinking_effort"
    agent_id: str          # defensive cross-check vs publish topic
    value: ThinkingEffort
    request_id: str        # UUIDv4, logs only -- no ack channel in v1
    issued_by: str         # discord user id, audit-only


class DiscoveryPingOp(BaseModel):
    op: Literal["discovery_ping"] = "discovery_ping"
    issued_at: datetime
    request_id: str


AgentControlCommand = Annotated[
    SetThinkingEffortOp | DiscoveryPingOp,
    Field(discriminator="op"),
]


class AgentControlEnvelope(BaseModel):
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    command: AgentControlCommand


# --- Agent -> bridge state messages ---

StateEventCause = Literal["startup", "command_applied", "discovery_response"]


class AgentStateEvent(BaseModel):
    # Explicit ``extra="ignore"`` (pydantic's default, made load-bearing on
    # purpose): a NEW agent may add wire fields (as ``memory`` was added) without
    # bumping CONTROL_PLANE_SCHEMA_VERSION, and an OLD bridge that predates the
    # field must silently ignore it rather than reject the event. Do NOT change
    # this to ``extra="forbid"`` for "consistency" with AgentDefinition — that
    # would break backward compatibility for mixed-version rolling deploys.
    model_config = ConfigDict(extra="ignore")

    kind: Literal["state"] = "state"
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    agent_id: str
    display_name: str
    description: str
    avatar_url: str | None = None
    role: AgentRole
    history_turns: int
    thinking_effort: ThinkingEffort | None = None
    provider: Provider | None = None      # nullable: required at the AgentDefinition
                                          # level only for non-bridge use; bridge needs
                                          # it for tier-3 model_settings resolution
    memory: bool = False                  # whether the agent opted into persistent
                                          # memory; the bridge gates shipping the
                                          # memory-prompt template in deps on this
                                          # (ingress._memory_prompt_deps). Defaulted
                                          # so events from an older agent that predates
                                          # this field deserialize as memory-off rather
                                          # than failing the schema_version gate.
    emitted_at: datetime
    cause: StateEventCause


class AgentDepartureEvent(BaseModel):
    kind: Literal["departure"] = "departure"
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    agent_id: str
    departed_at: datetime
    reason: Literal["shutdown"] = "shutdown"   # leave room for "heartbeat_loss" later


AgentStateMessage = Annotated[
    AgentStateEvent | AgentDepartureEvent,
    Field(discriminator="kind"),
]
