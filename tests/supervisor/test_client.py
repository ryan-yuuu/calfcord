"""Unit tests for the Process Compose REST client (the single HTTP seam).

``ProcessComposeClient`` is the one module that knows Process Compose's wire
routes (design §13.2) — so these tests pin *method + path* for every call,
because the routes are version-fragile and the docs were partly wrong: ``stop``
is a ``PATCH`` (not ``POST``), ``start``/``restart`` are ``POST``, and the
single-process state lives at ``GET /process/{name}`` (``/process/{name}/state``
is a 404). The HTTP layer is stubbed with ``httpx.MockTransport`` so no Process
Compose server is needed; the gated real-binary exercise lives in
``tests/integration/test_pc_client.py``.
"""

from __future__ import annotations

import httpx
import pytest

from calfcord.supervisor.client import ProcessComposeClient

_PORT = 9911


def _record(
    handler_response: httpx.Response,
) -> tuple[list[httpx.Request], ProcessComposeClient]:
    """A client whose injected transport records every request and replies fixed."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler_response

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return seen, ProcessComposeClient(port=_PORT, client_factory=factory)


def _json_client(
    payload: object,
) -> tuple[list[httpx.Request], ProcessComposeClient]:
    return _record(httpx.Response(200, json=payload))


# --- route + method pinning -------------------------------------------------


async def test_list_processes_is_get_processes() -> None:
    seen, client = _json_client({"data": []})
    await client.list_processes()
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/processes"


async def test_get_process_is_get_process_name_not_state() -> None:
    # /process/{name}/state is a 404 in v1.110.0 — the bare path is the live one.
    seen, client = _json_client({"status": "Running", "is_ready": "Ready"})
    await client.get_process("assistant")
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/process/assistant"


async def test_get_process_info_is_get_process_info_name() -> None:
    seen, client = _json_client({"name": "assistant"})
    await client.get_process_info("assistant")
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/process/info/assistant"


async def test_project_state_is_get_project_state() -> None:
    seen, client = _json_client({"running": True})
    await client.project_state()
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/project/state"


async def test_start_process_is_post() -> None:
    seen, client = _json_client({})
    await client.start_process("assistant")
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/process/start/assistant"


async def test_stop_process_is_patch_not_post() -> None:
    # The headline route correction: stop is a PATCH (the docs say POST).
    seen, client = _json_client({})
    await client.stop_process("assistant")
    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/process/stop/assistant"


async def test_restart_process_is_post() -> None:
    seen, client = _json_client({})
    await client.restart_process("assistant")
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/process/restart/assistant"


async def test_get_logs_is_get_with_offset_and_limit_in_path() -> None:
    seen, client = _record(httpx.Response(200, json={"logs": ["a", "b"]}))
    await client.get_logs("assistant", 100, 50)
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/process/logs/assistant/100/50"


async def test_update_project_is_post_project_with_body() -> None:
    seen, client = _json_client({})
    await client.update_project("version: '0.5'\n")
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/project"
    assert seen[0].content == b"version: '0.5'\n"


# --- response parsing -------------------------------------------------------


async def test_list_processes_returns_parsed_json() -> None:
    payload = {"data": [{"name": "broker"}, {"name": "bridge"}]}
    _, client = _json_client(payload)
    assert await client.list_processes() == payload


async def test_get_process_returns_parsed_state() -> None:
    state = {"status": "Running", "pid": 4242, "is_ready": "Ready", "restarts": 0}
    _, client = _json_client(state)
    assert await client.get_process("assistant") == state


# --- base url / port + auth -------------------------------------------------


async def test_port_drives_the_base_url() -> None:
    seen, client = _json_client({"data": []})
    await client.list_processes()
    url = seen[0].url
    assert url.host == "localhost"
    assert url.port == _PORT


async def test_explicit_base_url_overrides_port() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    client = ProcessComposeClient(
        base_url="http://10.0.0.5:7000",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    await client.list_processes()
    assert seen[0].url.host == "10.0.0.5"
    assert seen[0].url.port == 7000


async def test_token_is_sent_as_pc_header() -> None:
    seen, _ = _record(httpx.Response(200, json={}))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = ProcessComposeClient(
        port=_PORT,
        token="a-very-long-pc-api-token-key",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    await client.start_process("assistant")
    assert seen[0].headers["X-PC-Token-Key"] == "a-very-long-pc-api-token-key"


async def test_no_token_means_no_pc_header() -> None:
    seen, client = _json_client({})
    await client.start_process("assistant")
    assert "X-PC-Token-Key" not in seen[0].headers


def test_default_factory_builds_a_real_async_client() -> None:
    # With no injected factory the client must fall back to a real
    # httpx.AsyncClient (the production path); building it touches no network.
    client = ProcessComposeClient()
    built = client._client_factory()
    assert isinstance(built, httpx.AsyncClient)


# --- error mapping ----------------------------------------------------------


async def test_non_2xx_raises_runtimeerror_with_route_and_status() -> None:
    _, client = _record(httpx.Response(404, text="no such process"))
    with pytest.raises(RuntimeError) as excinfo:
        await client.get_process("ghost")
    message = str(excinfo.value)
    assert "get_process" in message
    assert "/process/ghost" in message
    assert "404" in message


async def test_stop_non_2xx_raises() -> None:
    _, client = _record(httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError) as excinfo:
        await client.stop_process("assistant")
    assert "stop_process" in str(excinfo.value)
    assert "500" in str(excinfo.value)


async def test_connection_error_is_mapped_to_runtimeerror() -> None:
    # A transport failure (e.g. the supervisor not up yet — the readiness-poll
    # case) must surface as the module's context-rich RuntimeError, not a raw
    # httpx error, so callers poll/handle a single error type.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = ProcessComposeClient(
        port=_PORT,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        await client.list_processes()
    message = str(excinfo.value)
    assert "list_processes" in message
    assert "/processes" in message
