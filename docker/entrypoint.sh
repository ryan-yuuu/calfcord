#!/bin/sh
# Calfcord container entrypoint with optional Codex login gating.
#
# Behaviour is keyed off the ``CALFCORD_CODEX_LOGIN_ON_START`` env var:
#
#   unset / "0"  →  exec the command immediately (default; no behaviour
#                   change for non-codex services like bridge / router /
#                   tools).
#
#   "1"          →  before running the command, check whether Codex OAuth
#                   credentials are already cached. If they are (e.g. from
#                   a prior login, or from a mounted host credential
#                   directory), skip and continue. If they're not, run
#                   ``calfkit-auth codex login --device-code`` and BLOCK
#                   until the operator completes the device-code flow.
#                   The URL + user code are printed to the container's
#                   stderr — visible via ``docker compose logs -f
#                   <service>`` — and the operator opens the URL on any
#                   device (phone, laptop browser) and enters the code.
#                   Once the polling loop sees the code accepted,
#                   credentials persist to ``$HOME/.calfcord/auth/`` and
#                   the script exec's the original command.
#
# Why exec: replaces the shell with the actual long-running process so
# signals (SIGTERM from ``docker compose stop``, SIGINT from Ctrl-C)
# propagate directly to the Python worker without an intermediate sh
# swallowing them.

set -e

if [ "${CALFCORD_CODEX_LOGIN_ON_START:-0}" = "1" ]; then
    if calfkit-auth codex status >/dev/null 2>&1; then
        echo "==> Codex credentials present; skipping device-code login" >&2
    else
        echo "==> No Codex credentials cached." >&2
        echo "==> Starting device-code login. Open the printed URL on any device and enter the code." >&2
        echo "==> The container will block here until you complete the flow (timeout: 15 min)." >&2
        calfkit-auth codex login --device-code
        echo "==> Codex login complete; starting service" >&2
    fi
fi

exec "$@"
