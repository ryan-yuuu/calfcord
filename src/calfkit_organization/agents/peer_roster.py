"""Build the per-invocation peer roster injected as ``temp_instructions``.

A2A-enabled agents (those whose ``.md`` declares ``tools: [private_chat]``)
need to know which peers exist before they can sensibly call the
``private_chat`` tool. We surface that roster by injecting it as
``temp_instructions`` on each invocation rather than baking it into the
system prompt at agent build time — that way a new agent added to the
:class:`AgentRegistry` becomes visible to existing peers on the very
next invocation, no agent restarts needed.

The registry is the single source of truth. Today's
:class:`AgentRegistry` is loaded once from ``agents/*.md`` at process
boot, so true hot-add still requires the bridge / tools runner to refresh
its registry — this helper is forward-compatible with that capability
once it lands.

Cost model: the roster is only attached when the target agent has the
``private_chat`` tool, so agents that don't use A2A pay zero token cost
for the feature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import is type-checking-only so we don't pull bridge.* (and its
    # transitive chain through gateway → ingress → back into this module)
    # at module load. ``from __future__ import annotations`` keeps the
    # function signature working without the runtime import.
    from calfkit_organization.bridge.registry import AgentRegistry

_PRIVATE_CHAT_TOOL_NAME = "private_chat"


def build_temp_instructions(
    registry: AgentRegistry,
    target_agent_id: str,
) -> str | None:
    """Return the ``temp_instructions`` to inject for an invocation of ``target_agent_id``.

    Returns ``None`` when no instructions are needed, i.e. when the
    target agent does not declare ``private_chat`` in its tools or when
    the registry has no peers to advertise. Callers can pass the result
    straight through to :meth:`calfkit.client.Client.invoke_node`
    (``temp_instructions=None`` is a no-op there).

    Args:
        registry: Source of truth for which agents exist and what tools
            they have. Read fresh on every call so a future hot-add
            mechanism on the registry takes effect without changes here.
        target_agent_id: The agent the invocation will be delivered to.
            Excluded from the roster — an agent never needs to be told
            it can talk to itself, and ``private_chat`` rejects
            self-targets anyway.

    Returns:
        A short multi-line instructions block listing each peer's id
        and description, or ``None`` if the target doesn't use A2A
        tools or has no peers to call.
    """
    target = registry.by_id(target_agent_id)
    if target is None:
        return None
    if _PRIVATE_CHAT_TOOL_NAME not in target.tools:
        return None
    peers = [spec for spec in registry.all() if spec.agent_id != target_agent_id]
    if not peers:
        return None
    lines = [f"- {spec.agent_id}: {spec.description}" for spec in peers]
    return (
        "Peer agents you can reach via the private_chat tool:\n"
        + "\n".join(lines)
    )
