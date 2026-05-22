"""Invoke a calfkit node with ``State.metadata`` populated.

:class:`~calfkit.client.Client`'s public :meth:`invoke_node` constructs
a fresh :class:`~calfkit.models.State` internally and does not expose a
``metadata`` parameter, so application-level data on the State (i.e.,
``state.metadata``) can't be set without bypassing the public API. This
matters for us because :class:`~calfkit.nodes.ConsumerNodeDef`'s
``consume_fn`` receives only :class:`NodeResult` — it never sees
``ctx.deps`` — and we need to carry the original Discord wire (and
phonebook) from the bridge ingress through the router agent to the
fan-out consumer, and from the fan-out consumer to the bridge's
synthesized-in consumer.

Verified that calfkit propagates :attr:`State.metadata` through
every publish path — parallel-fanout, ``Call``, ``ReturnCall``, and
``TailCall`` all carry the source ``state`` on the constructed
publish envelope. Calfkit's own ``Agent.run`` mutates only
``state.message_history`` and ``state.final_output_parts``; our
``state.metadata`` is left untouched.

A small caveat on the "no calfkit mutation" claim: pydantic-ai's
:mod:`pydantic_ai.agent` constructs a separate ``GraphAgentState``
internally (different object), and that object's metadata IS touched
by pydantic-ai's run loop. That has no effect on our publish channel
because the calfkit ``State`` we set is not the pydantic-ai
``GraphAgentState`` — they're separate pydantic models with the same
field name. The calfkit ``State.metadata`` we pack rides through the
publish chain untouched.

FIXME (tracked upstream: https://github.com/calf-ai/calfkit-sdk/issues/144) —
Remove this helper when upstream calfkit ships one of:

    1. ``deps`` field on :class:`~calfkit.client.NodeResult` (preferred):
       eliminates the metadata channel entirely; consumers read the
       original wire from ``result.deps.provided_deps["discord"]``,
       which is where the bridge already puts it. ~15-line SDK change.
    2. ``metadata=`` parameter on :meth:`Client.invoke_node`: keeps
       the metadata channel but removes the need to dip into
       :meth:`Client._invoke` directly. Half-step compromise.

**Private surfaces this helper depends on.** The shim reaches into
several underscore-prefixed APIs (``Client._invoke``, a sentinel
default, vendored pydantic-ai types, and a couple of internal
``State``/``Node`` models). All of these must remain stable for as
long as this file exists; the upstream cleanup PR(s) should pin or
expose them too so the migration here is a true revert and not
"swap one private dependency for another". The exact surface set is
visible at the import block below — any addition here is a new
private dependency the upstream cleanup must cover.

When either upstream option above lands, the cleanup is:

    - Swap ``invoke_node_with_metadata(...)`` callsites back to
      ``client.invoke_node(...)`` (or, if option 1 lands, drop
      ``metadata`` entirely and read from ``result.deps`` instead).
    - Drop the ``METADATA_KEY_*`` constants and their contract
      tests in :mod:`tests.router.test_fanout`.
    - Delete this ``_compat`` package.

Two callers in this project use the helper today:

    - ``bridge/ingress.py`` (ambient invocations)
    - ``router/fanout.py`` (synthesized-wire publications)
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NoReturn, Self

import uuid_utils
from calfkit._vendor.pydantic_ai.messages import ModelMessage, ModelRequest
from calfkit._vendor.pydantic_ai.settings import ModelSettings
from calfkit.client import Client, InvocationHandle
from calfkit.client.deserialize import _UNSET
from calfkit.models import State
from calfkit.models.node_schema import BaseToolNodeSchema
from calfkit.models.state import OverridesState
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# ``WireMessage`` and ``PhonebookEntry`` are typed fields on
# :class:`MetadataEnvelope`. Importing them at the TOP of this file
# would close a package-init cycle: ``bridge/__init__.py`` eagerly
# imports ``bridge.ingress`` which imports this module — so a
# top-of-file ``from calfkit_organization.bridge.wire import ...``
# triggers ``bridge/__init__.py``, which re-enters this module
# mid-load and fails to find ``MetadataEnvelope`` (still under
# construction).
#
# Workaround: keep the imports at the BOTTOM of the file (below the
# class definition) and rebuild the model's forward references then.
# With ``from __future__ import annotations`` the field types are
# strings at class-construction time, so pydantic doesn't need them
# resolved until the first ``model_validate`` call — by which point
# the bottom-of-module rebuild has run.
if TYPE_CHECKING:
    from calfkit_organization.agents.phonebook import PhonebookEntry
    from calfkit_organization.bridge.wire import WireMessage

METADATA_KEY_WIRE = "wire"
"""Legacy constant — kept for cross-package contract tests in
:mod:`tests.router.test_fanout` and the integration e2e test in
:mod:`tests.integration.test_ambient_routing_e2e`. Producers and
consumers in this project should use :class:`MetadataEnvelope` (and
its typed ``wire`` field) directly — the typed envelope centralizes
the key-and-shape contract and removes the need for per-consumer
isinstance/None guards on ``state.metadata``. Do not import this
constant from new consumer code."""

METADATA_KEY_PHONEBOOK = "phonebook"
"""Legacy constant — see :data:`METADATA_KEY_WIRE`.

Key under which the phonebook (a list[dict] via
:func:`~calfkit_organization.agents.phonebook.phonebook_to_deps`) is
packed into ``state.metadata``. Mirrors :data:`METADATA_KEY_WIRE`."""


class MetadataEnvelope(BaseModel):
    """Typed envelope for the ``state.metadata`` channel.

    The state-metadata channel carries two pieces of cross-process
    data: the original (or synthesized) Discord :class:`WireMessage`
    and, optionally, the phonebook snapshot the bridge took at
    publish time. Both fields are typed against their domain models
    so pydantic validates on construction and ``extract`` returns
    already-typed instances — consumers don't need to repeat
    :meth:`WireMessage.model_validate` themselves.

    The envelope deliberately omits ``extra="forbid"`` to avoid
    rolling-deploy hazards: a newer producer that adds a field would
    otherwise be rejected by every older consumer still running. The
    move to typed fields makes "extra dict keys" a non-issue at the
    envelope level anyway — :class:`WireMessage` and
    :class:`PhonebookEntry` own their own ``extra`` policies.

    Use :meth:`extract` to decode a ``result.state.metadata`` value
    in a consumer. It returns a fully-validated envelope or raises
    :class:`ValueError` — consumers can pair it with
    :func:`_raise_infra` to surface contract violations.
    """

    model_config = ConfigDict(frozen=True)

    wire: WireMessage
    """The original (ambient) or synthesized (fan-out) Discord
    :class:`WireMessage`. Pydantic validates this on envelope
    construction — consumers receive an already-typed instance and
    do not need to call :meth:`WireMessage.model_validate`
    themselves."""

    phonebook: tuple[PhonebookEntry, ...] | None = None
    """Snapshot of the bridge's :class:`AgentRegistry` projection at
    publish time, as a tuple of typed
    :class:`~calfkit_organization.agents.phonebook.PhonebookEntry`
    instances. ``tuple`` (not ``list``) because ``frozen=True`` only
    freezes attribute reassignment, not in-place mutation of a
    mutable container — using a tuple makes the envelope deeply
    immutable, matching :data:`RoutingDecision.agents` and
    :data:`AgentDefinition.tools`.

    **Why this lives on the envelope (not just in deps).** Calfkit's
    ``NodeResult`` does NOT expose ``Envelope.context.deps`` to
    ``@consumer`` consume_fns, so any consumer that needs the
    phonebook must read it from ``state.metadata``. The
    :func:`~calfkit_organization.router.fanout.build_fanout_consumer`
    consumer reads this field to validate that every ``agent_id`` in
    a :class:`RoutingDecision` is a known assistant before
    synthesizing a slash wire — catching LLM hallucinations and
    registry drift at the source rather than producing an orphaned
    publish that no assistant accepts.

    ``None`` carries two distinct semantics depending on the path:

    * Ambient → router → fan-out: ``None`` is an infra bug. Production
      producers ALWAYS pack the phonebook on the ambient publish so
      the fan-out can validate every chosen ``agent_id``. The fan-out
      fails closed (logs ERROR and raises) when it sees ``None`` here.
    * Fan-out → bridge synthesized-in: the synthesized envelope
      deliberately does NOT carry a phonebook — the bridge's slash
      branch rebuilds deps from its registry on each re-entry, so
      shipping the projection through this hop would be redundant."""

    @classmethod
    def extract(cls, state_metadata: Any) -> Self:
        """Decode a ``state.metadata`` payload into a typed envelope.

        Raises:
            ValueError: ``state_metadata`` is not a dict, or the dict
                does not match the envelope schema (missing ``wire``,
                wire/phonebook contents fail their domain-model
                validation, etc.). Callers in the ambient-routing
                pipeline pair this with ``_raise_infra`` to log +
                raise on infra contract violations.
        """
        if not isinstance(state_metadata, dict):
            raise ValueError(
                f"state.metadata must be a dict, got "
                f"{type(state_metadata).__name__}"
            )
        return cls.model_validate(state_metadata)


class MetadataEnvelopeError(RuntimeError):
    """Raised when a consumer receives a malformed/missing MetadataEnvelope.

    Subclass of RuntimeError (not ValueError) because this signals
    an infrastructure contract violation, not an input validation
    outcome. Kafka's AckPolicy.ACK_FIRST means re-raising produces
    no redelivery — the envelope is gone regardless; an operator
    ERROR log is the only signal. Construction logs at ERROR and
    raises so the consumer framework's exc_info trace surfaces the
    cause.

    Carries structured context (``correlation_id``, ``site``,
    ``reason``) so tests can assert on attributes rather than
    substring-matching messages, and so future structured-logging
    transport can extract fields cleanly.
    """

    def __init__(
        self,
        *,
        correlation_id: str,
        site: str,  # "fanout" or "synthesized-in"
        reason: str,
        cause: Exception | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.site = site
        self.reason = reason
        super().__init__(
            f"{site} infra error: {reason} correlation_id={correlation_id}"
        )
        if cause is not None:
            self.__cause__ = cause


def raise_envelope_error(
    *,
    correlation_id: str,
    site: str,
    reason: str,
    cause: Exception | None = None,
) -> NoReturn:
    """Log at ERROR and raise :class:`MetadataEnvelopeError`."""
    logger.error(
        "%s infra error: %s correlation_id=%s",
        site,
        reason,
        correlation_id,
        exc_info=cause is not None,
    )
    raise MetadataEnvelopeError(
        correlation_id=correlation_id,
        site=site,
        reason=reason,
        cause=cause,
    )


async def invoke_node_with_metadata(
    client: Client,
    *,
    user_prompt: str,
    topic: str,
    metadata: Any,
    deps: dict[str, Any] | None = None,
    reply_topic: str | None = None,
    correlation_id: str | None = None,
    temp_instructions: str | None = None,
    message_history: list[ModelMessage] | None = None,
    run_args: Sequence[Any] | None = None,
    tool_overrides: list[BaseToolNodeSchema] | None = None,
    output_type: type[Any] = _UNSET,
    model_settings: ModelSettings | dict[str, Any] | None = None,
) -> InvocationHandle[Any]:
    """Invoke a node with :attr:`State.metadata` set.

    Behavior is identical to :meth:`Client.invoke_node` EXCEPT
    ``state.metadata`` is set on the constructed :class:`State`
    before publish. ``metadata`` is any JSON-serializable value;
    typical use is a ``dict`` carrying the original wire / phonebook.

    See the module docstring for the upstream cleanup that obviates
    this helper.

    Args:
        client: Connected calfkit :class:`Client`.
        user_prompt: User message — published as the initial
            staged :class:`~pydantic_ai.messages.ModelRequest`.
        topic: Kafka topic the target node subscribes to.
        metadata: Set as ``state.metadata`` on the constructed
            :class:`State`. JSON-serializable.
        deps, reply_topic, correlation_id, temp_instructions,
        message_history, run_args, tool_overrides, output_type,
        model_settings: Mirror :meth:`Client.invoke_node` exactly;
            see that docstring for semantics.

    Returns:
        An :class:`InvocationHandle` whose ``result()`` resolves to
        a :class:`NodeResult` if anyone is awaiting the reply (the
        caller is expected to ``handle._future.cancel()`` for
        fire-and-forget patterns, mirroring the bridge ingress).
    """
    # Behavior parity with Client.invoke_node — same JSON check.
    if model_settings is not None:
        import json  # local — only needed on the model_settings path  # noqa: PLC0415

        try:
            json.dumps(model_settings, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"model_settings is not JSON-serializable: {exc}. "
                f"Payload: {model_settings!r}"
            ) from exc

    if correlation_id is None:
        correlation_id = uuid_utils.uuid7().hex
    if reply_topic is None:
        reply_topic = client.reply_topic

    state = State(
        message_history=message_history or [],
        temp_instructions=temp_instructions,
    )
    state.stage_message(ModelRequest.user_text_prompt(user_prompt))
    state.metadata = metadata

    overrides = (
        OverridesState(
            override_agent_tools=tool_overrides,
            model_settings=dict(model_settings) if model_settings is not None else None,
        )
        if tool_overrides is not None or model_settings is not None
        else None
    )

    # Single-underscore convention-private. Documented in the module
    # docstring as a temporary workaround; FIXME points at the upstream
    # cleanup that removes this dependency.
    return await client._invoke(
        topic=topic,
        reply_topic=reply_topic,
        correlation_id=correlation_id,
        run_args=run_args,
        state=state,
        overrides=overrides,
        deps=deps,
        output_type=output_type,
    )


# Bottom-of-module imports + rebuild for :class:`MetadataEnvelope`'s
# typed fields. The imports MUST live below the class definition so
# the re-entrant load through ``bridge/__init__.py`` →
# ``bridge.ingress`` → ``calfkit_organization._compat.invoke`` finds
# ``MetadataEnvelope`` already bound in this module's namespace by
# the time it resolves; otherwise the second-pass import fails with
# ``cannot import name 'MetadataEnvelope' from partially initialized
# module``. See the TYPE_CHECKING note at the top of the file.
from calfkit_organization.agents.phonebook import (  # noqa: E402, PLC0415
    PhonebookEntry as _PhonebookEntry,
)
from calfkit_organization.bridge.wire import (  # noqa: E402, PLC0415
    WireMessage as _WireMessage,
)

MetadataEnvelope.model_rebuild(
    _types_namespace={
        "WireMessage": _WireMessage,
        "PhonebookEntry": _PhonebookEntry,
    }
)

