"""Reusable gate predicates for calfkit agents that consume Discord wire events.

Two predicates, both factory-bound to a specific ``agent_id``:

- :func:`make_addressable_gate` — rejects events the agent should never respond
  to: its own persona webhook messages (self-recognition) and unknown bots.
- :func:`make_addressed_to_me_gate` — accepts only slash invocations whose
  ``slash_target`` matches this agent. Every other envelope on the channel
  topic — including raw ambient ``kind="message"`` — is rejected.

Both gates read the bridge's :class:`~calfcord.bridge.wire.WireMessage`
out of ``ctx.deps["discord"]``. A missing or non-mapping
``"discord"`` dep is treated as a reject — the agent must not act on events
that did not originate from the bridge.

Gates stack with AND semantics at registration time (see
``calfkit.nodes.base.BaseNodeDef.gate``). Register the addressable gate
*first* so the cheaper self/bot rejection short-circuits before the
content-based addressed-to-me check.

Per the calfkit gate contract these predicates are pure: they only read
``ctx`` and return ``bool``. No state mutation, no side effects — except
DEBUG-level logging on rejection. The DEBUG log preserves audit-trail
visibility (an operator tailing per-agent logs sees *why* a given
envelope was skipped) without contaminating the gate's pure-predicate
semantics.

Why the addressed-to-me gate is strict slash-only:
    With the routing-agent topology, ambient (``kind="message"``) traffic is
    no longer published to channel topics at all. The bridge ingress now
    routes ambient envelopes to ``discord.ambient.in`` (the router's
    exclusive ingress); the router selects which assistants should respond
    and the fan-out @consumer republishes synthesized ``kind="slash"`` wires
    through ``bridge.synthesized.in``, which the bridge's synthesized-in
    consumer feeds back through ``BridgeIngress.handle()`` onto the channel
    topic. So every legitimate envelope reaching an assistant via the
    channel topic — whether from a real Discord slash, a router fan-out, or
    an A2A invocation from :func:`~calfcord.tools.builtin.private_chat`
    (which synthesizes ``kind="slash"`` with the target's id, see
    ``private_chat.py``'s ``model_copy(update=...)`` pattern) — is a slash
    wire. The "ambient on channel topic" path is gone; rejecting it
    defensively makes the contract explicit and any future regression
    visible in logs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from calfkit.models import SessionRunContext

logger = logging.getLogger(__name__)


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
        discord = ctx.deps.get("discord")
        if not isinstance(discord, dict):
            return False
        author = discord.get("author", {})
        if author.get("agent_id") == agent_id:
            return False
        return not (author.get("is_bot", False) and not author.get("agent_id"))

    addressable.__name__ = f"addressable_{agent_id}"
    return addressable


def make_addressed_to_me_gate(agent_id: str) -> Callable[[SessionRunContext], bool]:
    """Build a gate that requires the envelope be a slash targeting this agent.

    Decision logic:
        - ``kind == "slash" AND slash_target == agent_id`` → accept.
        - anything else → reject. This includes:
            * ``kind == "slash"`` with a different ``slash_target`` (some other
              agent's invocation, or a peer agent's persona-posted slash);
            * ``kind == "message"`` regardless of author. Ambient channel
              traffic no longer reaches assistants directly — the bridge
              routes ambient to the router, which fans out synthesized
              ``kind="slash"`` wires per chosen agent. An assistant seeing
              raw ``kind="message"`` on its channel topic indicates a
              topology bug (e.g. the synthesized-in consumer missed a
              wire, or the bridge ingress's kind-branch regressed);
              rejecting keeps the agent silent and surfaces the issue via
              the absence of a reply rather than a runaway broadcast.

    A2A (peer→peer) compatibility:
        :func:`~calfcord.tools.builtin.private_chat.private_chat`
        synthesizes its outgoing envelope with ``kind="slash"`` and
        ``slash_target`` pointed at the target agent (see
        ``private_chat.py``'s ``WireMessage.model_copy(update=...)``
        block). That path continues to satisfy this gate unchanged — A2A
        does NOT depend on the removed ambient branch.

    Router fan-out compatibility:
        The router's fan-out @consumer synthesizes a fresh wire per chosen
        agent with ``kind="slash"`` and ``slash_target=<chosen agent>``;
        the bridge's synthesized-in consumer feeds these through
        ``BridgeIngress.handle()`` onto the channel topic. They reach the
        assistant looking identical to a real Discord slash and the gate
        accepts them on ``slash_target`` match.
    """

    def addressed_to_me(ctx: SessionRunContext) -> bool:
        discord = ctx.deps.get("discord")
        if not isinstance(discord, dict):
            return False
        kind = discord.get("kind")
        if kind != "slash":
            # A non-slash envelope on the channel topic is supposed
            # to be unreachable in the current topology (ambient
            # routes via ``discord.ambient.in`` → router → fan-out →
            # ``bridge.synthesized.in`` → channel topic, and the
            # last hop always synthesizes ``kind="slash"``). If we
            # land here, something upstream regressed. ERROR (not
            # WARN) because under the new topology a hit here is
            # ALWAYS a bug: the prior ambient-on-channel-topic path
            # is gone, so this is no longer a "skip" — it's a
            # regression. The user-visible symptom is no reply
            # (which has no log otherwise), so an actionable
            # ERROR alert is the right shape.
            logger.error(
                "addressed_to_me_%s reject: kind=%r (expected 'slash') "
                "event_id=%s — indicates a topology regression in the "
                "ambient → router → fan-out → synthesized-in chain",
                agent_id,
                kind,
                discord.get("event_id"),
            )
            return False
        return discord.get("slash_target") == agent_id

    addressed_to_me.__name__ = f"addressed_to_me_{agent_id}"
    return addressed_to_me
