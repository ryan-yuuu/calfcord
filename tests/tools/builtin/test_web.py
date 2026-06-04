"""Tests for the web_fetch / web_search wrappers — mock the smolagents
tool instances so we don't hit the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from calfkit.models import ToolContext

from calfcord.tools.builtin import web


def _ctx() -> ToolContext:
    return ToolContext(
        deps={},
        run_id="c",
        agent_name="alice",
    )


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    web._visit_tool = None
    web._search_tool = None
    yield
    web._visit_tool = None
    web._search_tool = None


class TestWebFetch:
    async def test_returns_smolagents_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = MagicMock()
        fake.forward.return_value = "# Hello\n\nFetched body"
        monkeypatch.setattr(web, "_get_visit_tool", lambda: fake)
        result = await web.web_fetch(_ctx(), "https://example.com")
        assert "Fetched body" in result
        fake.forward.assert_called_once_with(url="https://example.com")

    async def test_truncates_oversized_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        huge = "x" * (web._MAX_FETCH_CHARS + 1000)
        fake = MagicMock()
        fake.forward.return_value = huge
        monkeypatch.setattr(web, "_get_visit_tool", lambda: fake)
        result = await web.web_fetch(_ctx(), "https://example.com")
        assert "truncated" in result
        # Body is the first MAX_FETCH_CHARS plus the trailer; it must be
        # shorter than the full input.
        assert len(result) < len(huge) + 200

    async def test_network_error_returns_error_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = MagicMock()
        fake.forward.side_effect = ConnectionError("dns lookup failed")
        monkeypatch.setattr(web, "_get_visit_tool", lambda: fake)
        result = await web.web_fetch(_ctx(), "https://bad.example")
        assert result.startswith("error:")
        assert "dns lookup failed" in result


class TestWebSearch:
    async def test_passes_query_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = MagicMock()
        fake.forward.return_value = "1. example.com — top result"
        monkeypatch.setattr(web, "_get_search_tool", lambda: fake)
        result = await web.web_search(_ctx(), "calfcord docs")
        assert "top result" in result
        fake.forward.assert_called_once_with(query="calfcord docs")

    async def test_network_error_returns_error_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = MagicMock()
        fake.forward.side_effect = RuntimeError("ddg rate limited")
        monkeypatch.setattr(web, "_get_search_tool", lambda: fake)
        result = await web.web_search(_ctx(), "x")
        assert result.startswith("error:")
        assert "ddg rate limited" in result
