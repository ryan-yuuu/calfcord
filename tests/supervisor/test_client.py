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


async def test_update_project_is_post_project_with_json_body() -> None:
    # The real v1.110.0 server rejects a raw-YAML body with HTTP 400
    # ("invalid character 'v' ...") — POST /project decodes JSON, exactly like the
    # `process-compose project update -f <yaml>` CLI does. So the client takes the
    # rendered YAML (the lifecycle's one body type) and ships it as a JSON object.
    import json

    seen, client = _json_client({})
    await client.update_project("version: '0.5'\nprocesses: {}\n")
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/project"
    assert seen[0].headers["content-type"] == "application/json"
    assert json.loads(seen[0].content) == {"version": "0.5", "processes": {}}


async def test_update_project_accepts_207_multi_status() -> None:
    # The real server answers a no-op reconcile with 207 Multi-Status + a
    # per-process map (e.g. {"broker": "error"} for an unchanged process). 207 is
    # a 2xx, so it must NOT raise — the priming reconcile depends on this.
    _, client = _record(httpx.Response(207, json={"broker": "error"}))
    assert await client.update_project("version: '0.5'\nprocesses: {}\n") == {
        "broker": "error"
    }


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


async def test_non_2xx_surfaces_status_code_structurally_and_appends_body() -> None:
    # Fix #9: callers (roster.agent_start) must branch on the status WITHOUT string
    # parsing — so the status is carried as a structural attribute. And PC's
    # informative response body must be surfaced (truncated), not dropped: the
    # PC_API_TOKEN rides a header, so the body leaks no secret.
    _, client = _record(httpx.Response(404, text="process newbie is not defined"))
    with pytest.raises(RuntimeError) as excinfo:
        await client.start_process("newbie")
    exc = excinfo.value
    # Structural status: a caller can branch 4xx-vs-5xx without parsing the message.
    assert getattr(exc, "status_code", None) == 404
    # The body is appended so the operator/LLM sees PC's reason.
    assert "process newbie is not defined" in str(exc)


async def test_non_2xx_truncates_an_overlong_body() -> None:
    # An adversarially large PC body must not flood the error; cap it at 500 chars.
    long_body = "x" * 5000
    _, client = _record(httpx.Response(500, text=long_body))
    with pytest.raises(RuntimeError) as excinfo:
        await client.stop_process("assistant")
    message = str(excinfo.value)
    assert getattr(excinfo.value, "status_code", None) == 500
    # The body is present but bounded (the 5000-char body cannot appear in full).
    assert "x" * 500 in message
    assert long_body not in message


async def test_connection_error_has_no_status_code() -> None:
    # A transport failure carries no HTTP status, so the structural attribute must
    # be None — a caller branching on 4xx/5xx treats "no status" as "not a 4xx".
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
    assert getattr(excinfo.value, "status_code", None) is None


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
