"""Unit tests for ``calfkit-router`` runner helpers.

Covers the ``_build_router_nodes`` wiring (which assembles the router
agent + fan-out consumer onto the list a :class:`Worker` will host)
and the ``_run_worker`` shutdown contract. The full ``_amain``
requires a Kafka broker and an LLM client — too heavy for a unit
test. The wiring contract is what we pin here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from calfkit.nodes import Agent
from calfkit.providers.pydantic_ai.model_client import PydanticModelClient
from calfkit.worker import Worker

from calfkit_organization.agents.factory import AgentFactory
from calfkit_organization.router import runner
from calfkit_organization.router.definition import ROUTER_AGENT_ID


class TestBuildRouterNodes:
    """``_build_router_nodes`` constructs the two nodes that boot on a
    single Worker: the router agent and the fan-out consumer."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "CALFKIT_ROUTER_PROVIDER",
            "CALFKIT_ROUTER_MODEL",
            "CALFKIT_ROUTER_THINKING_EFFORT",
        ):
            monkeypatch.delenv(var, raising=False)

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


class TestRunWorkerShutdownContract:
    """The supervisor-restart invariant mirrors agents/runner.py and
    tools/runner.py: any non-signal exit raises out of ``_run_worker``
    so the process exits non-zero."""

    async def test_worker_crash_propagates(self) -> None:
        crash = ValueError("simulated kafka drop")
        worker = MagicMock(spec=Worker)
        worker.run = AsyncMock(side_effect=crash)
        with pytest.raises(ValueError, match="simulated kafka drop"):
            await runner._run_worker(worker)

    async def test_worker_unexpected_clean_return_raises(self) -> None:
        """A clean ``worker.run()`` return without a shutdown signal is
        unexpected — synthesize a RuntimeError so supervisors restart."""
        worker = MagicMock(spec=Worker)

        async def returns_immediately() -> None:
            return None

        worker.run = AsyncMock(side_effect=returns_immediately)
        with pytest.raises(RuntimeError, match="returned unexpectedly"):
            await runner._run_worker(worker)
