"""Parsing + validation for ``mcp/...`` tool selectors in agent frontmatter.

Background
----------

Agents declare their tools in ``agents/*.md`` frontmatter under a single
``tools:`` list. Builtin tools appear there by bare name (``shell``,
``web``); MCP-server tools appear in the *same* list using a ``mcp/``
selector syntax so an author never has to learn a second declaration
mechanism:

* ``mcp/<server>`` — expose *every* tool the named MCP server publishes.
* ``mcp/<server>/<tool>`` — expose a single tool of that server.

The agent's LLM sees each tool under the name the MCP server advertises;
tool calls dispatch to the server's calfkit toolbox over the
``mcp_server.<server>`` topic. Resolving a selector against the live
capability view is handled downstream in :mod:`calfcord.mcp.agent_select`;
this module is concerned only with recognizing and decomposing the
selector string itself.

Design choices
--------------

* **Leaf module, zero project imports** — this file imports nothing from
  :mod:`calfcord` or :mod:`calfkit` (only :mod:`re` + stdlib). The agent
  frontmatter parser (``calfcord.agents.definition``) needs to recognize
  and validate selectors *before* anything decides whether to build the
  schema-only MCP catalog. Keeping this module a pure leaf lets that
  parser ``from calfcord.mcp.selector import ...`` without dragging in
  the catalog build (which imports :mod:`calfkit`) or risking an import
  cycle with the ``tools`` package.

* **Regexes redeclared, not imported** — the character-class rules below
  intentionally duplicate the spirit of ``TOOL_NAME_REGEX`` in
  :mod:`calfcord.tools.discovery` rather than importing it. Importing
  from the ``tools`` package would create a coupling (and a potential
  import cycle, since tool modules import back through bridge/agent code)
  for the sake of one shared constant. A few lines of duplication here
  buys this module its leaf status; the regexes are commented so a future
  reader knows the omission is deliberate.

* **Two distinct name grammars** — the *server* segment is constrained to
  ``[a-z0-9_]`` because it must double as a Kafka topic segment
  (``mcp_server.<server>``), an ``mcp.json`` key, and the suffix of the
  server's roster process name (``mcp-<server>``); lowercase-plus-
  underscore is the safe intersection.
  The *tool* segment allows ``[a-zA-Z0-9_-]`` (and a longer bound) to
  match the original MCP tool name as advertised by the upstream server,
  which we do not control and which commonly uses mixed case or hyphens.

* **Strict, message-rich ``ValueError``** — every rejection names the
  offending ``entry`` verbatim so a typo in an ``agents/*.md`` file
  surfaces with the exact bad string, not a generic "invalid selector".
"""

from __future__ import annotations

import re
from typing import NamedTuple

MCP_SELECTOR_PREFIX = "mcp/"
"""The literal prefix that marks a ``tools:`` entry as an MCP selector
rather than a builtin tool name. A bare entry like ``shell`` is a builtin;
``mcp/gmail`` or ``mcp/gmail/search`` is an MCP selector."""

# Server segment: must double as a Kafka topic segment, an mcp.json key,
# and a roster process-name suffix, so we restrict to lowercase + digits +
# underscore. Redeclared here (rather than imported from
# calfcord.tools.discovery) to keep this module a pure leaf — see the
# module docstring's "Regexes redeclared" note.
_SERVER_NAME_REGEX = re.compile(r"^[a-z0-9_]{1,64}$")

# Tool segment: matches the *original* MCP tool name advertised by an
# upstream server we do not control; allow mixed case + hyphen + a longer
# bound, mirroring ``TOOL_NAME_REGEX`` in calfcord.tools.discovery (also
# redeclared, not imported — same leaf-module rationale).
_TOOL_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def is_valid_server_name(name: str) -> bool:
    """Return whether ``name`` is a legal ``<server>`` selector segment.

    Single source of truth for "is this a valid MCP server name?" — it
    matches against the *same* module-level :data:`_SERVER_NAME_REGEX`
    (``^[a-z0-9_]{1,64}$``) that :func:`parse_mcp_selector` enforces on the
    server segment, so a name accepted here is exactly a name a ``mcp/...``
    selector can reach. Reuses the compiled regex rather than re-spelling
    the pattern, so the grammar cannot drift between the two call sites.

    The intended consumers are the ``mcp.json`` loader and the
    ``calfcord mcp add`` writer, which key server config by this name: a
    configured server whose name is not a legal server segment would be
    unreachable by any valid ``mcp/...`` selector (and could not name a
    Kafka topic segment or roster process). Validating here lets both
    reject that case loudly instead.

    Args:
        name: A candidate server name (typically a ``schemas/`` module
            name). Not a full ``mcp/...`` selector — just the bare server
            segment.

    Returns:
        ``True`` if ``name`` matches the server-segment grammar, else
        ``False``.
    """
    return bool(_SERVER_NAME_REGEX.match(name))


def is_mcp_selector(entry: str) -> bool:
    """Return ``True`` if ``entry`` is an MCP selector (vs. a builtin name).

    A cheap prefix check only — it does *not* validate the rest of the
    selector. Callers that need a structurally-valid selector should run
    :func:`validate_mcp_selector` (or :func:`parse_mcp_selector`) after
    this returns ``True``. Splitting "is it ours?" from "is it valid?"
    lets the frontmatter parser route a ``mcp/`` entry into MCP handling
    and *then* report a precise parse error, rather than silently treating
    a malformed ``mcp/`` entry as an (always-unknown) builtin name.
    """
    return entry.startswith(MCP_SELECTOR_PREFIX)


class McpSelector(NamedTuple):
    """A parsed ``mcp/...`` selector: a server plus an optional single tool.

    ``tool is None`` means "all tools of ``server``" (the bare ``mcp/<server>``
    form); a concrete ``tool`` selects exactly one (``mcp/<server>/<tool>``).
    Prefer reading that distinction through :attr:`selects_all_tools` over
    re-checking ``tool is None`` at each call site.

    As a :class:`~typing.NamedTuple` it stays backward compatible with the
    previous ``(server, tool)`` return: positional unpacking
    (``server, tool = parse_mcp_selector(...)``) and tuple equality
    (``== ("gmail", "search")``) both still hold.
    """

    server: str
    tool: str | None

    @property
    def selects_all_tools(self) -> bool:
        """Whether this selector expands to every tool of :attr:`server`."""
        return self.tool is None


def parse_mcp_selector(entry: str) -> McpSelector:
    """Decompose an MCP selector into ``(server, tool_or_none)``.

    Examples::

        parse_mcp_selector("mcp/gmail")         -> ("gmail", None)
        parse_mcp_selector("mcp/gmail/search")  -> ("gmail", "search")

    A ``None`` tool means "all tools of this server" (the bare-server
    form); a non-``None`` tool selects exactly one.

    Args:
        entry: The raw ``tools:`` list entry. Must start with
            :data:`MCP_SELECTOR_PREFIX`.

    Returns:
        An :class:`McpSelector` (a ``(server, tool)`` NamedTuple) where
        ``tool`` is ``None`` for the bare-server form and the original tool
        name otherwise. It unpacks and compares as the plain ``(server,
        tool)`` tuple it replaced, so existing callers are unaffected.

    Raises:
        ValueError: When ``entry`` does not start with the ``mcp/``
            prefix, splits into a segment count other than 2 or 3, has an
            empty server or tool segment, or has a server/tool segment
            that violates the respective name grammar. The message always
            names ``entry`` verbatim so the offending frontmatter line is
            unambiguous.
    """
    if not entry.startswith(MCP_SELECTOR_PREFIX):
        raise ValueError(
            f"MCP selector {entry!r} must start with {MCP_SELECTOR_PREFIX!r}"
        )

    # Split the WHOLE entry (not the post-prefix remainder) so the segment
    # count is checked against the documented forms directly:
    #   "mcp/gmail"        -> ["mcp", "gmail"]            (len 2)
    #   "mcp/gmail/search" -> ["mcp", "gmail", "search"]  (len 3)
    # Anything else (a trailing slash, an extra path segment, a doubled
    # slash producing an empty middle) lands outside {2, 3} or trips the
    # emptiness checks below.
    segments = entry.split("/")
    if len(segments) not in (2, 3):
        raise ValueError(
            f"MCP selector {entry!r} must be 'mcp/<server>' or "
            f"'mcp/<server>/<tool>', got {len(segments)} '/'-separated "
            f"segment(s)"
        )

    server = segments[1]
    tool = segments[2] if len(segments) == 3 else None

    if not server:
        raise ValueError(f"MCP selector {entry!r} has an empty server segment")
    if not _SERVER_NAME_REGEX.match(server):
        raise ValueError(
            f"MCP selector {entry!r} has invalid server name {server!r}; "
            f"must match {_SERVER_NAME_REGEX.pattern}"
        )

    if tool is not None:
        if not tool:
            raise ValueError(f"MCP selector {entry!r} has an empty tool segment")
        if not _TOOL_NAME_REGEX.match(tool):
            raise ValueError(
                f"MCP selector {entry!r} has invalid tool name {tool!r}; "
                f"must match {_TOOL_NAME_REGEX.pattern}"
            )

    return McpSelector(server, tool)


def validate_mcp_selector(entry: str) -> None:
    """Raise :class:`ValueError` if ``entry`` is not a well-formed selector.

    Thin wrapper over :func:`parse_mcp_selector` that discards the parsed
    result — for call sites (e.g. the frontmatter validator) that care
    only about *whether* a selector is structurally valid, not about its
    decomposed parts. Keeping this as a named function makes those call
    sites read as the assertion they are and avoids a stray
    ``parse_mcp_selector(entry)``-with-unused-result lint smell.
    """
    parse_mcp_selector(entry)
