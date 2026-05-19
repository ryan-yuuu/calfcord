"""Construct a runnable calfkit :class:`Worker` from an :class:`AgentDefinition`.

The factory builds a vanilla :class:`calfkit.Agent` node — no subclassing —
configured to subscribe to ``discord.channel.{cid}.in`` for each channel in
the agent's persisted state. The agent's identity rides on every outbound
publish via calfkit's ``x-calf-emitter`` Kafka header, so the bridge egress
can resolve the responding agent's persona from
``NodeResult.emitter_node_id`` without any application-level identity
stamping.

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

from calfkit.client import Client
from calfkit.nodes import Agent
from calfkit.providers import AnthropicModelClient, OpenAIModelClient
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient
from calfkit.worker import Worker

from calfkit_organization.agents.definition import AgentDefinition, Provider
from calfkit_organization.agents.gates import make_addressable_gate, make_addressed_to_me_gate
from calfkit_organization.agents.state import AgentRuntimeState, AgentStateStore
from calfkit_organization.agents.thinking import build_model_settings
from calfkit_organization.discord.persona import DiscordPersonaSender

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER: Provider = "anthropic"
"""Project-wide default LLM provider when neither ``definition.provider``
nor the ``CALFKIT_AGENT_DEFAULT_PROVIDER`` env var is set. Public so the
bridge can pin to the same value as the agent runner."""

_DEFAULT_PROVIDER_ENV_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"
_DEFAULT_MODEL_ENV_VAR = "CALFKIT_AGENT_DEFAULT_MODEL"
_DEFAULT_SUBSCRIBE_TOPIC_TEMPLATE = "discord.channel.{cid}.in"

_PROVIDER_DEFAULT_MODELS: dict[Provider, str] = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-5-mini",
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
        persona_sender: DiscordPersonaSender,
        calfkit_client: Client,
        *,
        default_provider: Provider = DEFAULT_PROVIDER,
        default_model: str | None = None,
        model_client_factory: ModelClientFactory | None = None,
        subscribe_topic_template: str = _DEFAULT_SUBSCRIBE_TOPIC_TEMPLATE,
    ) -> None:
        """Construct an agent factory.

        Args:
            persona_sender: Held for future use (e.g. when agents gain
                direct-send capabilities for non-bridge-mediated flows).
                Currently unused — the bridge's own persona sender posts
                replies. Accepted here so the constructor signature stays
                stable as the factory's responsibilities grow.
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
        """
        self._persona_sender = persona_sender
        self._calfkit_client = calfkit_client
        self._default_provider = default_provider
        self._default_model = default_model
        self._model_client_factory = model_client_factory or _default_model_client_factory
        self._subscribe_topic_template = subscribe_topic_template

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
        state: AgentRuntimeState,
        store: AgentStateStore,
    ) -> Agent:
        """Build one :class:`Agent` node from ``definition`` and ``state``.

        Use this when assembling several agents into a single :class:`Worker`
        (multi-node co-tenancy). Each node gets its own Kafka consumer group
        keyed on its ``node_id``, so co-tenant nodes do not contend for
        partitions.

        Raises:
            ValueError: If ``state.channels`` is empty, or if the resolved
                provider isn't one of :data:`_PROVIDER_DEFAULT_MODELS`
                (typically only reachable via env-var override with an
                unknown value).
        """
        if not state.channels:
            raise ValueError(
                f"agent {definition.agent_id!r} has no channels in state; "
                "an agent must subscribe to at least one channel"
            )
        if definition.tools:
            logger.warning(
                "agent %r declares tools=%s but tools are not wired in v1; ignoring",
                definition.agent_id,
                list(definition.tools),
            )

        provider = self._resolve_provider(definition)
        model_name = self._resolve_model(definition, provider)
        subscribe_topics = [
            self._subscribe_topic_template.format(cid=cid) for cid in state.channels
        ]
        model_settings = build_model_settings(provider, definition.thinking_effort)

        logger.info(
            "building agent=%s provider=%s model=%s topics=%s thinking_effort=%s",
            definition.agent_id,
            provider,
            model_name,
            subscribe_topics,
            definition.thinking_effort,
        )

        agent = Agent(
            node_id=definition.agent_id,
            system_prompt=definition.system_prompt,
            subscribe_topics=subscribe_topics,
            model_client=self._model_client_factory(provider, model_name),
            model_settings=model_settings,
        )
        agent.gate(make_addressable_gate(definition.agent_id))
        agent.gate(make_addressed_to_me_gate(definition.agent_id))

        # `store` is accepted for forward compatibility. Calfkit's Worker
        # registers subscribers once at start; runtime channel changes are
        # not yet wired (see module docstring).
        del store

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
