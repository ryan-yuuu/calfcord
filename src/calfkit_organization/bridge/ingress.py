"""Discord → calfkit publish path: fire-and-forget agent invocation.

The bridge publishes the normalized :class:`WireMessage` to the agent
ingress topic and returns immediately. Replies land on ``discord.outbox``
and are posted by the outbox consumer
(:mod:`calfkit_organization.bridge.outbox`) — every reply, not just the
first to win the calfkit reply dispatcher's ``correlation_id`` race.

Two pieces of state cross the ingress→egress boundary:

* ``deps={"discord": wire}`` rides on the envelope so the agent's gates
  (:mod:`calfkit_organization.agents.gates`) can read it. The dep
  survives the agent's :class:`ReturnCall` republish into
  ``discord.outbox`` because :meth:`BaseNodeDef._publish_action` carries
  ``envelope.context.deps`` forward.
* The same wire is also written to a process-local :class:`PendingWires`
  map keyed on ``correlation_id``. The outbox consumer reads this map
  to recover the channel id / message id / author info it needs for
  the Discord post — :class:`~calfkit.NodeResult` doesn't expose
  ``Envelope.context.deps``. See :mod:`pending_wires` for the rationale.

Per-call thinking-effort overrides: when ``wire.slash_target`` is set
we read the target agent's current ``thinking_effort`` from the
in-memory registry and attach a provider-specific ``model_settings``
dict to the invocation so the agent uses the configured effort on this
exact call. Ambient messages flow without an override (the agent falls
back to whatever was baked into its model client at boot).
"""

from __future__ import annotations

import logging
from typing import Any

from calfkit.client import Client

from calfkit_organization.agents.definition import Provider
from calfkit_organization.agents.factory import DEFAULT_PROVIDER, resolve_provider
from calfkit_organization.agents.peer_roster import build_temp_instructions
from calfkit_organization.agents.thinking import build_model_settings
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireMessage

logger = logging.getLogger(__name__)

_DEFAULT_INGRESS_TOPIC_TEMPLATE = "discord.channel.{cid}.in"


class BridgeIngress:
    """Publish inbound Discord events as agent invocations.

    Fire-and-forget. The eventual reply (or replies — multi-agent flows
    are the point of the migration) is posted by the outbox consumer,
    not by this class. The slash command path and the ambient-message
    path share this single handler.
    """

    def __init__(
        self,
        calfkit_client: Client,
        registry: AgentRegistry,
        pending_wires: PendingWires,
        *,
        default_provider: Provider = DEFAULT_PROVIDER,
        ingress_topic_template: str = _DEFAULT_INGRESS_TOPIC_TEMPLATE,
    ) -> None:
        self._client = calfkit_client
        self._registry = registry
        self._pending_wires = pending_wires
        self._default_provider = default_provider
        self._ingress_topic_template = ingress_topic_template
        # Validate every agent's provider at boot so a typo'd
        # CALFKIT_AGENT_DEFAULT_PROVIDER surfaces here (fail-fast) rather
        # than as an uncaught ValueError inside every targeted invocation.
        for spec in registry.all():
            resolve_provider(spec, default_provider=default_provider)

    async def handle(self, wire: WireMessage) -> None:
        """Publish ``wire`` to its channel's ingress topic. Fire-and-forget.

        Writes the wire to :class:`PendingWires` *before* publishing so a
        fast agent reply can never race the consumer's lookup. If the
        publish itself raises, the entry is popped so it doesn't waste an
        LRU slot waiting for a reply that will never come.

        Per-call ``model_settings`` resolution: when
        ``wire.slash_target`` is set, we look up the target agent's
        current ``thinking_effort`` in the registry and build a
        provider-specific override. Ambient messages send no override.

        Cancelling the invocation handle's future immediately after publish
        is load-bearing: :meth:`Client.invoke_node` unconditionally registers
        a pending future with the reply dispatcher (the dispatcher will
        otherwise resolve+pop it on the first reply, but a no-reply event
        would leak ``_pending`` forever, and a redelivered ``correlation_id``
        would raise inside ``_ReplyDispatcher.expect``). Cancelling the
        future triggers its ``add_done_callback`` to pop the registry entry
        synchronously. The bridge's egress is the outbox consumer in a
        different consumer group, so the actual reply is still observed.
        """
        model_settings = self._resolve_model_settings(wire)
        temp_instructions = self._resolve_temp_instructions(wire)
        self._pending_wires.put(wire.event_id, wire)
        try:
            handle = await self._client.invoke_node(
                user_prompt=wire.content,
                topic=self._ingress_topic_template.format(cid=wire.channel_id),
                correlation_id=wire.event_id,
                deps={"discord": wire.model_dump(mode="json")},
                output_type=str,
                model_settings=model_settings,
                temp_instructions=temp_instructions,
            )
        except Exception:
            # Publish failed; the agent will not run, so no reply will
            # ever look up this wire. Free the slot.
            self._pending_wires.pop(wire.event_id)
            logger.exception(
                "ingress publish failed event_id=%s channel=%s",
                wire.event_id,
                wire.channel_id,
            )
            raise

        handle._future.cancel()

    def _resolve_temp_instructions(self, wire: WireMessage) -> str | None:
        """Compute the per-call ``temp_instructions`` for ``wire``.

        Only returns content for slash invocations (where we know exactly
        which agent will pick the call up). For ambient channel messages
        the same envelope reaches every subscriber, so we can't tailor a
        per-target roster — those agents fall back to the reactive error
        path on ``private_chat`` if they reach for it.

        Reads the registry on every call so a future hot-add mechanism
        (the registry itself doesn't support hot-add yet, but this code
        path is ready when it does) takes effect immediately.
        """
        target = wire.slash_target
        if target is None:
            return None
        return build_temp_instructions(self._registry, target)

    def _resolve_model_settings(self, wire: WireMessage) -> dict[str, Any] | None:
        """Compute per-call ``model_settings`` for ``wire``, or ``None``.

        Reads the target agent's current ``thinking_effort`` from the
        in-memory registry (kept fresh by
        :meth:`AgentRegistry.set_thinking_effort`). Returns ``None`` for
        ambient messages and for any error — the agent then falls back
        to whatever was baked into its model client at boot.
        """
        target = wire.slash_target
        if target is None:
            return None

        spec = self._registry.by_id(target)
        if spec is None:
            logger.error(
                "slash_target=%r missing from registry event_id=%s; "
                "operator effort tier will not apply",
                target,
                wire.event_id,
            )
            return None

        try:
            provider = resolve_provider(spec, default_provider=self._default_provider)
            return build_model_settings(provider, spec.thinking_effort)
        except ValueError as e:
            # resolve_provider raises on a typo'd CALFKIT_AGENT_DEFAULT_PROVIDER
            # (boot validates the steady state, but env can drift); the mapper
            # raises on an unknown provider. Neither should fail the LLM call.
            logger.warning(
                "model_settings resolution failed for agent=%s event_id=%s "
                "cause=%s; falling back to model client defaults",
                target,
                wire.event_id,
                type(e).__name__,
                exc_info=True,
            )
            return None
