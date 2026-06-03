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
    McpSelector,
    is_mcp_selector,
    is_valid_server_name,
    parse_mcp_selector,
    validate_mcp_selector,
)


class TestMcpSelectorType:
    """The parsed result is an :class:`McpSelector` NamedTuple — named-field
    access plus a ``selects_all_tools`` predicate — while staying compatible
    with the legacy ``(server, tool)`` tuple (equality and unpacking)."""

    def test_named_fields_and_predicate(self) -> None:
        sel = parse_mcp_selector("mcp/gmail/search")
        assert isinstance(sel, McpSelector)
        assert sel.server == "gmail"
        assert sel.tool == "search"
        assert sel.selects_all_tools is False

    def test_bare_server_selects_all(self) -> None:
        sel = parse_mcp_selector("mcp/gmail")
        assert sel.server == "gmail"
        assert sel.tool is None
        assert sel.selects_all_tools is True

    def test_backward_compatible_with_tuple(self) -> None:
        sel = parse_mcp_selector("mcp/gmail/search")
        assert sel == ("gmail", "search")
        server, tool = sel
        assert (server, tool) == ("gmail", "search")


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

    def test_server_segment_at_length_bound_accepted(self) -> None:
        """The server grammar caps at 64 chars — exactly 64 is accepted."""
        server = "a" * 64
        assert parse_mcp_selector(f"mcp/{server}") == (server, None)

    def test_tool_segment_at_length_bound_accepted(self) -> None:
        """The tool grammar caps at 128 chars — exactly 128 is accepted."""
        tool = "a" * 128
        assert parse_mcp_selector(f"mcp/demo/{tool}") == ("demo", tool)


class TestParseLengthBounds:
    def test_server_segment_over_length_bound_rejected(self) -> None:
        """65 chars exceeds the 64-char server cap and is rejected."""
        with pytest.raises(ValueError, match="invalid server name"):
            parse_mcp_selector(f"mcp/{'a' * 65}")

    def test_tool_segment_over_length_bound_rejected(self) -> None:
        """129 chars exceeds the 128-char tool cap and is rejected."""
        with pytest.raises(ValueError, match="invalid tool name"):
            parse_mcp_selector(f"mcp/demo/{'a' * 129}")


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


class TestIsValidServerName:
    """``is_valid_server_name`` is the single-source check that a bare
    server name (e.g. a ``schemas/`` module name) matches the SAME
    ``[a-z0-9_]{1,64}`` grammar ``parse_mcp_selector`` enforces on the
    server segment."""

    @pytest.mark.parametrize(
        "name",
        ["gmail", "g", "srv_2", "a_b_c", "server123", "a" * 64],
    )
    def test_true_for_valid(self, name: str) -> None:
        assert is_valid_server_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Gmail",       # uppercase rejected (server grammar is lowercase)
            "GMAIL",       # all-caps
            "",            # empty
            "a" * 65,      # over the 64-char bound
            "list-labels",  # hyphen not allowed in a server segment
            "a.b",         # dot not allowed
            "a b",         # space not allowed
            "a/b",         # slash not allowed
        ],
    )
    def test_false_for_invalid(self, name: str) -> None:
        assert is_valid_server_name(name) is False
