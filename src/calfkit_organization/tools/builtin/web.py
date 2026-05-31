"""Web tools: ``web_fetch`` and ``web_search``.

Thin wrappers around ``smolagents``' :class:`VisitWebpageTool` and
:class:`DuckDuckGoSearchTool`. Each tool is constructed lazily as a
module-global singleton so import-time cost stays low — the upstream
classes pull network adapters on first call.

* ``web_fetch`` — fetches a URL, converts HTML to markdown, returns the
  text. Smolagents handles encoding, timeout, and markdown conversion.
  No API key needed.
* ``web_search`` — DuckDuckGo via the ``ddgs`` library. No API key.
  If you want richer results (Brave, Tavily, SerpAPI), see
  ``docs/authoring-tools.md`` for the swap path.

Both ``VisitWebpageTool`` (``markdownify``) and ``DuckDuckGoSearchTool``
(``ddgs``) need third-party packages that smolagents ships ONLY behind
its optional ``toolkit`` extra — bare ``smolagents`` does not pull them
in. This project therefore depends on ``smolagents[toolkit]``; without
the extra, the first ``web_search`` call raises ``ImportError`` ("You
must install package ``ddgs`` ...") from the tool constructor.

Both wrappers convert smolagents exceptions to ``"error: ..."`` strings
rather than raising, so the calling LLM can adapt (retry, rephrase
query, give up) instead of triggering the tool retry-with-feedback path
on a transient network error.
"""

from __future__ import annotations

import logging

from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool

logger = logging.getLogger(__name__)

# Module-globals constructed lazily — see ``_get_*`` helpers below.
# Typed as Any because the smolagents Tool base class is generic and
# uninteresting at the call site.
_visit_tool: object | None = None
_search_tool: object | None = None

_MAX_FETCH_CHARS = 50_000
"""Defense-in-depth cap on what ``web_fetch`` returns. Smolagents already
truncates internally; this guard catches a misconfigured local mirror or
an article whose markdown body slips past the upstream limit. The unit
is *characters* (not bytes — non-Latin scripts use multi-byte UTF-8 but
single chars). 50K is roughly a long article's worth of English
markdown; for non-Latin or dense code dumps the same char budget yields
fewer tokens, which keeps the LLM input safe across content types."""


def _get_visit_tool() -> object:
    """Lazy-init the smolagents VisitWebpageTool singleton."""
    global _visit_tool
    if _visit_tool is None:
        from smolagents import VisitWebpageTool

        _visit_tool = VisitWebpageTool()
    return _visit_tool


def _get_search_tool() -> object:
    """Lazy-init the smolagents DuckDuckGoSearchTool singleton."""
    global _search_tool
    if _search_tool is None:
        from smolagents import DuckDuckGoSearchTool

        _search_tool = DuckDuckGoSearchTool()
    return _search_tool


async def web_fetch(ctx: ToolContext, url: str) -> str:
    """Fetch a web page and return its content as markdown.

    Use this when the user mentions a URL, when you need to read API
    docs / a blog post / a README, or to follow up on a ``web_search``
    result. HTML is converted to markdown so headings, links, and lists
    survive the round-trip; images and styling do not.

    Args:
        url: A fully-qualified ``http://`` or ``https://`` URL.

    Returns:
        The page content as markdown, capped at 50KB. On network errors
        (DNS failure, 4xx/5xx, timeout) returns an ``"error: ..."``
        message so the caller can adapt.
    """
    _ = ctx
    try:
        result = _get_visit_tool().forward(url=url)  # type: ignore[attr-defined]
    except Exception as e:
        # Catch broad: smolagents wraps a wide variety of network/parse
        # errors and we want all of them to flow back to the LLM as a
        # recoverable string rather than triggering tool-error retry.
        logger.warning("web_fetch failed url=%s: %s", url, e)
        return f"error: web_fetch failed for {url!r}: {e}"
    if not isinstance(result, str):
        result = str(result)
    if len(result) > _MAX_FETCH_CHARS:
        return result[:_MAX_FETCH_CHARS] + f"\n\n(truncated at {_MAX_FETCH_CHARS} chars)"
    return result


async def web_search(ctx: ToolContext, query: str) -> str:
    """Search the web with DuckDuckGo and return the top results.

    Use this when you need fresh information you don't already have:
    current events, recent library versions, error messages, or a
    starting point for a topic you haven't read about yet. Follow up
    with ``web_fetch`` on a promising result URL.

    Args:
        query: Natural-language search query. The same query rules
            that work in DuckDuckGo's search bar work here (quotes for
            exact phrase, ``site:`` filters, etc.).

    Returns:
        A list of result titles, URLs, and snippets, formatted as
        markdown. On network errors returns an ``"error: ..."`` message.
    """
    _ = ctx
    try:
        result = _get_search_tool().forward(query=query)  # type: ignore[attr-defined]
    except Exception as e:
        # Same rationale as web_fetch — surface all errors as strings.
        logger.warning("web_search failed query=%r: %s", query, e)
        return f"error: web_search failed for {query!r}: {e}"
    if not isinstance(result, str):
        result = str(result)
    return result


web_fetch_tool: ToolNodeDef = agent_tool(web_fetch)
web_search_tool: ToolNodeDef = agent_tool(web_search)
