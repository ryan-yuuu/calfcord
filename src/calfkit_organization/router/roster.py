"""Per-invocation agent roster injected as the router's ``temp_instructions``.

The router needs to know which assistants exist before it can sensibly
emit a :class:`~calfkit_organization.agents.routing.RoutingDecision`. We
surface that list as ``temp_instructions`` on each invocation — same
pattern as :mod:`calfkit_organization.agents.peer_roster`, but the
constraints differ:

* The router has no ``private_chat`` tool, so we do NOT gate visibility
  on a tool name. Every assistant is a candidate respondent regardless
  of which tools (if any) it declares.
* The router itself is excluded from the roster — it can never be a
  respondent. In production this filter is belt-and-suspenders:
  :func:`~calfkit_organization.agents.phonebook.phonebook_from_registry`
  already drops the router upstream, so the input phonebook normally
  doesn't contain it. The filter here defends against direct callers
  that construct a phonebook some other way (tests, future
  integrations) and pass the registry verbatim.

Operates on a :class:`PhonebookEntry` sequence (the same wire shape used
elsewhere in the project) so the same helper is reusable in any
deployment that received the phonebook via ``deps``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from calfkit_organization.agents.phonebook import PhonebookEntry
from calfkit_organization.router.definition import ROUTER_AGENT_ID

logger = logging.getLogger(__name__)


def build_router_temp_instructions(
    phonebook: Sequence[PhonebookEntry],
) -> str | None:
    """Return the ``temp_instructions`` to inject for a router invocation.

    Returns ``None`` when there is nothing useful to advertise — the
    phonebook is empty, or after filtering out the router itself the
    list is empty. Callers can pass the result straight through to
    :func:`calfkit_organization._compat.invoke.invoke_node_with_metadata`
    (``temp_instructions=None`` is treated as no-op by calfkit).

    Args:
        phonebook: The current set of known agents. The bridge builds
            this on every invocation so any registry hot-add takes
            effect on the next call. The router is excluded from the
            output even if present in the input.

    Returns:
        A short multi-line block listing each non-router agent's id
        and description, or ``None`` if no eligible respondents exist.

    Operator signal: when ``None`` is returned (the
    no-eligible-respondents case), a WARN is logged on this side
    identifying the registry shape. The bridge ingress logs an
    ERROR at the same point (with the event/channel that triggered
    it), so a single failure window produces one WARN + one ERROR
    that an operator can correlate. The WARN names the cause (no
    assistants registered); the ERROR names the symptom (a
    specific ambient message that won't get a response).
    """
    candidates = [e for e in phonebook if e.agent_id != ROUTER_AGENT_ID]
    if not candidates:
        logger.warning(
            "build_router_temp_instructions: no eligible respondents "
            "(phonebook size=%d excluding router). Router LLM will "
            "run with no agents to choose from; ambient messages "
            "will likely produce no responses. Check that the "
            "registry includes at least one non-router agent.",
            len(phonebook),
        )
        return None
    lines = [f"- {e.agent_id}: {e.description}" for e in candidates]
    return (
        "Available agents you can route to:\n"
        + "\n".join(lines)
    )
