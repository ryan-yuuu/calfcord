"""Tests for ``calfcord explain topology`` (src/calfcord/cli/explain.py).

``explain`` is a pure teaching command: no supervisor, no broker, no install
home. These tests pin the *content contract* (it must name the substrate, the
roster, the four process types, and the distributed graduation) through an
injected output sink.
"""

from __future__ import annotations

import pytest

from calfcord.cli import explain

# --------------------------------------------------------------------- render


def test_render_topology_returns_text() -> None:
    text = explain.render_topology()
    assert isinstance(text, str)
    assert text.strip(), "topology screen must not be blank"


def test_topology_names_the_substrate() -> None:
    # The always-on background office: broker + bridge, autostarted by ``start``.
    text = explain.render_topology().lower()
    assert "substrate" in text
    assert "broker" in text
    assert "bridge" in text


def test_topology_names_the_roster() -> None:
    # Teammates that clock in/out on demand: agents, tools, mcp.
    text = explain.render_topology().lower()
    assert "roster" in text
    for member in ("agent", "tools", "mcp"):
        assert member in text, f"topology omits roster member {member!r}"


def test_topology_names_the_four_process_types() -> None:
    # The four calfkit-* process types onboarding maps the layers onto.
    text = explain.render_topology()
    for proc in ("calfkit-bridge", "calfkit-agent", "calfkit-tools", "calfkit-mcp"):
        assert proc in text, f"topology omits process type {proc!r}"


def test_topology_explains_distributed_graduation() -> None:
    # The load-bearing invariant: same config/commands on one host or twenty;
    # graduating is a deployment change (a remote broker URL), never a rewrite.
    text = explain.render_topology().lower()
    assert "distributed" in text
    assert "broker url" in text or "remote broker" in text
    # The shared wire that makes graduation a config change, not a rewrite.
    assert "kafka" in text


# --------------------------------------------------------------------- run


def test_run_topology_writes_to_injected_sink_and_returns_zero() -> None:
    captured: list[str] = []
    code = explain.run_topology(out=captured.append)
    assert code == 0
    # The sink received the rendered screen, verbatim, with no stdout capture.
    assert captured == [explain.render_topology()]


def test_run_dispatches_the_topology_topic() -> None:
    captured: list[str] = []
    code = explain.run("topology", out=captured.append)
    assert code == 0
    assert captured == [explain.render_topology()]


def test_run_rejects_an_unknown_topic() -> None:
    captured: list[str] = []
    code = explain.run("nonsense", out=captured.append)
    assert code == 1
    # The error names the offending topic and the topics that DO exist, so the
    # message is actionable rather than a bare failure.
    joined = "\n".join(captured)
    assert "nonsense" in joined
    assert "topology" in joined


def test_topic_registry_is_extensible_and_currently_ships_only_topology() -> None:
    # The dispatch table is the seam future topics register against; today it is
    # exactly ``{"topology"}`` so the surface stays honest about what ships.
    assert set(explain.TOPICS) == {"topology"}


# --------------------------------------------------------------------- no home


def test_explain_needs_no_install_home(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pure teaching screen: it must render with no ``$CALFCORD_HOME`` and touch
    # no filesystem, so a dev run and a native install print the same thing.
    monkeypatch.delenv("CALFCORD_HOME", raising=False)
    captured: list[str] = []
    assert explain.run_topology(out=captured.append) == 0
    assert captured and captured[0].strip()


def test_topology_names_mcp_servers() -> None:
    """The teaching screen covers the fifth process type: per-server MCP
    toolboxes (calfkit-mcp), roster members holding the MCP credentials."""
    text = explain.render_topology()
    assert "calfkit-mcp" in text
    assert "mcp" in text.lower()
