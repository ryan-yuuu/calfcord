"""Unit tests for :mod:`calfcord.mcp.selector`.

The selector module is a pure leaf: it recognizes and decomposes the
``mcp/...`` tool-selector syntax used in agent frontmatter without touching
the catalog, calfkit, or any transport. These tests pin the two documented
forms (``mcp/<server>`` and ``mcp/<server>/<tool>``), the rejection of every
malformed shape, and the cheap ``is_mcp_selector`` prefix check.
"""

from __future__ import annotations

import pytest

from calfcord.mcp.selector import (
    MCP_SELECTOR_PREFIX,
    is_mcp_selector,
    parse_mcp_selector,
    validate_mcp_selector,
)


class TestIsMcpSelector:
    @pytest.mark.parametrize(
        "entry",
        ["mcp/gmail", "mcp/gmail/search", "mcp/", "mcp/a/b/c"],
    )
    def test_true_for_mcp_prefixed(self, entry: str) -> None:
        """``is_mcp_selector`` is a cheap prefix check — it returns ``True``
        for anything starting with ``mcp/``, even structurally malformed
        selectors (validation is a separate step)."""
        assert is_mcp_selector(entry) is True

    @pytest.mark.parametrize(
        "entry",
        ["shell", "calendar", "email", "web_search", "mcp", "mcpx/gmail", ""],
    )
    def test_false_for_bare_names(self, entry: str) -> None:
        """Bare builtin names (and near-misses that lack the trailing slash)
        are not MCP selectors."""
        assert is_mcp_selector(entry) is False

    def test_prefix_constant_value(self) -> None:
        assert MCP_SELECTOR_PREFIX == "mcp/"


class TestParseValid:
    def test_bare_server_yields_none_tool(self) -> None:
        """``mcp/<server>`` selects every tool of the server — tool is ``None``."""
        assert parse_mcp_selector("mcp/gmail") == ("gmail", None)

    def test_server_and_tool(self) -> None:
        assert parse_mcp_selector("mcp/gmail/search") == ("gmail", "search")

    def test_tool_segment_allows_hyphen_and_mixed_case(self) -> None:
        """The tool segment matches the upstream-advertised tool name, which
        we do not control and which may use hyphens or mixed case."""
        assert parse_mcp_selector("mcp/demo/get-Item") == ("demo", "get-Item")

    def test_server_segment_allows_underscore_and_digits(self) -> None:
        assert parse_mcp_selector("mcp/srv_2") == ("srv_2", None)


class TestParseMalformed:
    @pytest.mark.parametrize(
        "entry",
        [
            "mcp/",          # empty server segment
            "mcp/a/b/c",     # too many segments
            "mcp//x",        # empty server (doubled slash)
            "mcp/gmail/",    # empty tool segment
            "mcp/Gmail",     # uppercase server (server grammar is [a-z0-9_])
            "mcp/gmail/bad tool",  # space in tool name (bad charset)
            "shell",         # non-mcp prefix
            "calendar",      # non-mcp prefix
        ],
    )
    def test_raises_value_error(self, entry: str) -> None:
        with pytest.raises(ValueError):
            parse_mcp_selector(entry)

    @pytest.mark.parametrize("entry", ["mcp/", "mcp/a/b/c", "mcp//x", "mcp/gmail/"])
    def test_error_names_the_offending_entry(self, entry: str) -> None:
        """Every rejection names ``entry`` verbatim so a frontmatter typo
        surfaces with the exact bad string."""
        with pytest.raises(ValueError, match=repr(entry)):
            parse_mcp_selector(entry)


class TestValidateMcpSelector:
    def test_returns_none_on_valid(self) -> None:
        assert validate_mcp_selector("mcp/gmail/search") is None

    def test_raises_on_malformed(self) -> None:
        with pytest.raises(ValueError):
            validate_mcp_selector("mcp/a/b/c")
