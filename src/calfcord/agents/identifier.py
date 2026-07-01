"""Single source of truth for agent_id format constraints.

The pattern ``[a-z0-9_-]{1,32}`` appears in several places across the
codebase (notably :class:`AgentDefinition.agent_id` and the bridge
normalizer's @-mention scanner). This module is the canonical
definition; importers should reach for :data:`AGENT_ID_PATTERN` rather
than re-declaring the regex.

This module is intentionally a leaf — only stdlib imports — so any
package in the codebase can import it without risking cycles.
"""

from __future__ import annotations

import re

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
