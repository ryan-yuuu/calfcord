"""Single source of truth for agent_id format constraints.

The pattern ``[a-z0-9_-]{1,32}`` appears in four places across the
codebase: :class:`AgentDefinition.agent_id`,
:class:`PhonebookEntry.agent_id`, :class:`RoutingDecision.agent_id`,
and the bridge normalizer's @-mention scanner. This module is the
canonical definition; importers should reach for
:data:`AGENT_ID_PATTERN` or the :data:`AgentId` annotated type rather
than re-declaring the regex.

This module is intentionally a leaf — only stdlib + pydantic imports
— so any package in the codebase can import it without risking
cycles.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import StringConstraints

AGENT_ID_CHARSET = "a-z0-9_-"
"""Raw character class (without brackets) for agent_id characters.
Use for building related regexes (e.g. the bridge's @-mention
scanner) where the surrounding pattern shape differs but the
character set must match."""

_AGENT_ID_REGEX_STR = rf"[{AGENT_ID_CHARSET}]{{1,32}}"

AGENT_ID_PATTERN = re.compile(_AGENT_ID_REGEX_STR)
"""Compiled regex matching the canonical agent_id format. Use
``.fullmatch(value)`` for membership checks; the character class
also appears verbatim inside the bridge normalizer's @-mention scanner."""

AgentId = Annotated[
    str,
    StringConstraints(pattern=_AGENT_ID_REGEX_STR, min_length=1, max_length=32),
]
"""Nominal type for agent ids on pydantic models. Use as a field
annotation instead of ``str`` to get format validation for free —
e.g. ``agent_id: AgentId`` on a model. Behaves as ``str`` at
runtime; pydantic enforces the length constraints at validation time.

**Known limitation — partial-match pattern enforcement.** Pydantic v2's
:class:`StringConstraints` ``pattern=`` uses
:meth:`re.Pattern.search`-style matching, NOT full-string match. A
value containing any matching substring slips through (verified with
pydantic 2.13: ``"aaaUPPERCASE"`` is accepted because ``"aaa"``
matches the pattern). The ``min_length`` / ``max_length`` constraints
ARE enforced strictly. Consumers that need true full-string format
enforcement (notably :class:`AgentDefinition.agent_id`,
:class:`PhonebookEntry.agent_id`, :class:`HistoryRecord.author_agent_id`)
use an explicit ``@field_validator`` with :meth:`AGENT_ID_PATTERN.fullmatch`
instead of this alias. A future cleanup could anchor the regex with
``^...$`` to force full-string match — but anchoring inside the
``pattern=`` is awkward across pydantic versions; the explicit
validator is the project's canonical approach."""
