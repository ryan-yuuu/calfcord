"""The single seam that knows Process Compose's REST routes (design §13.2).

Everything calfcord does to a running supervisor — list/state/logs, start/stop/
restart a roster member, push an updated project — goes through this thin async
wrapper. Confining the wire contract to one module is deliberate (design §12.4,
Risk #2): Process Compose's API is version-fragile and the published docs are
partly wrong, so a binary upgrade is a one-file change here, and the CLI veneer
above stays the stable surface.

The routes are pinned to Process Compose ``v1.110.0`` from the Phase-0 spike,
and several correct an upstream doc error:

* single-process state is ``GET /process/{name}`` — ``GET /process/{name}/state``
  is a 404, so it is intentionally absent;
* **stop is a ``PATCH``** (``PATCH /process/stop/{name}``), not a ``POST`` —
  start and restart are ``POST``;
* logs carry the window in the path: ``GET /process/logs/{name}/{end}/{limit}``.

An optional ``PC_API_TOKEN`` rides only in the ``X-PC-Token-Key`` header. Every
call is an infrastructure call against the local supervisor, so a non-2xx is a
genuine bug (a missing process / a wedged server), not something an LLM can
adapt to — per the CLAUDE.md convention these ``raise RuntimeError`` with the
caller, route, and status rather than returning an ``"error: ..."`` string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

# The supervisor is detached but local; calls should be near-instant. The
# timeout exists only so a wedged server fails loudly instead of hanging the CLI.
_HTTP_TIMEOUT = 10.0

# Process Compose's optional API auth (design §13.2): a >=20ch shared key sent
# in this header. Confined here so no other module reconstructs the scheme.
_TOKEN_HEADER = "X-PC-Token-Key"


class ProcessComposeClient:
    """Async wrapper over the Process Compose REST API on ``http://localhost:{port}``.

    ``port`` defaults to ``8080`` (the supervisor default); calfcord derives a
    per-home port and passes it both to ``process-compose up -p`` and here so a
    second ``$CALFCORD_HOME`` does not collide. ``base_url`` overrides ``port``
    outright for non-default hosts/ports. ``token`` is the optional
    ``PC_API_TOKEN`` and ``client_factory`` injects an ``httpx.AsyncClient`` for
    tests — mirroring the ``client_factory`` seam in ``cli/doctor.py`` and the
    Codex prompt resolver, so unit tests stub the transport and never touch a
    real supervisor.
    """

    def __init__(
        self,
        *,
        port: int = 8080,
        base_url: str | None = None,
        token: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._base_url = (base_url or f"http://localhost:{port}").rstrip("/")
        self._token = token
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)

    def _headers(self) -> dict[str, str] | None:
        # Only send the auth header when a token is configured, so an unauthed
        # supervisor (the common dev case) sees a clean request.
        return {_TOKEN_HEADER: self._token} if self._token else None

    async def _request(
        self, *, caller: str, method: str, route: str, content: str | None = None
    ) -> httpx.Response:
        """Issue one request and raise a context-rich ``RuntimeError`` on non-2xx.

        Each call opens its own client via the factory (the resolver pattern) so
        the wrapper holds no long-lived connection — the supervisor is local and
        calls are sparse, so a per-call client keeps lifetime management trivial.
        """
        async with self._client_factory() as client:
            try:
                response = await client.request(
                    method,
                    f"{self._base_url}{route}",
                    headers=self._headers(),
                    content=content,
                )
            except httpx.RequestError as exc:
                # Transport failure (server not up yet / wedged / wrong port) — an
                # infra failure, so normalize to the same RuntimeError contract as
                # a non-2xx, letting callers (e.g. the start readiness poll) handle
                # one error type instead of leaking httpx.
                raise RuntimeError(
                    f"{caller}: process-compose {method} {route} failed to connect: {exc}"
                ) from exc
        if not response.is_success:
            raise RuntimeError(
                f"{caller}: process-compose {method} {route} "
                f"failed with HTTP {response.status_code}"
            )
        return response

    async def list_processes(self) -> Any:
        """All declared processes and their states (``GET /processes``)."""
        response = await self._request(
            caller="list_processes", method="GET", route="/processes"
        )
        return response.json()

    async def get_process(self, name: str) -> Any:
        """One process's state (``GET /process/{name}``).

        The state object carries ``status``, ``pid``, ``is_ready``,
        ``is_running``, and ``restarts``. Note the bare path — the documented
        ``/process/{name}/state`` is a 404 in v1.110.0.
        """
        response = await self._request(
            caller="get_process", method="GET", route=f"/process/{name}"
        )
        return response.json()

    async def get_process_info(self, name: str) -> Any:
        """One process's *config* as declared in the project (``GET /process/info/{name}``)."""
        response = await self._request(
            caller="get_process_info", method="GET", route=f"/process/info/{name}"
        )
        return response.json()

    async def project_state(self) -> Any:
        """The project-wide state (``GET /project/state``)."""
        response = await self._request(
            caller="project_state", method="GET", route="/project/state"
        )
        return response.json()

    async def start_process(self, name: str) -> Any:
        """Start a process — a disabled roster slot clocking in (``POST /process/start/{name}``)."""
        response = await self._request(
            caller="start_process", method="POST", route=f"/process/start/{name}"
        )
        return response.json()

    async def stop_process(self, name: str) -> Any:
        """Stop a process (``PATCH /process/stop/{name}``).

        Stop is a ``PATCH``, not a ``POST`` — the published docs are wrong here,
        and getting the method wrong silently fails the lifecycle, so it is
        pinned by test.
        """
        response = await self._request(
            caller="stop_process", method="PATCH", route=f"/process/stop/{name}"
        )
        return response.json()

    async def restart_process(self, name: str) -> Any:
        """Restart a process (``POST /process/restart/{name}``)."""
        response = await self._request(
            caller="restart_process", method="POST", route=f"/process/restart/{name}"
        )
        return response.json()

    async def get_logs(self, name: str, end_offset: int, limit: int) -> Any:
        """A bounded log window for one process (``GET /process/logs/{name}/{end}/{limit}``).

        The window is encoded in the path (not query params) in v1.110.0.
        """
        response = await self._request(
            caller="get_logs",
            method="GET",
            route=f"/process/logs/{name}/{end_offset}/{limit}",
        )
        return response.json()

    async def update_project(self, yaml_text: str) -> Any:
        """Apply an updated project to the running supervisor (``POST /project``).

        Powers the dynamic-add path (an agent authored after ``start``). The
        body is the rendered project YAML; the supervisor reconciles it without
        bouncing unchanged processes in steady state (the once-only first-update
        bounce, upstream #494, is handled by the caller's priming reconcile).
        """
        response = await self._request(
            caller="update_project", method="POST", route="/project", content=yaml_text
        )
        return response.json()
