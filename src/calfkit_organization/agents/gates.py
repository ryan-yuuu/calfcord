"""Reusable gate predicates for calfkit agents that consume Discord wire events.

Two predicates, both factory-bound to a specific ``agent_id``:

- :func:`make_addressable_gate` — rejects events the agent should never respond
  to: its own persona webhook messages (self-recognition) and unknown bots.
- :func:`make_addressed_to_me_gate` — rejects slash invocations targeted at
  other agents; accepts non-slash messages unconditionally (assumes upstream
  channel-membership filtering).

Both gates read the bridge's :class:`~calfkit_organization.bridge.wire.WireMessage`
out of ``ctx.deps.provided_deps["discord"]``. A missing or non-mapping
``"discord"`` dep is treated as a reject — the agent must not act on events
that did not originate from the bridge.

Gates stack with AND semantics at registration time (see
``calfkit.nodes.base.BaseNodeDef.gate``). Register the addressable gate
*first* so the cheaper self/bot rejection short-circuits before the
content-based addressed-to-me check.

Per the calfkit gate contract these predicates are pure: they only read
``ctx`` and return ``bool``. No state mutation, no side effects.
"""

from __future__ import annotations

from collections.abc import Callable

from calfkit.models import SessionRunContext


def make_addressable_gate(agent_id: str) -> Callable[[SessionRunContext], bool]:
    """Build a gate that rejects self-emissions and unknown bots.

    Decision logic:
        - ``author.agent_id == agent_id`` → reject (this agent's own persona;
          prevents self-reply loops).
        - ``author.is_bot == True AND author.agent_id is None`` → reject.
          Covers the bridge's own non-webhook messages and any third-party
          bots that are not registered agents.
        - everything else → accept. Humans pass; recognized peer agents pass.
    """

    def addressable(ctx: SessionRunContext) -> bool:
        discord = ctx.deps.provided_deps.get("discord")
        if not isinstance(discord, dict):
            return False
        author = discord.get("author", {})
        if author.get("agent_id") == agent_id:
            return False
        if author.get("is_bot", False) and not author.get("agent_id"):
            return False
        return True

    addressable.__name__ = f"addressable_{agent_id}"
    return addressable


def make_addressed_to_me_gate(agent_id: str) -> Callable[[SessionRunContext], bool]:
    """Build a gate that requires slash invocations to target this agent
    and rejects ambient peer-agent traffic.

    Decision logic:
        - ``kind == "slash" AND slash_target == agent_id`` → accept.
        - ``kind == "slash" AND slash_target != agent_id`` → reject (slash
          was for some other agent; this includes slashes posted by peer
          agents' personas, since the slash target is the source of truth).
        - ``kind == "message"`` (ambient channel traffic, no @-mention):
          - author has ``agent_id`` (a peer agent's persona) → reject.
            Without an explicit address (@-mention or native slash),
            agent-to-agent ambient chatter would cascade into reply
            storms. Self-recognition is already handled by
            :func:`make_addressable_gate`, so reaching this branch means
            the message came from a DIFFERENT registered agent.
          - else (human or unrecognized author) → accept.
    """

    def addressed_to_me(ctx: SessionRunContext) -> bool:
        discord = ctx.deps.provided_deps.get("discord")
        if not isinstance(discord, dict):
            return False
        if discord.get("kind") == "slash":
            return discord.get("slash_target") == agent_id
        author = discord.get("author", {})
        if author.get("agent_id") is not None:
            return False
        return True

    addressed_to_me.__name__ = f"addressed_to_me_{agent_id}"
    return addressed_to_me
