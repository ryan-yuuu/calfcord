"""Unit tests for :meth:`BridgeIngress.handle`'s kind-branch (Phase 4).

The bridge now branches on ``wire.kind``:

* ``kind="slash"`` continues to publish to ``discord.channel.{cid}.in``
  via ``client.invoke_node`` (existing behavior — covered by
  ``test_ingress.py``).
* ``kind="message"`` (ambient) publishes to ``discord.ambient.in`` via
  :func:`invoke_node_with_metadata` with the original wire packed into
  ``state.metadata`` and the discard reply_topic.

These tests pin the new ambient publish shape: topic, reply_topic,
metadata contents, deps contents, and per-call temp_instructions
(router roster).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from calfkit_organization.agents.definition import AgentDefinition
from calfkit_organization.bridge.ingress import (
    _AMBIENT_INGRESS_TOPIC,
    _AMBIENT_REPLY_DISCARD_TOPIC,
    BridgeIngress,
)
from calfkit_organization.bridge.pending_wires import PendingWires
from calfkit_organization.bridge.registry import AgentRegistry
from calfkit_organization.bridge.wire import WireAuthor, WireMessage
from calfkit_organization.router.definition import build_router_definition


def _wire(
    *,
    event_id: str = "evt-ambient",
    slash_target: str | None = None,
    kind: str = "message",
    content: str = "what's the weather like?",
    is_bot: bool = False,
    is_webhook: bool = False,
    author_agent_id: str | None = None,
    author_display_name: str = "alice",
) -> WireMessage:
    return WireMessage(
        event_id=event_id,
        kind=kind,  # type: ignore[arg-type]
        slash_target=slash_target,
        message_id=12345,
        channel_id=6789,
        guild_id=4242,
        content=content,
        author=WireAuthor(
            discord_user_id=111,
            display_name=author_display_name,
            is_bot=is_bot,
            is_webhook=is_webhook,
            agent_id=author_agent_id,
            avatar_url="https://cdn.discordapp.com/avatars/111/abc.png",
        ),
        created_at=datetime.now(UTC),
    )


def _registry() -> AgentRegistry:
    """Registry including the router and two assistants — production
    shape post-Phase 3."""
    return AgentRegistry(
        [
            AgentDefinition(
                agent_id="scheduler",
                slash="/scheduler",
                display_name="Aksel (Scheduler)",
                description="Calendar mechanics.",
                avatar_url=None,
                provider="anthropic",
                system_prompt="x",
            ),
            AgentDefinition(
                agent_id="scribe",
                slash="/scribe",
                display_name="Scribe",
                description="Note-taking.",
                avatar_url=None,
                provider="openai",
                system_prompt="x",
            ),
            build_router_definition(),
        ]
    )


def _fresh_handle() -> MagicMock:
    handle = MagicMock()
    handle._future = asyncio.get_event_loop().create_future()
    return handle


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    c.invoke_node = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c._invoke = AsyncMock(side_effect=lambda *_a, **_kw: _fresh_handle())
    c.reply_topic = "discord.outbox"
    return c


@pytest.fixture
def pending_wires() -> PendingWires:
    return PendingWires()


class TestSlashUnchanged:
    """Slash wires continue to use the existing channel-topic path."""

    async def test_slash_goes_to_channel_topic(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(kind="slash", slash_target="scribe"))
        # Slash uses invoke_node; ambient helper not called.
        client.invoke_node.assert_awaited_once()
        client._invoke.assert_not_called()
        assert (
            client.invoke_node.await_args.kwargs["topic"] == "discord.channel.6789.in"
        )

    async def test_slash_writes_to_pending_wires(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(kind="slash", slash_target="scribe")
        await ingress.handle(wire)
        assert pending_wires.get(wire.event_id) is wire


class TestAmbientPublish:
    """Ambient wires go through the router via ``invoke_node_with_metadata``."""

    async def test_ambient_skips_slash_invoke_node(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        client.invoke_node.assert_not_called()

    async def test_ambient_publishes_to_ambient_ingress_topic(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        kwargs = client._invoke.await_args.kwargs
        assert kwargs["topic"] == _AMBIENT_INGRESS_TOPIC == "discord.ambient.in"

    async def test_ambient_uses_discard_reply_topic(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """The router's reply lands on the caller's reply_topic; we
        route it to a no-subscriber topic so it doesn't echo to the
        bridge's outbox consumer or anywhere visible."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        kwargs = client._invoke.await_args.kwargs
        assert (
            kwargs["reply_topic"]
            == _AMBIENT_REPLY_DISCARD_TOPIC
            == "_calf.ambient.callback-discard"
        )

    async def test_ambient_correlation_id_is_fresh_uuid7(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """The ambient publish uses a FRESH ``correlation_id`` rather
        than reusing ``wire.event_id``. The router's reply lands on
        the discard topic and is never looked up by event_id, and
        the fan-out mints its own ids for synthesized wires —
        nothing downstream correlates to the original event_id.
        Decoupling the ambient ``correlation_id`` from
        ``wire.event_id`` removes a collision risk against the
        reply dispatcher's pending-future map on Discord
        redeliveries (gateway reconnects).

        Asserts the decoupling invariant only — not the specific
        id format. A future switch to a different collision-free
        generator should not require updating this test."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(event_id="evt-abc"))
        cid = client._invoke.await_args.kwargs["correlation_id"]
        # Must be a non-empty string and must NOT equal the wire's
        # event_id (which is what the old behavior would have set).
        assert isinstance(cid, str)
        assert cid
        assert cid != "evt-abc"

    async def test_ambient_user_prompt_is_wire_content(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """The router's LLM sees the user's message text on
        invocation — the same way an assistant agent sees a slash
        invocation's content. The wire field, not the metadata, is
        what calfkit feeds into the model loop."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire(content="who's free at 3pm?"))
        # The helper stages the user_prompt as a ModelRequest on
        # state.message_history; the staged message should carry the
        # wire's content verbatim. Without this assertion, a
        # regression that stages an empty string (or the wrong field)
        # would pass the "something staged" check above.
        state = client._invoke.await_args.kwargs["state"]
        assert state.uncommitted_message is not None
        # ModelRequest text content extraction: parts is a list of
        # UserPromptPart / SystemPromptPart; the first part should be
        # the user prompt with our text.
        request = state.uncommitted_message
        text_blobs = [
            getattr(p, "content", "")
            for p in getattr(request, "parts", [])
        ]
        assert any("who's free at 3pm?" in str(t) for t in text_blobs), (
            f"staged ModelRequest does not contain wire content; "
            f"parts={text_blobs!r}"
        )

    async def test_ambient_metadata_carries_wire(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """The original wire is packed into ``state.metadata["wire"]``
        so the router's fan-out consumer can recover it (consumers
        don't see deps, only state.metadata)."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(event_id="evt-meta-1")
        await ingress.handle(wire)
        state = client._invoke.await_args.kwargs["state"]
        assert state.metadata["wire"]["event_id"] == "evt-meta-1"
        assert state.metadata["wire"]["channel_id"] == 6789

    async def test_ambient_metadata_carries_phonebook(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        state = client._invoke.await_args.kwargs["state"]
        phonebook = state.metadata["phonebook"]
        ids = sorted(e["agent_id"] for e in phonebook)
        # Both assistants are in the phonebook; the router is
        # filtered out by ``phonebook_from_registry`` (Issue 3).
        assert "scribe" in ids
        assert "scheduler" in ids
        assert "_router" not in ids

    async def test_ambient_deps_carries_wire(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """The wire ALSO rides in deps for the synthesized-assistant
        chain that follows. ingress.handle is reused for each
        synthesized wire, and the slash branch reads from deps to
        produce the assistant's invocation deps."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(event_id="evt-deps-1")
        await ingress.handle(wire)
        deps = client._invoke.await_args.kwargs["deps"]
        assert deps["discord"]["event_id"] == "evt-deps-1"
        assert deps["discord"]["channel_id"] == 6789

    async def test_ambient_deps_carries_phonebook(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        deps = client._invoke.await_args.kwargs["deps"]
        ids = sorted(e["agent_id"] for e in deps["phonebook"])
        # Router is filtered out by phonebook_from_registry (Issue 3).
        assert "scribe" in ids
        assert "_router" not in ids

    async def test_ambient_injects_router_temp_instructions(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """The router's LLM sees the agent roster as temp_instructions
        on every invocation. The router roster (different from the
        per-agent peer roster) lists ALL non-router agents regardless
        of tool presence."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        state = client._invoke.await_args.kwargs["state"]
        instructions = state.temp_instructions
        assert instructions is not None
        # Roster includes both assistants but NOT the router itself.
        assert "scheduler" in instructions
        assert "scribe" in instructions
        assert "_router" not in instructions

    async def test_ambient_does_not_populate_pending_wires(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """Only the slash branch writes to PendingWires. The ambient
        branch deliberately skips the ``put`` because the router's
        reply goes to the discard topic (the original event_id is
        never looked up) and the synthesized fan-out wires each get
        their own fresh event_id. Populating here would waste LRU
        slots and could evict legitimate slash entries under load."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(event_id="evt-pending-1")
        await ingress.handle(wire)
        assert pending_wires.get(wire.event_id) is None

    async def test_ambient_cancels_dispatcher_future(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """Same cancel-after-publish idiom as the slash path: the
        pending future would otherwise leak."""
        captured: dict[str, Any] = {}

        async def _invoke(*_a: Any, **_kw: Any) -> Any:
            handle = MagicMock()
            handle._future = asyncio.get_event_loop().create_future()
            captured["handle"] = handle
            return handle

        client._invoke.side_effect = _invoke
        ingress = BridgeIngress(client, _registry(), pending_wires)
        await ingress.handle(_wire())
        assert captured["handle"]._future.cancelled()

    async def test_ambient_publish_logs_info(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Pin the operator-side health signal contract from
        ``docs/ambient-routing.md``: every ambient publish logs INFO
        ``ingress ambient publish event_id=... channel=... topic=...``.

        Operators correlate this with the synthesized-arrival INFO log
        in :mod:`bridge.synthesized` (asserted in
        ``test_synthesized_consumer.py::test_logs_arrival_at_info``) to
        detect a silent router: a growing gap between the two streams
        signals the router process is down or wedged. Without this
        publish-side assertion, a refactor that downgrades the log to
        DEBUG would silently break the operator runbook documented at
        ``docs/ambient-routing.md`` lines 224-234.
        """
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(event_id="evt-publish-log")
        with caplog.at_level(
            logging.INFO, logger="calfkit_organization.bridge.ingress"
        ):
            await ingress.handle(wire)
        info_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and r.name == "calfkit_organization.bridge.ingress"
        ]
        publish_records = [
            r for r in info_records if "ingress ambient publish" in r.message
        ]
        assert publish_records, (
            "expected an INFO 'ingress ambient publish' log line; got "
            f"{[r.message for r in info_records]}"
        )
        # The event_id is part of the documented correlation key — the
        # operator runbook reads ``event_id=...`` to pair publishes with
        # synthesized arrivals.
        assert any("evt-publish-log" in r.message for r in publish_records)


class TestAmbientNonHumanFilter:
    """Issue 1: peer-agent webhook chatter without an @-mention must not
    trigger the router. If allowed, the router LLM could pick a
    respondent based on a peer's persona-text and unleash a reply
    storm. The filter is on ``is_bot`` or ``is_webhook`` — both
    identify non-human authors.

    Agent @-mentions still route normally because the normalizer
    classifies on content (turning ``@scribe foo`` into
    ``kind="slash"``), entering the slash branch *before* this filter.
    """

    async def test_ambient_from_webhook_author_dropped(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(
            event_id="evt-webhook",
            is_webhook=True,
            author_agent_id="scribe",
            author_display_name="Scribe",
        )
        await ingress.handle(wire)
        client._invoke.assert_not_called()
        client.invoke_node.assert_not_called()
        assert pending_wires.get(wire.event_id) is None

    async def test_ambient_from_bot_author_dropped(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(
            event_id="evt-bot",
            is_bot=True,
            author_display_name="some-bot",
        )
        await ingress.handle(wire)
        client._invoke.assert_not_called()
        client.invoke_node.assert_not_called()
        assert pending_wires.get(wire.event_id) is None

    async def test_ambient_from_human_still_publishes(
        self, client: MagicMock, pending_wires: PendingWires
    ) -> None:
        """Regression positive: the filter is on author identity, not
        kind. A human ambient still reaches the router."""
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(event_id="evt-human")
        await ingress.handle(wire)
        client._invoke.assert_awaited_once()


class TestAmbientFailureHandling:
    async def test_ambient_publish_failure_propagates(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A broker-side failure during the ambient publish propagates
        out of ``handle()`` AND fires ``logger.exception`` before the
        raise so operators see the stack trace at the failing call
        site. PendingWires is unaffected because the ambient branch
        never inserted (see
        ``test_ambient_does_not_populate_pending_wires``)."""
        client._invoke.side_effect = RuntimeError("kafka down")
        ingress = BridgeIngress(client, _registry(), pending_wires)
        wire = _wire(event_id="evt-fail-1")
        with (
            caplog.at_level(
                logging.ERROR, logger="calfkit_organization.bridge.ingress"
            ),
            pytest.raises(RuntimeError),
        ):
            await ingress.handle(wire)
        assert pending_wires.get(wire.event_id) is None
        # ``logger.exception`` log fires with the event_id + channel.
        # Without this assertion, a refactor that drops the
        # ``logger.exception`` (or downgrades it to DEBUG) silently
        # breaks operator observability of ambient failures.
        assert any(
            "ingress ambient publish failed" in r.message
            and "evt-fail-1" in r.message
            for r in caplog.records
            if r.levelno >= logging.ERROR
        )


class TestAmbientEmptyRosterAbort:
    """When the phonebook has no eligible respondents (no assistants
    registered, only the router), the ambient publish aborts and
    ``handle`` raises :class:`AmbientRosterEmptyError`. The gateway
    catches this specific type and surfaces the misconfiguration to
    the user via an inline reply (tested separately in the gateway
    tests). The router-side WARN names the registry shape; the
    bridge-side ERROR identifies the specific ambient message that
    won't get a response."""

    async def test_raises_ambient_roster_empty_error(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        from calfkit_organization.bridge.ingress import (  # noqa: PLC0415
            AmbientRosterEmptyError,
        )

        router_only_registry = AgentRegistry([build_router_definition()])
        ingress = BridgeIngress(client, router_only_registry, pending_wires)
        wire = _wire(event_id="evt-no-roster")
        with pytest.raises(AmbientRosterEmptyError) as excinfo:
            await ingress.handle(wire)
        # The carried context lets the gateway log + reply without
        # re-deriving the identifiers.
        assert excinfo.value.event_id == "evt-no-roster"
        assert excinfo.value.channel_id == 6789

    async def test_no_publish_when_roster_empty(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
    ) -> None:
        """The whole point of the abort: zero LLM tokens burned on a
        router run with no roster, zero envelope on the ambient
        topic."""
        from calfkit_organization.bridge.ingress import (  # noqa: PLC0415
            AmbientRosterEmptyError,
        )

        router_only_registry = AgentRegistry([build_router_definition()])
        ingress = BridgeIngress(client, router_only_registry, pending_wires)
        with pytest.raises(AmbientRosterEmptyError):
            await ingress.handle(_wire())
        client._invoke.assert_not_called()
        client.invoke_node.assert_not_called()

    async def test_logs_error_with_event_id_and_channel(
        self,
        client: MagicMock,
        pending_wires: PendingWires,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """ERROR log identifies the affected ambient message — operator
        can correlate the registry-shape WARN to a user complaint."""
        from calfkit_organization.bridge.ingress import (  # noqa: PLC0415
            AmbientRosterEmptyError,
        )

        router_only_registry = AgentRegistry([build_router_definition()])
        ingress = BridgeIngress(client, router_only_registry, pending_wires)
        with caplog.at_level(
            logging.ERROR, logger="calfkit_organization.bridge.ingress"
        ), pytest.raises(AmbientRosterEmptyError):
            await ingress.handle(_wire(event_id="evt-abort-1"))
        assert any(
            "ambient publish aborted" in r.message
            and "evt-abort-1" in r.message
            and r.levelno >= logging.ERROR
            for r in caplog.records
        )
