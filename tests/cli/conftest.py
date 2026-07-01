"""Shared fixtures for the CLI test package."""

from __future__ import annotations

import webbrowser

import pytest


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may ever open a real browser tab on the developer's machine.

    ``init``'s invite step pops the OAuth page via ``webbrowser.open`` as a
    best-effort default; any test that drives the wizard without injecting
    ``open_url_fn`` would otherwise open Discord for the fake app id. Tests
    that assert open behavior re-patch this seam themselves.
    """
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _offline_mcp_enumeration_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the tool-checkbox surfaces (agent tools editor, create wizard's
    pick_tools) off the network in unit tests: the default MCP enumeration
    reads mcp.json and probes the broker's capability topic, which a unit
    test must never do. The snapshot is stubbed at the capability_read seam
    (an empty-but-successful view) so ``_default_live_tools``'s own logic
    still runs; tests exercising MCP rows inject their own
    ``mcp_servers_fn`` / ``live_tools_fn`` or re-patch these seams.
    """
    from calfcord.cli import agent_tools
    from calfcord.mcp import capability_read

    monkeypatch.setattr(agent_tools, "_default_mcp_servers", lambda: [])
    monkeypatch.setattr(capability_read, "snapshot_capability_tools", lambda *a, **k: {})
