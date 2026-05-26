"""Phonebook — wire-format projection of the agent registry.

The bridge owns the canonical :class:`AgentRegistry` (loaded from
``agents/*.md`` at boot). Decoupled deployments such as the
``calfkit-tools`` runner cannot read those files, so the bridge
serializes the registry into a phonebook and ships it in ``deps`` on
every invocation. Downstream consumers (agent runtimes, tool runtimes)
use the phonebook for persona lookup, peer-roster building, and
unknown-agent error messages — without needing local file access or any
view of the registry beyond what the bridge sent.

The phonebook is intentionally a *projection* of the registry, not the
registry itself: it carries only the fields any non-bridge deployment
reasonably needs (identity, persona presentation, description, tools).
Add a field here when a downstream consumer needs it; do not pass
``AgentRegistry`` instances over the wire.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from calfkit_organization.agents.identifier import AGENT_ID_PATTERN

if TYPE_CHECKING:
    # Type-checking-only import; avoids the same package-init cycle that
    # bites peer_roster (bridge.* transitively imports agents.factory).
    from calfkit_organization.bridge.registry import AgentRegistry


class PhonebookEntry(BaseModel):
    """One agent's identity as seen by other deployments.

    Mirrors the subset of :class:`AgentDefinition` that any non-bridge
    consumer of ``deps["phonebook"]`` actually needs: enough to render
    a persona, list peers in a roster, and decide which agents have
    A2A tools available. Field validators enforce the *same* constraints
    the source schema does — the wire format must not be looser than its
    origin or a misbehaving bridge could ship strings downstream
    consumers reject when posted to Discord.
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str
    display_name: str
    avatar_url: str | None = None
    description: str
    tools: tuple[str, ...] = ()
    history_turns: int = Field(default=30, ge=0, le=100)
    """Mirrors :attr:`AgentDefinition.history_turns`. Carried so the A2A
    private_chat tool, which has no registry access, can use the target
    agent's configured history budget when fetching prior turns from a
    Discord thread on continue. Default ``30`` matches
    :attr:`AgentDefinition.history_turns`'s own default, so pre-extension
    phonebook payloads (which lack the field) parse cleanly. ``ge=0/le=100``
    mirrors :attr:`AgentDefinition.history_turns`'s bounds so a malformed
    upstream phonebook (negative or above Discord's per-call cap) is
    rejected at wire-validation time rather than silently disabling
    history or over-fetching."""

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        if not AGENT_ID_PATTERN.fullmatch(v):
            raise ValueError(f"agent_id must match [a-z0-9_-]{{1,32}}, got {v!r}")
        return v

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v: str) -> str:
        # 1-100 mirrors AgentDefinition.description (Discord slash-command
        # description limit). A wire-format phonebook that admits a longer
        # string would surface as a Discord error far away from the bridge
        # that emitted it.
        if not (1 <= len(v) <= 100):
            raise ValueError(f"description must be 1-100 chars, got {len(v)}")
        return v


def phonebook_from_registry(registry: AgentRegistry) -> list[PhonebookEntry]:
    """Snapshot ``registry`` as a list of :class:`PhonebookEntry`.

    Called by the bridge ingress on every invocation so a future
    hot-add on the registry takes effect the next time we publish —
    no caching here.

    The router agent is **filtered out** of the phonebook: it has no
    A2A inbox (``agent.{id}.in`` topic) so it can't receive
    :func:`~calfkit_organization.tools.builtin.private_chat.private_chat`
    invocations, and listing it as a peer would mislead assistants'
    LLMs into trying to call it (the call would publish to a topic
    with no consumer and time out). The router fan-out's per-call
    roster (``router.roster.build_router_temp_instructions``) is the
    intentional place for the router-visible agent list.
    """
    return [
        PhonebookEntry(
            agent_id=spec.agent_id,
            display_name=spec.display_name,
            avatar_url=spec.avatar_url,
            description=spec.description,
            tools=spec.tools,
            history_turns=spec.history_turns,
        )
        for spec in registry.all()
        if spec.role != "router"
    ]


def format_roster_lines(entries: Iterable[PhonebookEntry]) -> str:
    """Render entries as ``- agent_id: description`` lines, newline-joined.

    Shared between :mod:`calfkit_organization.agents.peer_roster`
    (assistant-facing rosters in slash/@-mention/A2A contexts) and
    :mod:`calfkit_organization.router.roster` (the router's
    ambient-fanout roster). Keeping the format in one place means a
    rewording — switching the bullet character, adding the persona
    display name, etc. — updates every roster the LLM sees at once.
    """
    return "\n".join(f"- {e.agent_id}: {e.description}" for e in entries)


def phonebook_to_deps(phonebook: Sequence[PhonebookEntry]) -> list[dict[str, Any]]:
    """Serialize for inclusion in ``deps`` (JSON-friendly nested dict)."""
    return [entry.model_dump(mode="json") for entry in phonebook]


def phonebook_from_deps(raw: object) -> list[PhonebookEntry]:
    """Parse a phonebook out of a raw deps value.

    Raises:
        ValueError: if the value isn't a list or any entry fails schema
            validation. Callers should treat this as an infrastructure
            bug — the bridge is expected to populate a well-formed
            phonebook on every publish.
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"phonebook must be a list, got {type(raw).__name__}"
        )
    return [PhonebookEntry.model_validate(item) for item in raw]
