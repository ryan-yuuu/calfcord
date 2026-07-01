"""Liveness heartbeats for the substrate/roster runners (design §4.2 / §12.1).

Each long-lived calfcord process refreshes a small JSON heartbeat under
``$CALFCORD_HOME/state/health/<component>.json``; the ``disco _healthcheck``
exec probe Process Compose runs on the *agent/tools hosts* reads it to decide
readiness. This package is therefore deliberately light and dependency-free at
its core: :mod:`calfcord.health.heartbeat` is pure filesystem with no broker and
no config, so it can be imported on a host that has no shared filesystem — a
readiness probe must not carry secrets or heavy deps.
"""
