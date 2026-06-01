"""Pure projection helpers between AgentDefinition and AgentStateEvent.

Used by the agent process when announcing (definition -> event) and by the bridge
when projecting received events into its registry (event -> definition).

The event form excludes agent-internal fields (system_prompt, tools, model,
publish_topic, source_path). The bridge stubs them with sentinel values when
rebuilding an AgentDefinition -- those fields are never read on the bridge side
but the AgentDefinition validator still requires legal values.
"""
from __future__ import annotations

from datetime import UTC, datetime

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.control_plane.schema import AgentStateEvent, StateEventCause


def build_state_event(
    definition: AgentDefinition, cause: StateEventCause
) -> AgentStateEvent:
    """Project an AgentDefinition into a wire-friendly AgentStateEvent.

    Agent-internal fields (system_prompt, tools, model, publish_topic, source_path)
    are intentionally excluded -- the bridge never reads them.
    """
    return AgentStateEvent(
        agent_id=definition.agent_id,
        display_name=definition.display_name,
        description=definition.description,
        avatar_url=definition.avatar_url,
        role=definition.role,
        history_turns=definition.history_turns,
        thinking_effort=definition.thinking_effort,
        provider=definition.provider,
        memory=definition.memory,
        emitted_at=datetime.now(UTC),
        cause=cause,
    )


# Stubs used when the bridge rebuilds an AgentDefinition from a state event.
# These fields are never read on the bridge side; their values exist only to
# satisfy AgentDefinition's pydantic validators.
_BRIDGE_STUB_SYSTEM_PROMPT = "(agent-internal; bridge projection from state event)"


def state_event_to_definition(event: AgentStateEvent) -> AgentDefinition:
    """Reconstitute an AgentDefinition from a state event for bridge-side use.

    Stubs agent-internal fields. The reconstituted definition is valid (passes
    pydantic validation) and answers all the bridge-side accessors the registry
    and downstream code use. Do not call this in agent-side code paths -- agents
    have their own real AgentDefinition from disk.
    """
    return AgentDefinition(
        # AgentDefinition uses ``alias="name"`` for agent_id; populate_by_name=True
        # in its model_config means we can pass either; use the field name here.
        agent_id=event.agent_id,
        display_name=event.display_name,
        description=event.description,
        avatar_url=event.avatar_url,
        provider=event.provider,
        model=None,
        tools=(),
        thinking_effort=event.thinking_effort,
        role=event.role,
        publish_topic=None,   # state events come from assistants only; bridge
                              # doesn't receive router announcements
        history_turns=event.history_turns,
        memory=event.memory,  # the bridge reads this to decide whether to ship the
                              # memory-prompt template in deps. Safe to set even though
                              # tools is stubbed to (): the factory's memory-needs-fs-tools
                              # guard runs only in build_node (agent-runner side), and the
                              # bridge never builds nodes from these reconstituted defs.
        system_prompt=_BRIDGE_STUB_SYSTEM_PROMPT,
        source_path=None,
    )
