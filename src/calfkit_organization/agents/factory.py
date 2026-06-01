"""Construct a runnable calfkit :class:`Worker` from an :class:`AgentDefinition`.

The factory builds a vanilla :class:`calfkit.Agent` node — no subclassing —
configured to subscribe to:

* a per-agent **private return topic** ``{agent_id}.private.return`` at
  index ``[0]`` of ``subscribe_topics`` (so it is also the callback topic
  for tool ``Call`` and ``TailCall`` envelopes — see below);
* ``discord.channel.{cid}.in`` for each channel in the agent's persisted
  state;
* a single per-agent inbox topic ``agent.{agent_id}.in`` used by the
  ``calfkit-tools`` runner to invoke the agent for A2A traffic without
  round-tripping through Discord.

The agent's identity rides on every outbound publish via calfkit's
``x-calf-emitter`` Kafka header, so the bridge egress can resolve the
responding agent's persona from ``NodeResult.emitter_node_id`` without
any application-level identity stamping.

**Why the private return topic at index [0]:** calfkit's
:class:`~calfkit.nodes.base.BaseNodeDef` treats
``subscribe_topics[0]`` as the callback topic for tool ``Call``
envelopes and as the target for ``TailCall`` retries. When two
agents co-tenant on the same channel topic (the multi-agent ambient
fan-out this project relies on), making that shared channel the
callback would deliver each agent's tool return to *every*
co-tenant, causing each peer to also run its LLM on the originating
agent's state and emit a duplicate (and often incorrect) reply to
the user. Putting a per-agent private topic at ``[0]`` keeps tool
returns scoped to the agent that initiated the call; the channel
topics still live at ``[1:]`` so ambient fan-out is unaffected. The
name matches calfkit's own :attr:`BaseNodeDef._return_topic`
attribute so this workaround dovetails with an upstream fix that
wires the attribute in.

Tools declared in the agent's ``.md`` frontmatter under ``tools:`` are
resolved against :data:`calfkit_organization.tools.TOOL_REGISTRY` and
passed to the calfkit ``Agent`` constructor. Each agent only carries the
tool's :class:`~calfkit.nodes.ToolNodeDef` for schema + subscribe-topic
purposes — the actual tool body runs in the ``calfkit-tools`` deployment.

Two public entry points:

* :meth:`AgentFactory.build_node` returns a bare :class:`Agent`. The
  ``calfkit-agent`` runner uses this in both single-agent and all-agents
  modes — the runner constructs the :class:`Worker` itself so it can pack
  one or many nodes into a single Worker depending on invocation.
* :meth:`AgentFactory.build` is a thin convenience that wraps
  :meth:`build_node` in a one-node :class:`Worker`. Kept for callers that
  want a complete Worker without assembling it themselves (and for the
  existing ``test_factory.py`` suite); the in-tree runner does not use
  it.

The factory dispatches on :attr:`AgentDefinition.provider` to choose between
:class:`calfkit.AnthropicModelClient` and :class:`calfkit.OpenAIModelClient`.
Each provider has a default model name baked in (see
:data:`_PROVIDER_DEFAULT_MODELS`); a definition's ``model`` field wins, the
``CALFKIT_AGENT_DEFAULT_MODEL`` env var is the next fallback, and the
provider-specific default is the last resort.

The factory does NOT manage the lifecycle of its dependencies
(``DiscordPersonaSender`` and calfkit :class:`Client`) — they are owned by
:mod:`calfkit_organization.agents.runner` and passed in already-started.

**Worker subscription is fixed at boot.** Calfkit's
:meth:`Worker.register_handlers` is one-shot, so mutating
``state.channels`` at runtime (via ``store.add_channel``/``store.remove_channel``)
does not change the running agent's Kafka subscriptions. Adding a channel
to an existing agent requires a process restart. The ``store`` parameter
is accepted into :meth:`build` for forward compatibility but is not used
in v1 — ``thinking_effort`` (the other runtime-tunable knob) lives in the
``.md`` frontmatter rather than the state file, so the store is purely a
channels reader.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from calfkit._vendor.pydantic_ai import ToolOutput
from calfkit.client import Client
from calfkit.nodes import Agent
from calfkit.nodes.tool import ToolNodeDef
from calfkit.providers import AnthropicModelClient, OpenAIModelClient
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient
from calfkit.worker import Worker

from calfkit_organization.agents.definition import AgentDefinition, Provider
from calfkit_organization.agents.gates import make_addressable_gate, make_addressed_to_me_gate
from calfkit_organization.agents.memory import memory_instructions
from calfkit_organization.agents.routing import ROUTER_OUTPUT_TOOL_NAME, RoutingDecision
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore
from calfkit_organization.agents.thinking import build_model_settings
from calfkit_organization.discord.persona import DiscordPersonaSender
from calfkit_organization.topics import AGENT_STEPS_TOPIC, AMBIENT_INGRESS_TOPIC

# NOTE: ``TOOL_REGISTRY`` is imported lazily inside :meth:`AgentFactory.__init__`.
# Tool modules transitively import bridge code, and bridge imports agents.factory
# for DEFAULT_PROVIDER/resolve_provider — a top-level import here would cycle.
# Lazy import defers the resolution to factory construction time, by which point
# all modules have finished loading.

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER: Provider = "anthropic"
"""Project-wide default LLM provider when neither ``definition.provider``
nor the ``CALFKIT_AGENT_DEFAULT_PROVIDER`` env var is set. Public so the
bridge can pin to the same value as the agent runner."""

_DEFAULT_PROVIDER_ENV_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"
_DEFAULT_MODEL_ENV_VAR = "CALFKIT_AGENT_DEFAULT_MODEL"
_DEFAULT_SUBSCRIBE_TOPIC_TEMPLATE = "discord.channel.{cid}.in"
_AGENT_INBOX_TOPIC_TEMPLATE = "agent.{agent_id}.in"
"""Per-agent private inbox topic. The ``calfkit-tools`` runner publishes
A2A invocations here so an agent can be reached without routing through
Discord. Subscribed to by every agent in addition to its channel topics."""
_PRIVATE_RETURN_TOPIC_TEMPLATE = "{agent_id}.private.return"
"""Per-agent private return topic. Placed at ``subscribe_topics[0]`` so
calfkit's :class:`~calfkit.nodes.base.BaseNodeDef` uses it as the
callback for tool ``Call`` envelopes (and as the ``TailCall`` retry
target). Only this agent's Worker subscribes to the topic, so a tool
return cannot leak into a co-tenant agent's handler — see the module
docstring for the failure mode this prevents. Matches calfkit's own
:attr:`BaseNodeDef._return_topic` attribute (defined but not yet
wired into the publish path) for forward compatibility with an
upstream fix."""

_PROVIDER_DEFAULT_MODELS: dict[Provider, str] = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-5-mini",
    "openai-codex": "gpt-5.3-codex",
}
"""Default model name per provider when neither ``definition.model`` nor
``CALFKIT_AGENT_DEFAULT_MODEL`` is set. Each provider's model namespace is
disjoint, so a single cross-provider default is meaningless — picking one
per provider is the only sensible fallback."""

ModelClientFactory = Callable[[Provider, str], PydanticModelClient]
"""Construct a model client for ``(provider, model_name)``. The default
factory dispatches on ``provider``; tests override this to inject fakes."""


def _default_model_client_factory(provider: Provider, model_name: str) -> PydanticModelClient:
    """Map ``provider`` to its concrete calfkit model-client class.

    Provider authentication is read from each SDK's standard env var
    (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``); the factory does not
    handle keys. A missing key surfaces on first invocation, not at
    construction.
    """
    if provider == "anthropic":
        return AnthropicModelClient(model_name=model_name)
    if provider == "openai":
        return OpenAIModelClient(model_name=model_name)
    if provider == "openai-codex":
        # Lazy import: pulls in authlib + OpenHands auth machinery only when
        # an agent actually opts in to ChatGPT subscription billing.
        from calfkit_organization.providers.codex import build_codex_subscription_client

        return build_codex_subscription_client(model_name=model_name)
    # Unreachable when ``provider`` is typed as Provider; defensive for
    # runtime callers that bypass the Literal.
    raise ValueError(
        f"unknown provider {provider!r}; expected one of {list(_PROVIDER_DEFAULT_MODELS)}"
    )


def resolve_provider(
    definition: AgentDefinition,
    *,
    default_provider: Provider = DEFAULT_PROVIDER,
) -> Provider:
    """Resolve the LLM provider for ``definition`` using the standard fallback chain.

    Precedence (first non-empty wins):
        1. ``definition.provider``
        2. ``os.environ["CALFKIT_AGENT_DEFAULT_PROVIDER"]``
        3. ``default_provider``

    Raises:
        ValueError: if the resolved value isn't one of the known providers
            (typically only reachable via env-var override with an unknown
            value).
    """
    raw = definition.provider or os.getenv(_DEFAULT_PROVIDER_ENV_VAR) or default_provider
    if raw not in _PROVIDER_DEFAULT_MODELS:
        raise ValueError(
            f"unknown provider {raw!r} for agent {definition.agent_id!r}; "
            f"expected one of {sorted(_PROVIDER_DEFAULT_MODELS)}"
        )
    return raw  # type: ignore[return-value]


class AgentFactory:
    """Builds a :class:`Worker` running one LLM-backed agent.

    Identity carriage:
        The constructed :class:`Agent` is unmodified calfkit; calfkit
        stamps ``x-calf-emitter`` / ``x-calf-emitter-kind`` Kafka headers on
        every outbound publish, so the bridge resolves the responding agent
        via :attr:`NodeResult.emitter_node_id` and looks up the persona in
        the :class:`AgentRegistry`.

    Provider selection priority (first non-empty wins):
        1. ``definition.provider``
        2. ``os.environ["CALFKIT_AGENT_DEFAULT_PROVIDER"]``
        3. ``default_provider`` constructor argument (defaults to
           ``"anthropic"``)

    Model selection priority (first non-empty wins):
        1. ``definition.model``
        2. ``os.environ["CALFKIT_AGENT_DEFAULT_MODEL"]``
        3. ``default_model`` constructor argument (if set)
        4. The resolved provider's entry in
           :data:`_PROVIDER_DEFAULT_MODELS`
    """

    def __init__(
        self,
        persona_sender: DiscordPersonaSender | None,
        calfkit_client: Client,
        *,
        default_provider: Provider = DEFAULT_PROVIDER,
        default_model: str | None = None,
        model_client_factory: ModelClientFactory | None = None,
        subscribe_topic_template: str = _DEFAULT_SUBSCRIBE_TOPIC_TEMPLATE,
        tool_registry: dict[str, ToolNodeDef] | None = None,
    ) -> None:
        """Construct an agent factory.

        Args:
            persona_sender: Held for future use (e.g. when agents gain
                direct-send capabilities for non-bridge-mediated flows).
                Currently unused — the bridge's own persona sender posts
                replies, and the router build path doesn't post to
                Discord at all. Pass ``None`` from the router runner;
                pass a real :class:`DiscordPersonaSender` from the
                assistant runner so this stays available when the
                "future use" arrives.
            calfkit_client: The calfkit :class:`Client` the agent worker
                connects through.
            default_provider: Fallback provider when neither
                ``definition.provider`` nor the env var is set.
            default_model: Optional override for the model fallback. When
                ``None``, the provider's entry in
                :data:`_PROVIDER_DEFAULT_MODELS` is used. When set, this
                value wins over the provider default but still loses to
                ``definition.model`` and the env var. Note that a
                cross-provider default may not be valid for every provider
                (e.g. a Claude model name will fail against OpenAI).
            model_client_factory: Optional override for model-client
                construction. Used by tests to inject a fake without
                building a real provider client.
            subscribe_topic_template: Format string with ``{cid}`` placeholder,
                applied to each channel id in ``state.channels`` to build the
                agent's subscribe topics. Mirrors the bridge's publish topic.
            tool_registry: Map of tool name → :class:`ToolNodeDef` used to
                resolve names declared in ``definition.tools``. Defaults to
                the module-level :data:`TOOL_REGISTRY`; tests pass a
                fixture-built dict.
        """
        self._persona_sender = persona_sender
        self._calfkit_client = calfkit_client
        self._default_provider = default_provider
        self._default_model = default_model
        self._model_client_factory = model_client_factory or _default_model_client_factory
        self._subscribe_topic_template = subscribe_topic_template
        if tool_registry is None:
            from calfkit_organization.tools import TOOL_REGISTRY

            tool_registry = TOOL_REGISTRY
        self._tool_registry = tool_registry

    def build(
        self,
        definition: AgentDefinition,
        state: AgentRuntimeState,
        store: AgentStateStore,
    ) -> Worker:
        """Build a single-agent :class:`Worker`.

        Thin convenience wrapper around :meth:`build_node` for callers
        that want a complete one-node Worker without assembling it
        themselves. The in-tree ``calfkit-agent`` runner does not use
        this — it calls :meth:`build_node` directly in both single-agent
        and all-agents modes so it can pack one or many nodes into a
        single Worker.

        Raises:
            ValueError: If ``state.channels`` is empty, or if the resolved
                provider isn't one of :data:`_PROVIDER_DEFAULT_MODELS`.
        """
        return Worker(self._calfkit_client, [self.build_node(definition, state, store)])

    def build_node(
        self,
        definition: AgentDefinition,
        state: AgentRuntimeState | None,
        store: AgentStateStore | None,
    ) -> Agent:
        """Build one :class:`Agent` node from ``definition`` and ``state``.

        Use this when assembling several agents into a single :class:`Worker`
        (multi-node co-tenancy). Each node gets its own Kafka consumer group
        keyed on its ``node_id``, so co-tenant nodes do not contend for
        partitions.

        Routers (``definition.role == "router"``) take a separate build
        path: single-topic subscription on the configured ambient topic,
        no standard gates, ``ToolOutput`` final-output type, explicit
        ``publish_topic``. See :meth:`_build_router_node`. Routers
        accept ``state=None``/``store=None`` because their subscription
        list does not depend on per-channel state.

        Raises:
            ValueError: If ``definition.role == "assistant"`` and
                ``state`` is ``None`` or ``state.channels`` is empty
                (assistants must subscribe to at least one channel),
                or if the resolved provider isn't one of
                :data:`_PROVIDER_DEFAULT_MODELS` (typically only
                reachable via env-var override with an unknown
                value), or if a router definition violates
                router-specific invariants.
        """
        if definition.role == "router":
            return self._build_router_node(definition)

        if state is None or not state.channels:
            raise ValueError(
                f"agent {definition.agent_id!r} has no channels in state; "
                "an assistant must subscribe to at least one channel"
            )
        tools = self._resolve_tools(definition)
        self._require_memory_tools(definition, tools)

        provider = self._resolve_provider(definition)
        model_name = self._resolve_model(definition, provider)
        # Per-agent private return topic at index [0]: calfkit uses
        # ``subscribe_topics[0]`` as the callback for tool ``Call``
        # envelopes and the ``TailCall`` retry target. Channel topics
        # are shared across co-tenant agents (the ambient fan-out
        # pattern), so they must NOT be index 0 or each agent would
        # receive the others' tool returns and emit duplicate replies.
        # See the module docstring for the full failure mode.
        subscribe_topics = [
            _PRIVATE_RETURN_TOPIC_TEMPLATE.format(agent_id=definition.agent_id)
        ]
        subscribe_topics.extend(
            self._subscribe_topic_template.format(cid=cid) for cid in state.channels
        )
        # Per-agent inbox: the ``calfkit-tools`` runner publishes A2A
        # invocations to this topic so the LLM-driven ``private_chat``
        # tool can reach this agent without round-tripping through Discord.
        subscribe_topics.append(_AGENT_INBOX_TOPIC_TEMPLATE.format(agent_id=definition.agent_id))
        model_settings = build_model_settings(provider, definition.thinking_effort)

        logger.info(
            "building agent=%s provider=%s model=%s topics=%s thinking_effort=%s tools=%s",
            definition.agent_id,
            provider,
            model_name,
            subscribe_topics,
            definition.thinking_effort,
            [t.tool_schema.name for t in tools] if tools else [],
        )

        # ``publish_topic=AGENT_STEPS_TOPIC`` makes FastStream mirror every
        # handler hop (``Call`` / ``TailCall`` / ``ReturnCall``) to a shared
        # audit feed the bridge's steps consumer subscribes to. The action-
        # specific publish (``ReturnCall`` → frame.callback_topic, ``Call`` →
        # tool topic) still happens through ``BaseNodeDef._publish_action``;
        # the publish_topic is an additional mirror, not a replacement.
        # The frontmatter-side ``publish_topic`` field stays assistant-
        # prohibited (``AgentDefinition._validate_router_constraints``)
        # because the injection here is a factory-level concern, not an
        # operator-tunable. See ``topics.AGENT_STEPS_TOPIC`` for the
        # single-partition operator requirement on this topic.
        agent = Agent(
            node_id=definition.agent_id,
            system_prompt=definition.system_prompt,
            subscribe_topics=subscribe_topics,
            publish_topic=AGENT_STEPS_TOPIC,
            model_client=self._model_client_factory(provider, model_name),
            model_settings=model_settings,
            tools=tools or None,
        )
        agent.gate(make_addressable_gate(definition.agent_id))
        agent.gate(make_addressed_to_me_gate(definition.agent_id))

        # Memory-enabled agents carry a dynamic-instructions hook. It reads
        # the bridge-injected template from ``deps`` at runtime, localizes it
        # to this agent's ``memory/<agent_id>/`` dir, and appends it to the
        # instructions. The agent process never reads the prompt file — only
        # the bridge does (see ``agents/memory.py``). ``_require_memory_tools``
        # above already guaranteed the agent has the fs tools the block tells
        # it to use.
        if definition.memory:
            agent.instructions(memory_instructions(definition.agent_id))

        # ``store`` is accepted for forward compatibility but unused;
        # see module docstring on why runtime channel changes aren't
        # wired yet. We deliberately do not bind it locally.
        _ = store

        return agent

    def _build_router_node(
        self,
        definition: AgentDefinition,
    ) -> Agent:
        """Build the router :class:`Agent` node.

        Router invariants enforced by :class:`AgentDefinition`:
            - ``tools`` is empty (the router uses :class:`ToolOutput`,
              not function tools)
            - ``publish_topic`` is non-None (declares the router's own
              output topic; the fan-out consumer subscribes there)

        Differences from the assistant build path:
            - No state channels required (router subscribes to a single
              fixed ambient ingress topic, not per-channel topics).
            - No standard gates (no self-recognition, no
              addressed-to-me check — the router is the only consumer
              of its ingress topic, so every envelope is for it).
            - ``final_output_type=ToolOutput(RoutingDecision,
              name=ROUTER_OUTPUT_TOOL_NAME)`` so the LLM's structured
              output terminates the agent loop in one turn (pydantic-ai
              recognizes the tool call as the agent's terminal output
              under ``end_strategy="early"``).
            - ``publish_topic`` is set, so FastStream's ``@publisher``
              wrapping mirrors the agent's ``ReturnCall`` to that topic.
              The duplicate publish to ``frame.callback_topic`` lands
              on a throwaway topic with no consumer (see
              ``bridge/ingress.py``).

        Router builds need no per-channel state (single fixed ambient
        topic) and no on-disk state store (router state is
        env-driven). Callers should reach this method via
        :meth:`build_node` with ``state=None``/``store=None`` rather
        than building a never-used :class:`AgentStateStore`.

        Note on env-var precedence: the router definition's
        ``provider``/``model`` are populated by
        :func:`build_router_definition` from ``CALFKIT_ROUTER_*``
        env vars (with defaults). Those values are always non-None,
        so the assistant-targeted ``CALFKIT_AGENT_DEFAULT_*`` env
        vars never apply on this path. A future refactor that lets
        the router definition leave ``provider`` or ``model`` as
        ``None`` would silently start picking up the assistant
        defaults — keep them non-None.
        """
        provider = self._resolve_provider(definition)
        model_name = self._resolve_model(definition, provider)
        model_settings = build_model_settings(provider, definition.thinking_effort)

        subscribe_topics = [AMBIENT_INGRESS_TOPIC]

        logger.info(
            "building router agent=%s provider=%s model=%s topic=%s publish_topic=%s",
            definition.agent_id,
            provider,
            model_name,
            subscribe_topics[0],
            definition.publish_topic,
        )

        agent = Agent(
            node_id=definition.agent_id,
            system_prompt=definition.system_prompt,
            subscribe_topics=subscribe_topics,
            publish_topic=definition.publish_topic,
            model_client=self._model_client_factory(provider, model_name),
            model_settings=model_settings,
            final_output_type=ToolOutput(RoutingDecision, name=ROUTER_OUTPUT_TOOL_NAME),
        )
        # No standard gates: the router is the only consumer of its
        # ingress topic, so every envelope is for it.
        # :meth:`BridgeIngress.handle`'s ambient branch already drops
        # ``is_bot`` / ``is_webhook`` authors before publishing
        # (preventing agent-on-agent reply storms), and a
        # "self-author" loop is impossible because the router never
        # publishes back to its own subscribe topic.
        return agent

    def _resolve_provider(self, definition: AgentDefinition) -> Provider:
        return resolve_provider(definition, default_provider=self._default_provider)

    def _resolve_model(self, definition: AgentDefinition, provider: Provider) -> str:
        return (
            definition.model
            or os.getenv(_DEFAULT_MODEL_ENV_VAR)
            or self._default_model
            or _PROVIDER_DEFAULT_MODELS[provider]
        )

    def _resolve_tools(self, definition: AgentDefinition) -> list[ToolNodeDef]:
        """Resolve ``definition.tools`` names against the tool registry.

        Semantics (mirrors :attr:`AgentDefinition.tools`):
            - ``None``: tools-by-default — every registered tool. This is
              the in-memory representation of the "no ``tools:`` line in
              frontmatter" case before the loader normalizes it. The
              loader normalizes to a concrete tuple, so a ``None`` reaching
              this method usually means a code-built definition bypassed
              the loader (e.g. a test or the router build path, which
              doesn't go through this method).
            - empty tuple ``()``: zero tools (explicit opt-out).
            - non-empty tuple: exactly those tools, with unknown-name
              validation.

        Raises:
            ValueError: if any declared name is missing from the registry.
                Lists every unknown name in one message so a multi-typo
                ``.md`` surfaces all of them in a single boot.
        """
        if definition.tools is None:
            return list(self._tool_registry.values())
        if not definition.tools:
            return []
        resolved: list[ToolNodeDef] = []
        unknown: list[str] = []
        for name in definition.tools:
            node = self._tool_registry.get(name)
            if node is None:
                unknown.append(name)
            else:
                resolved.append(node)
        if unknown:
            known = sorted(self._tool_registry)
            raise ValueError(
                f"agent {definition.agent_id!r} declares unknown tool(s) "
                f"{unknown!r}; known tools: {known or '<none registered>'}"
            )
        return resolved

    def _require_memory_tools(
        self, definition: AgentDefinition, tools: list[ToolNodeDef]
    ) -> None:
        """Reject a ``memory: true`` agent that lacks the filesystem tools memory needs.

        A memory-enabled agent manages its notepad with the general-purpose
        ``read_file`` / ``write_file`` tools (see
        ``docs/design/agent-memory-plan.md``); without them the injected memory
        instructions are a silent no-op, so fail loud at build time instead.
        Agents that omit ``tools:`` get every registered tool and pass
        automatically — only an explicitly-restricted ``tools:`` list can trip
        this. Operates on the resolved :class:`ToolNodeDef` list (not the raw
        names) so the "all tools" expansion is reflected correctly.
        """
        if not definition.memory:
            return
        available = {t.tool_schema.name for t in tools}
        missing = sorted({"read_file", "write_file"} - available)
        if missing:
            raise ValueError(
                f"agent {definition.agent_id!r} sets memory: true but is missing "
                f"required filesystem tool(s) {missing}; memory needs read_file and "
                f"write_file. Add them to the agent's tools:, or omit tools: to grant all."
            )
