"""Unit tests for the shared supervisor seam (``_workspace.py``, Fix #14).

``lifecycle`` / ``roster`` / ``component`` / ``cli.doctor`` all build on these four
consolidated primitives, so they are pinned here once: the per-home client
resolver, the workspace-up probe, the one not-running hint, and the
``{"data": [...]}``-vs-bare-list process-list normalizer. The surfaces keep thin
re-export aliases (``roster._resolve_client``, ``component._workspace_is_up``,
``lifecycle._process_rows`` …) whose own tests cover the wiring; these cover the
seam directly so the shared behavior is not only ever exercised second-hand.
"""

from __future__ import annotations

from calfcord.supervisor import _workspace
from calfcord.supervisor.client import ProcessComposeClient


class _StubClient:
    """A scriptable stand-in: ``project_state`` raises iff the workspace is down."""

    def __init__(self, *, up: bool) -> None:
        self._up = up

    async def project_state(self):
        if not self._up:
            # Mirrors ProcessComposeClient: a transport failure surfaces as
            # RuntimeError, which the up-probe reads as "not running".
            raise RuntimeError("project_state: connection refused")
        return {"status": "ok"}


def test_one_hint_string_is_shared_by_every_surface() -> None:
    # The hint must be byte-identical everywhere so the lifecycle surfaces speak
    # with one voice (the whole point of consolidating it).
    from calfcord.supervisor import component, roster

    assert roster._NOT_RUNNING_HINT is _workspace.WORKSPACE_NOT_RUNNING_HINT
    assert component._NOT_RUNNING_HINT is _workspace.WORKSPACE_NOT_RUNNING_HINT
    assert "disco start" in _workspace.WORKSPACE_NOT_RUNNING_HINT


def test_resolve_client_passes_through_an_injected_client() -> None:
    injected = ProcessComposeClient(port=1234)
    assert _workspace.resolve_client(injected, "/srv/home") is injected


def test_resolve_client_defaults_to_a_per_home_client(tmp_path) -> None:
    # With no client injected the resolver builds a per-home ProcessComposeClient
    # on the port pc_port_for derives from the home (the port `up -p` pinned).
    from calfcord.supervisor.lifecycle import pc_port_for

    home = str(tmp_path)
    client = _workspace.resolve_client(None, home)
    assert isinstance(client, ProcessComposeClient)
    expected = ProcessComposeClient(port=pc_port_for(home))
    # Equal base URLs prove equal ports without a live call.
    assert client._base_url == expected._base_url


async def test_workspace_is_up_true_when_project_state_answers() -> None:
    assert await _workspace.workspace_is_up(_StubClient(up=True)) is True


async def test_workspace_is_up_false_on_transport_runtimeerror() -> None:
    assert await _workspace.workspace_is_up(_StubClient(up=False)) is False


def test_iter_process_dicts_handles_bare_list() -> None:
    payload = [{"name": "broker"}, {"name": "bridge"}]
    assert list(_workspace.iter_process_dicts(payload)) == payload


def test_iter_process_dicts_unwraps_data_envelope() -> None:
    # Process Compose's process-list shape wobbles across versions; the
    # ``{"data": [...]}`` envelope must be unwrapped exactly like the bare list.
    inner = [{"name": "broker"}]
    assert list(_workspace.iter_process_dicts({"data": inner})) == inner


def test_iter_process_dicts_skips_non_dicts_and_none() -> None:
    # A stray non-dict entry (or a None payload) must be skipped, never crash a
    # caller (the status board / ps physical view / drift read).
    assert list(_workspace.iter_process_dicts(["junk", {"name": "broker"}, 7])) == [
        {"name": "broker"}
    ]
    assert list(_workspace.iter_process_dicts(None)) == []
