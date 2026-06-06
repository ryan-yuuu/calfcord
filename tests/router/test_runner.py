"""Unit tests for ``calfkit-router`` runner helpers.

Covers the ``_build_router_nodes`` wiring (which assembles the router
agent + fan-out consumer onto the list a :class:`Worker` will host)
and the ``_amain`` boot contract (``provision_infra`` then the managed
``worker.run()``). The full ``_amain`` still requires a Kafka broker
and an LLM client end-to-end — too heavy for a unit test — so the
wiring + provision-before-run ordering is what we pin here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.nodes import Agent
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfcord.agents.factory import AgentFactory
from calfcord.router import runner
from calfcord.router.definition import ROUTER_AGENT_ID


class TestBuildRouterNodes:
    """``_build_router_nodes`` constructs the two nodes that boot on a
    single Worker: the router agent and the fan-out consumer."""

    def _factory(self) -> AgentFactory:
        def fake_model_factory(provider: str, model_name: str) -> PydanticModelClient:
            return MagicMock(spec=PydanticModelClient)

        # ``persona_sender=None`` mirrors the production runner — the
        # router build path doesn't use it.
        return AgentFactory(
            persona_sender=None,
            calfkit_client=MagicMock(),
            model_client_factory=fake_model_factory,  # type: ignore[arg-type]
        )

    def test_returns_exactly_two_nodes(self) -> None:
        nodes = runner._build_router_nodes(self._factory(), MagicMock())
        assert len(nodes) == 2

    def test_first_node_is_router_agent(self) -> None:
        nodes = runner._build_router_nodes(self._factory(), MagicMock())
        assert isinstance(nodes[0], Agent)
        assert nodes[0].node_id == ROUTER_AGENT_ID

    def test_router_subscribes_to_ambient_topic(self) -> None:
        nodes = runner._build_router_nodes(self._factory(), MagicMock())
        assert nodes[0].subscribe_topics == ["discord.ambient.in"]

    def test_router_publishes_to_routing_decisions(self) -> None:
        nodes = runner._build_router_nodes(self._factory(), MagicMock())
        assert nodes[0].publish_topic == "routing.decisions"

    def test_second_node_is_fanout_consumer(self) -> None:
        nodes = runner._build_router_nodes(self._factory(), MagicMock())
        # The fan-out consumer subscribes to the router's publish_topic.
        # Stock ConsumerNodeDef stores topics on ``subscribe_topics`` as
        # a list.
        subscribe_topics = nodes[1].subscribe_topics
        if not isinstance(subscribe_topics, list):
            subscribe_topics = [subscribe_topics]
        assert "routing.decisions" in subscribe_topics


class TestAmainBootWiring:
    """The 0.5.4 boot contract: ``_amain`` provisions calfcord's blind-spot
    topics (``provision_infra`` — the #180 reply topic plus the router's
    ambient-discard topic) BEFORE handing the lifecycle to the managed
    ``worker.run()``. The Worker now owns signals + broker start/stop, so the
    runner no longer hand-rolls a shutdown loop; what must stay pinned is the
    provision-before-run ordering (a stray run before provisioning would hang
    the reply dispatcher on a no-auto-create broker) and that a runtime crash
    escapes ``_amain`` (the supervisor-restart guarantee, now native to
    ``Worker.run()``)."""

    def _patch_common(self, monkeypatch: pytest.MonkeyPatch, *, client: object) -> None:
        """Stub the heavy boot collaborators shared by every wiring test.

        Leaves ``provision_infra`` and ``Worker`` for each test to set, since
        those are the seam under test."""

        @asynccontextmanager
        async def _fake_connect(*_a, **_k):
            yield client

        monkeypatch.setattr(runner.Client, "connect", lambda *a, **k: _fake_connect(*a, **k))
        monkeypatch.setattr(runner, "build_router_definition", lambda: MagicMock())
        monkeypatch.setattr(runner, "_prewarm_codex_if_needed", AsyncMock())
        monkeypatch.setattr(runner, "AgentFactory", lambda **_k: MagicMock())
        monkeypatch.setattr(runner, "_build_router_nodes", lambda *_a, **_k: [MagicMock()])

    async def test_provision_infra_runs_before_worker_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        client = MagicMock()
        self._patch_common(monkeypatch, client=client)

        worker = MagicMock()

        async def _run() -> None:
            order.append("run")

        worker.run = _run

        async def _provision(_client, **_k) -> None:
            order.append("provision")

        monkeypatch.setattr(runner, "provision_infra", _provision)
        monkeypatch.setattr(runner, "Worker", lambda *_a, **_k: worker)

        await runner._amain()

        assert order == ["provision", "run"]

    async def test_provision_infra_receives_router_infra_topics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The router's non-node topics (the ambient-discard callback target)
        must be passed through to ``provision_infra`` as ``extra_topics`` so
        they exist before the broker starts."""
        from calfcord._provisioning import router_infra_topics

        client = MagicMock()
        self._patch_common(monkeypatch, client=client)

        worker = MagicMock()
        worker.run = AsyncMock()
        provision = AsyncMock()
        monkeypatch.setattr(runner, "provision_infra", provision)
        monkeypatch.setattr(runner, "Worker", lambda *_a, **_k: worker)

        await runner._amain()

        provision.assert_awaited_once_with(client, extra_topics=router_infra_topics())

    async def test_worker_run_crash_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A crash inside the managed ``worker.run()`` must escape ``_amain`` so
        the surrounding ``asyncio.run`` exits non-zero and the supervisor
        restarts the process — the lifecycle guarantee is now native to
        ``Worker.run()``, but the runner must not swallow it."""
        client = MagicMock()
        self._patch_common(monkeypatch, client=client)

        worker = MagicMock()
        worker.run = AsyncMock(side_effect=ValueError("simulated kafka drop"))
        monkeypatch.setattr(runner, "provision_infra", AsyncMock())
        monkeypatch.setattr(runner, "Worker", lambda *_a, **_k: worker)

        with pytest.raises(ValueError, match="simulated kafka drop"):
            await runner._amain()


class TestPrewarmCodexIfNeeded:
    """Router-side equivalent of the agents-runner prewarm bridge. Must invoke
    prewarm when the router definition resolves to openai-codex (including via
    the env-var default-provider path), skip otherwise, and convert upstream
    failures into BootstrapError."""

    def _definition(self, provider: str | None):
        from calfcord.agents.definition import AgentDefinition

        definition = MagicMock(spec=AgentDefinition)
        definition.provider = provider
        definition.agent_id = "router"
        return definition

    @pytest.mark.asyncio
    async def test_skips_prewarm_when_router_not_openai_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        prewarm = AsyncMock()
        import calfcord.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", prewarm)
        await runner._prewarm_codex_if_needed(self._definition("anthropic"))
        prewarm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invokes_prewarm_when_router_uses_openai_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        prewarm = AsyncMock()
        import calfcord.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", prewarm)
        await runner._prewarm_codex_if_needed(self._definition("openai-codex"))
        prewarm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invokes_prewarm_when_env_var_default_is_openai_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Future-proofs against a router-definition refactor that lets
        ``provider`` be None — the env-var fallback must still trigger prewarm."""
        monkeypatch.setenv("CALFKIT_AGENT_DEFAULT_PROVIDER", "openai-codex")
        prewarm = AsyncMock()
        import calfcord.providers.codex as codex_pkg

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", prewarm)
        await runner._prewarm_codex_if_needed(self._definition(None))
        prewarm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_converts_codex_prompts_unavailable_to_bootstrap_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CALFKIT_AGENT_DEFAULT_PROVIDER", raising=False)
        import calfcord.providers.codex as codex_pkg
        from calfcord.providers.codex import CodexPromptsUnavailableError

        async def _failing_prewarm() -> None:
            raise CodexPromptsUnavailableError("simulated network failure")

        monkeypatch.setattr(codex_pkg, "prewarm_codex_prompts", _failing_prewarm)
        with pytest.raises(runner.BootstrapError, match="refresh-prompts"):
            await runner._prewarm_codex_if_needed(self._definition("openai-codex"))
