"""Construct a runnable calfkit :class:`Worker` from an :class:`AgentDefinition`.

The factory builds a vanilla :class:`calfkit.Agent` node -- no subclassing --
addressed **by name** (calfkit name-addressing, ADR-0017). The agent declares
**no** channel ``subscribe_topics`` and **no** addressing gate: the bridge
reaches it directly by name on its automatic private input topic
``agent.{name}.private.input``. That topic -- and the agent's private return
topic ``{name}.private.return`` (calfkit's callback for tool ``Call`` /
``TailCall`` envelopes) -- are declared by the managed Worker's startup
provisioning pass, so nothing here lists them explicitly.

Agent-to-agent collaboration is native. ``peers=[Messaging(...)]`` injects
calfkit's built-in ``message_agent`` consult tool and ``peers=[Handoff(...)]``
enables transferring the caller's turn to a peer -- both driven by the
``a2a``/``handoff`` frontmatter fields via :func:`_build_peers`. calfkit
renders the live peer directory into the agent's prompt from the native
``AgentCard`` plane (which every agent advertises automatically), so the
factory also passes ``description=`` -- otherwise the agent's ``AgentCard``,
the mesh roster, and the ``message_agent`` directory would all render blank.

The agent's identity rides on every outbound publish via calfkit's
``x-calf-emitter`` Kafka header, so the bridge resolves the responding
agent's persona from the emitter without any application-level identity
stamping.

Tools declared in the agent's ``.md`` frontmatter under ``tools:`` are
resolved against :data:`calfcord.tools.TOOL_REGISTRY` and passed to the
calfkit ``Agent`` constructor; ``mcp/...`` selectors collapse into one
:class:`~calfkit.mcp.MCPToolbox` per server, resolved per turn against the
capability view. Each builtin agent only carries the tool's
:class:`~calfkit.nodes.ToolNodeDef` for schema purposes -- the actual tool
body runs in the ``calfkit-tools`` deployment.

Two public entry points:

* :meth:`AgentFactory.build_node` returns a bare :class:`Agent`. The
  ``calfkit-agent`` runner uses this in both single-agent and all-agents
  modes -- the runner constructs the :class:`Worker` itself so it can pack
  one or many nodes into a single Worker.
* :meth:`AgentFactory.build` is a thin convenience that wraps
  :meth:`build_node` in a one-node :class:`Worker`. Kept for callers that
  want a complete Worker without assembling it themselves; the in-tree
  runner does not use it.

The factory dispatches on :attr:`AgentDefinition.provider` to choose between
:class:`calfkit.AnthropicModelClient` and :class:`calfkit.OpenAIModelClient`.
Each provider has a default model name baked in (see
:data:`_PROVIDER_DEFAULT_MODELS`); a definition's ``model`` field wins, the
``CALFKIT_AGENT_DEFAULT_MODEL`` env var is the next fallback, and the
provider-specific default is the last resort.

The factory does NOT manage the lifecycle of its dependencies
(``DiscordPersonaSender`` and calfkit :class:`Client`) -- they are owned by
:mod:`calfcord.agents.runner` and passed in already-started.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from calfkit import Handoff, Messaging
from calfkit.client import Client
from calfkit.mcp import MCPToolbox
from calfkit.nodes import Agent
from calfkit.nodes.tool import ToolNodeDef
from calfkit.providers import AnthropicModelClient, OpenAIModelClient
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient
from calfkit.worker import Worker

from calfcord.agents.definition import AgentDefinition, Provider
from calfcord.agents.memory import memory_instructions
from calfcord.agents.thinking import build_model_settings
from calfcord.discord.persona import DiscordPersonaSender
from calfcord.mcp.agent_select import selectors_from_entries
from calfcord.mcp.selector import is_mcp_selector

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

_PROVIDER_DEFAULT_MODELS: dict[Provider, str | None] = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-5-mini",
    # ``openai-codex`` has no static default: the set of usable models (and
    # which is the flagship) changes as OpenAI retires/ships models, and a
    # hard-coded slug here is exactly what caused retired models to be sent.
    # ``None`` flows through to the Codex client, which resolves the
    # highest-priority model from the live ``models.json`` catalog at
    # construction. The key is kept so ``resolve_provider`` still recognises
    # the provider.
    "openai-codex": None,
}
"""Default model name per provider when neither ``definition.model`` nor
``CALFKIT_AGENT_DEFAULT_MODEL`` is set. Each provider's model namespace is
disjoint, so a single cross-provider default is meaningless — picking one
per provider is the only sensible fallback. ``openai-codex`` is ``None``: its
default is resolved from the live catalog at client construction, not pinned
here."""

ModelClientFactory = Callable[[Provider, str | None], PydanticModelClient]
"""Construct a model client for ``(provider, model_name)``. The default
factory dispatches on ``provider``; tests override this to inject fakes.
``model_name`` may be ``None`` only for ``openai-codex`` (catalog-resolved
default)."""


def _default_model_client_factory(provider: Provider, model_name: str | None) -> PydanticModelClient:
    """Map ``provider`` to its concrete calfkit model-client class.

    Provider authentication is read from each SDK's standard env var
    (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``); the factory does not
    handle keys. A missing key surfaces on first invocation, not at
    construction.

    ``model_name`` is ``None`` only when the resolved provider is
    ``openai-codex`` and no model was configured (the Codex client resolves a
    catalog default). The other providers always carry a static default from
    :data:`_PROVIDER_DEFAULT_MODELS`, so a ``None`` reaching them is a bug —
    guarded explicitly rather than passed through as an invalid model name.
    """
    if provider == "anthropic":
        return AnthropicModelClient(model_name=_require_model(provider, model_name))
    if provider == "openai":
        return OpenAIModelClient(model_name=_require_model(provider, model_name))
    if provider == "openai-codex":
        # Lazy import: pulls in authlib + OpenHands auth machinery only when
        # an agent actually opts in to ChatGPT subscription billing.
        from calfcord.providers.codex import build_codex_subscription_client

        return build_codex_subscription_client(model_name=model_name)
    # Unreachable when ``provider`` is typed as Provider; defensive for
    # runtime callers that bypass the Literal.
    raise ValueError(f"unknown provider {provider!r}; expected one of {list(_PROVIDER_DEFAULT_MODELS)}")


def _require_model(provider: Provider, model_name: str | None) -> str:
    """Return ``model_name`` or raise — for providers that need an explicit slug.

    Only ``openai-codex`` tolerates ``None`` (it resolves a catalog default);
    every other provider must have a concrete model by this point.
    """
    if model_name is None:
        raise ValueError(
            f"provider {provider!r} requires a model name but none was resolved; "
            f"only 'openai-codex' supports a catalog-resolved default"
        )
    return model_name


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


def _build_peers(definition: AgentDefinition) -> list[Messaging | Handoff]:
    """Build the agent's native peer handles from its ``a2a``/``handoff`` fields.

    Each field is ``bool | tuple[str, ...]`` (both default ``True``):

    * ``True`` -> reach any agent (``discover=True``); calfkit renders the live
      peer directory into the agent's prompt from the native ``AgentCard`` plane.
    * a tuple of names -> restrict to those peers (``Messaging(*names)`` /
      ``Handoff(*names)``).
    * ``False`` -> omit that capability entirely (no ``message_agent`` consult
      tool / no handoff).

    ``Messaging`` injects calfkit's built-in ``message_agent`` consult tool (C4);
    ``Handoff`` enables transferring the caller's turn to a peer (C7).
    """
    peers: list[Messaging | Handoff] = []
    if definition.a2a is not False:
        peers.append(Messaging(discover=True) if definition.a2a is True else Messaging(*definition.a2a))
    if definition.handoff is not False:
        peers.append(Handoff(discover=True) if definition.handoff is True else Handoff(*definition.handoff))
    return peers


class AgentFactory:
    """Builds a :class:`Worker` running one LLM-backed agent.

    Identity carriage:
        The constructed :class:`Agent` is unmodified calfkit; calfkit
        stamps ``x-calf-emitter`` / ``x-calf-emitter-kind`` Kafka headers on
        every outbound publish, so the bridge resolves the responding agent
        via the emitter and looks up its persona.

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
        tool_registry: dict[str, ToolNodeDef] | None = None,
    ) -> None:
        """Construct an agent factory.

        Args:
            persona_sender: Held for future use (e.g. when agents gain
                direct-send capabilities for non-bridge-mediated flows).
                Currently unused — the bridge's own persona sender posts
                replies. Pass a real :class:`DiscordPersonaSender` from the
                runner so this stays available when the "future use" arrives.
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
            tool_registry: Map of tool name → :class:`ToolNodeDef` used to
                resolve the bare builtin names declared in
                ``definition.tools``. Defaults to the module-level
                :data:`TOOL_REGISTRY`; tests pass a fixture-built dict.
        """
        self._persona_sender = persona_sender
        self._calfkit_client = calfkit_client
        self._default_provider = default_provider
        self._default_model = default_model
        self._model_client_factory = model_client_factory or _default_model_client_factory
        if tool_registry is None:
            from calfcord.tools import TOOL_REGISTRY

            tool_registry = TOOL_REGISTRY
        self._tool_registry = tool_registry

    def build(self, definition: AgentDefinition) -> Worker:
        """Build a single-agent :class:`Worker`.

        Thin convenience wrapper around :meth:`build_node` for callers
        that want a complete one-node Worker without assembling it
        themselves. The in-tree ``calfkit-agent`` runner does not use
        this — it calls :meth:`build_node` directly in both single-agent
        and all-agents modes so it can pack one or many nodes into a
        single Worker.

        Raises:
            ValueError: If the resolved provider isn't one of
                :data:`_PROVIDER_DEFAULT_MODELS`, or if ``definition.memory``
                is set but the agent lacks the filesystem tools memory needs
                (see :meth:`_require_memory_tools`).
        """
        return Worker(self._calfkit_client, [self.build_node(definition)])

    def build_node(self, definition: AgentDefinition) -> Agent:
        """Build one name-addressed :class:`Agent` node from ``definition``.

        Use this when assembling several agents into a single :class:`Worker`
        (multi-node co-tenancy). Each node gets its own Kafka consumer group
        keyed on its name, so co-tenant nodes do not contend for partitions.

        The agent declares no channel ``subscribe_topics`` and no addressing
        gate: it is reachable by name on its automatic private input topic
        (provisioned, with its private return topic, by the managed Worker's
        startup pass). ``description=`` is passed so the advertised
        ``AgentCard`` is not blank, and ``peers=`` (from :func:`_build_peers`)
        declares the agent's native A2A/handoff reach.

        Raises:
            ValueError: If the resolved provider isn't one of
                :data:`_PROVIDER_DEFAULT_MODELS` (typically only reachable
                via env-var override with an unknown value), or if
                ``definition.memory`` is set but the agent lacks the
                ``read_file``/``write_file`` tools memory requires (see
                :meth:`_require_memory_tools`).
        """
        tools, mcp_selectors = self._resolve_tools(definition)
        self._require_memory_tools(definition, tools)

        provider = self._resolve_provider(definition)
        model_name = self._resolve_model(definition, provider)
        model_settings = build_model_settings(provider, definition.thinking_effort)
        peers = _build_peers(definition)

        logger.info(
            "building agent=%s provider=%s model=%s thinking_effort=%s tools=%s peers=%s",
            definition.agent_id,
            provider,
            # ``None`` for an unconfigured Codex model — resolved to the live
            # catalog default at client construction (logged there).
            model_name if model_name is not None else "<codex catalog default>",
            definition.thinking_effort,
            [t.tool_schema.name for t in tools] + [f"mcp:{s.name}" for s in mcp_selectors],
            [type(p).__name__ for p in peers],
        )

        agent = Agent(
            name=definition.agent_id,
            system_prompt=definition.system_prompt,
            description=definition.description,
            model_client=self._model_client_factory(provider, model_name),
            model_settings=model_settings,
            tools=[*tools, *mcp_selectors] or None,
            peers=peers or None,
        )

        # Memory-enabled agents carry a dynamic-instructions hook. It reads
        # the bridge-injected template from ``deps`` at runtime, localizes it
        # to this agent's ``memory/<agent_id>/`` dir, and appends it to the
        # instructions. The agent process never reads the prompt file — only
        # the bridge does (see ``agents/memory.py``). ``_require_memory_tools``
        # above already guaranteed the agent has the fs tools the block tells
        # it to use.
        if definition.memory:
            agent.instructions(memory_instructions(definition.agent_id))

        return agent

    def _resolve_provider(self, definition: AgentDefinition) -> Provider:
        return resolve_provider(definition, default_provider=self._default_provider)

    def _resolve_model(self, definition: AgentDefinition, provider: Provider) -> str | None:
        """Resolve the model name, or ``None`` for catalog-defaulted providers.

        Precedence: ``definition.model`` → ``CALFKIT_AGENT_DEFAULT_MODEL`` →
        ``self._default_model`` → the provider's static default. Returns
        ``None`` only for ``openai-codex`` when none of the above is set —
        its static default is ``None`` and the Codex client resolves a live
        catalog default instead.
        """
        return (
            definition.model
            or os.getenv(_DEFAULT_MODEL_ENV_VAR)
            or self._default_model
            or _PROVIDER_DEFAULT_MODELS[provider]
        )

    def _resolve_tools(self, definition: AgentDefinition) -> tuple[list[ToolNodeDef], list[MCPToolbox]]:
        """Resolve ``definition.tools`` into builtin nodes + deferred MCP selectors.

        The flat ``tools:`` list mixes two kinds of entry, partitioned here:

        * bare *builtin* names (``terminal``, ``calendar``) resolve against the
          in-memory tool registry, with an aggregate unknown-name
          :class:`ValueError` (every unknown name in one message);
        * ``mcp/...`` selectors collapse into one
          :class:`~calfkit.mcp.MCPToolbox` per server
          (:func:`~calfcord.mcp.agent_select.selectors_from_entries`),
          resolved per turn against the capability view — never against any
          local registry, so there is nothing further to validate here.

        Name collisions between the two kinds cannot be checked statically
        (MCP tool names are only known at runtime); calfkit's per-turn
        resolution drops a toolbox tool that collides with a static binding
        (static wins, logged), which is the operative policy.

        Semantics (mirrors :attr:`AgentDefinition.tools`):
            - ``None``: tools-by-default — every registered builtin tool and
              **no** MCP tools (MCP is always an explicit grant). This is
              the in-memory representation of the "no ``tools:`` line in
              frontmatter" case before the loader normalizes it.
            - empty tuple ``()``: zero tools (explicit opt-out).
            - non-empty tuple: exactly those entries.

        Raises:
            ValueError: if any builtin name is missing from the registry
                (lists every unknown name in one message).
        """
        if definition.tools is None:
            return list(self._tool_registry.values()), []
        if not definition.tools:
            return [], []

        mcp_entries = [e for e in definition.tools if is_mcp_selector(e)]
        nodes: list[ToolNodeDef] = []
        unknown: list[str] = []
        for name in definition.tools:
            if is_mcp_selector(name):
                continue
            node = self._tool_registry.get(name)
            if node is None:
                unknown.append(name)
            else:
                nodes.append(node)
        if unknown:
            known = sorted(self._tool_registry)
            raise ValueError(
                f"agent {definition.agent_id!r} declares unknown tool(s) "
                f"{unknown!r}; known tools: {known or '<none registered>'}"
            )

        return nodes, selectors_from_entries(mcp_entries)

    def _require_memory_tools(self, definition: AgentDefinition, tools: list[ToolNodeDef]) -> None:
        """Reject a ``memory: true`` agent that lacks the filesystem tools memory needs.

        A memory-enabled agent manages its notepad with the general-purpose
        ``read_file`` / ``write_file`` tools; without them the injected memory
        instructions are a silent no-op, so fail loud at build time instead.
        Agents that omit ``tools:`` get every registered tool and pass
        automatically — only an explicitly-restricted ``tools:`` list can trip
        this. Operates on the resolved :class:`~calfkit.nodes.tool.ToolNodeDef`
        list (not the raw names) so the "all tools" expansion is reflected
        correctly.
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
