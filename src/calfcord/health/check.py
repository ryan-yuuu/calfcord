"""Readiness logic for the ``disco _healthcheck <component>`` exec probe.

Process Compose runs this probe ON THE AGENT/TOOLS HOSTS to gate readiness
(design §12.1 / §13.2), so this module must stay **light**: a readiness probe
carries no secrets and no heavy broker deps at import time. The
broker-reachability default lazy-imports its admin client *inside* the function
that needs it, keeping ``import calfcord.health.check`` pure-filesystem.

Only **two** components carry a readiness signal, so only two are probeable
(§12.1):

* ``"broker"`` → reachability is **metadata/admin** reachability, not bare TCP:
  Tansu is no-auto-create, so a bound port does not mean the broker can serve. The
  probe is an injected async callable (a stub in unit tests, a real metadata fetch
  in production) returning ``True`` when the broker answers.
* ``"bridge"`` → a **fresh heartbeat** must exist. The bridge refreshes its beat
  every few seconds and writes the first one only once Discord is connected (see
  ``bridge/gateway.py``'s ``_on_ready``), so a fresh bridge beat means
  "Discord-connected"; a stale or missing beat means "not ready".

The roster runners (agents, ``tools``) deliberately have NO
readiness signal: they run via ``run_worker_until_signal`` and never beat, and
Process Compose attaches a readiness probe only to the broker and the bridge (see
``supervisor/compose.py``). Probing any other component is therefore a
programming/config bug, so it RAISES rather than fabricating a "not ready" verdict
for a signal that does not exist (a fabricated verdict would lie about readiness).

The two probeable paths return a POSIX exit code — ``0`` healthy, ``1`` not — so
the caller can ``sys.exit`` it directly.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from calfcord.health.heartbeat import is_fresh, read_beat

_DEFAULT_TTL_SECONDS = 10

# A metadata round-trip on a healthy local broker returns in well under a second;
# a generous-but-bounded cap turns a hung/dead broker into a "not ready" verdict
# instead of a probe that never exits (Process Compose would treat that as a
# failure anyway, but a clean bool keeps the exit code well-defined).
_METADATA_TIMEOUT_MS = 5000

BrokerProbe = Callable[[], Awaitable[bool]]
AdminFactory = Callable[..., Any]


async def healthcheck(
    home: str | os.PathLike[str],
    component: str,
    *,
    now: datetime,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    broker_probe: BrokerProbe,
) -> int:
    """Return ``0`` if ``component`` is healthy, ``1`` if not (a POSIX exit code).

    Only ``"broker"`` and ``"bridge"`` are probeable — they are the sole components
    that carry a readiness signal (see the module docstring). For ``"broker"`` the
    verdict is metadata reachability via the injected ``broker_probe`` (why not bare
    TCP: module docstring). For ``"bridge"`` the verdict is a fresh heartbeat under
    ``<home>/state/health/`` — ``now`` and ``ttl_seconds`` are injected so freshness
    is deterministic and pinned to the bridge's refresh interval (§12.1).

    Any other component raises :class:`RuntimeError`: the roster runners never beat,
    so there is nothing to read, and a readiness probe must not invent a verdict for
    a signal that does not exist (it must not lie). The raise names the offending
    component so the misconfiguration is obvious in the exec probe's output.
    """
    if component == "broker":
        return 0 if await broker_probe() else 1

    if component == "bridge":
        beat = read_beat(home, component)
        if beat is not None and is_fresh(beat, now=now, ttl_seconds=ttl_seconds):
            return 0
        return 1

    raise RuntimeError(
        f"no readiness signal for component {component!r}: only 'broker' and 'bridge' "
        "are probeable (the roster runners run via run_worker_until_signal and never "
        "write a heartbeat)"
    )


def default_broker_probe(
    server_urls: str, *, admin_factory: AdminFactory | None = None
) -> BrokerProbe:
    """Build the production broker-reachability probe for ``server_urls``.

    The returned coroutine connects an admin client, fetches cluster metadata
    (``list_topics``), and reports whether the broker actually *served* it — this
    is the §12.1 contract: on Tansu (no-auto-create) a bound port does not mean
    the broker can answer, so we probe metadata, not bare TCP. Any failure
    (connect refused, metadata timeout, auth error) returns ``False`` rather than
    raising, because a readiness probe must report "not ready", never crash.

    ``aiokafka``'s admin client is **lazy-imported inside the coroutine** (via the
    default factory) so importing this module stays pure-filesystem and free of
    heavy broker deps — it runs as a Process Compose exec probe on the agent/tools
    hosts (see the module docstring). ``admin_factory`` is an injection seam for
    tests; production passes ``None`` and gets the real client.
    """

    def _make_admin(**kwargs: Any) -> Any:
        # Lazy import: the admin client (and aiokafka) must not load at module
        # import time — only when a broker probe actually runs.
        from aiokafka.admin import AIOKafkaAdminClient

        return AIOKafkaAdminClient(**kwargs)

    factory = admin_factory if admin_factory is not None else _make_admin

    async def _probe() -> bool:
        admin = None
        try:
            # Construct INSIDE the try so even a constructor failure (e.g. a
            # malformed bootstrap string) degrades to "not ready" per the
            # never-raises contract, rather than escaping the exec probe.
            admin = factory(
                bootstrap_servers=server_urls,
                request_timeout_ms=_METADATA_TIMEOUT_MS,
            )
            await admin.start()
            await admin.list_topics()
            return True
        except Exception:
            # Any connection / metadata failure means "not ready"; the probe never
            # raises so the exec probe gets a clean bool (→ exit code).
            return False
        finally:
            # Always release the admin connection (when one was built), even when
            # start/metadata raised; a leaked socket would outlive the probe.
            if admin is not None:
                with contextlib.suppress(Exception):
                    await admin.close()

    return _probe
