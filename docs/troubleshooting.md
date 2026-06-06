# Troubleshooting

Operational failure modes and how to resolve them. Each entry leads with the
**symptom** (the log line or error you'll actually see) so you can match it
quickly, then explains the cause and the fix.

- [Lifecycle & daemon](#lifecycle--daemon)
  - [Substrate is up but nothing replies in Discord](#substrate-is-up-but-nothing-replies-in-discord)
  - [`calfcord start` times out at the readiness gate](#calfcord-start-times-out-at-the-readiness-gate)
  - [`status` shows "process up, Discord disconnected"](#status-shows-process-up-discord-disconnected)
  - [Everything's green but the agent still doesn't reply](#everythings-green-but-the-agent-still-doesnt-reply)
  - [`status` says an agent's process is up, but it isn't registered](#status-says-an-agents-process-is-up-but-it-isnt-registered)
  - [Nothing survives a reboot](#nothing-survives-a-reboot)
  - [`agent start` refused: "already running in the organization"](#agent-start-refused-already-running-in-the-organization)
- [Codex subscription auth](#codex-subscription-auth)
  - [`401 token_revoked` — agent stops mid-response](#401-token_revoked--agent-stops-mid-response)
  - [`CodexNotLoggedInError` at startup](#codexnotloggedinerror-at-startup)

For the lifecycle model these entries assume (substrate vs. roster), see
[architecture.md](./architecture.md). For one-time Codex setup, see
[codex-auth.md](./codex-auth.md).

---

## Lifecycle & daemon

calfcord runs as two layers: a **substrate** (broker + bridge) that `calfcord
start` brings up in the background, and a **roster** (agents, tools, router,
mcp) that you clock in on demand with `calfcord agent start <name>` and friends.
Most "it's running but quiet" reports come from confusing the two — the office
is open, but no teammate has clocked in yet. The entries below are ordered the
way you'll meet them: from "I started the substrate and expected a reply" down
to multi-host drift.

Your first two tools for any of these are `calfcord status` (the glanceable org
board — substrate + roster health) and `calfcord logs [component] [-f]` (per-
component supervisor logs, also on disk at `$CALFCORD_HOME/state/logs/<name>.log`).
When `status` looks green but behavior is still wrong, escalate to `calfcord
doctor`, which is the only check that probes the broker + agents end-to-end.

### Substrate is up but nothing replies in Discord

**Symptom.** `calfcord start` printed its success banner, `calfcord status`
shows the broker and bridge healthy, the bot shows **online** in Discord — but
`@assistant hello` gets no answer. `calfcord status` shows an **empty roster**
(no agents online).

**What it means.** This is working as designed, not a fault. `calfcord start`
brings up **only the substrate** (broker + bridge). It deliberately does *not*
auto-start any agent — "nothing runs that you didn't start" is a trust property.
The bot is online because the *bridge* is connected to Discord, but no teammate
has clocked in to actually answer.

**Resolution.** Clock an agent in:

```bash
calfcord agent start assistant   # use any name from `calfcord agent list`
calfcord status                  # the agent now shows under the roster
```

Then `@assistant hello` will get a reply. `start`'s own success banner names
this next step for you (`→ calfcord agent start assistant`); if you arrived here
by reopening the workspace, that banner is the prompt you skipped.

> `calfcord agent list` shows agents **defined** on disk; `calfcord agent ps`
> shows agents **running** right now. An empty `agent ps` with a non-empty
> `agent list` is exactly this case: defined, but not clocked in.

### `calfcord start` times out at the readiness gate

**Symptom.** `calfcord start` hangs for a while, then exits non-zero with a
readiness/health-gate timeout — the **bridge never reached healthy**. It tears
the substrate back down rather than leaving a half-open office.

**What it means.** `start` is health-gated: it polls until the **bridge**
heartbeat reports healthy before declaring success. The bridge writes its first
healthy beat only once it is **connected to Discord** (`on_ready`), so the gate
times out whenever the bridge can connect a process but never completes the
Discord handshake.

**Most common cause — privileged intents are off.** If the bridge can reach
Discord but the gateway never finishes coming up, the usual culprit is that the
bot's **privileged gateway intents are disabled**. In the Discord Developer
Portal → your app → **Bot**, enable **Message Content Intent** and **Server
Members Intent**, then re-run `calfcord start`.

**Other causes to rule out, in order:**

```bash
calfcord doctor                  # checks config, broker reachability, token + app id
```

- **Bad / revoked bot token or wrong app id** — `doctor` flags both. Fix in
  `.env` (or re-run `calfcord init`'s Discord step), then `calfcord start`.
- **Broker not reachable.** The gate treats broker TCP as a fast-fail
  precondition; `doctor` reports it. If the broker can't be reached, fix
  `CALF_HOST_URL` / the broker first.
- **Network egress blocked to Discord** (corporate proxy / firewall). The
  process is up but the gateway handshake never completes — same timeout.

`calfcord logs bridge -f` while you retry shows exactly where the bridge stalls
(token rejected vs. intents-gated vs. no network).

### `status` shows "process up, Discord disconnected"

**Symptom.** `calfcord status` shows the bridge process **up** but flags it as
**Discord disconnected** (not "healthy"). The bot may appear offline in Discord,
or appear online but answer nothing.

**What it means.** Heartbeat health tracks the **Discord connection state**, not
just process liveness. The bridge marks itself healthy only while its Discord
client reports *connected*; if the gateway drops (revoked token, network blip,
Discord-side disconnect) the process keeps running but the heartbeat correctly
reports unhealthy. This is the distinction a bare "is the process alive?" check
would miss — a silent-but-running bridge.

**Resolution.**

- **Transient drop** (network blip, brief Discord outage): the bridge
  reconnects on its own and `status` returns to healthy. Watch it with
  `calfcord logs bridge -f`.
- **Won't recover** (token revoked / invalidated): the connection can't come
  back on its own. Run `calfcord doctor` to confirm the token/app-id, fix the
  credential, then restart the substrate:

  ```bash
  calfcord stop
  calfcord start
  ```

A **healthy** bridge in `status` means "process up *and* connected to Discord" —
so treat "Discord disconnected" as the bot being effectively offline even though
its PID is alive.

### Everything's green but the agent still doesn't reply

**Symptom.** `calfcord status` is all green — broker healthy, bridge connected,
the agent shows online in the roster — yet `@assistant hello` still gets no
reply.

**What it means.** `status` is cheap and glanceable: it reads per-component
**heartbeats**, which prove each process is alive and (for the bridge) connected
to Discord. It does *not* prove the pieces actually talk to each other over the
broker. A green board can still hide a broken path — wrong broker for one
component, a topic that isn't flowing, an agent wedged after boot.

**Resolution — escalate to the deep probe:**

```bash
calfcord doctor
```

`doctor` is the authoritative check: beyond the static config checks it runs an
**end-to-end control-plane probe** — it publishes a discovery ping over the
broker and waits for live agents to answer. A non-empty result proves the
broker + bridge + agents function together, end to end; an empty or partial
result pinpoints the broken hop that `status` can't see. This deep probe is the
*only* check that can diagnose the "green but no replies" symptom — `status`
structurally cannot.

If `doctor` shows the agent not answering the probe, see the next entry (drift)
and `calfcord logs <agent> -f`.

### `status` says an agent's process is up, but it isn't registered

**Symptom.** `calfcord agent ps` (or `status`) flags an agent as **"process up
but not registered"** / wedged — the supervisor shows the process running
locally, but the agent never joined the live org and doesn't answer.

**What it means.** `agent ps` reconciles two independent views:

- the **physical** view — the local supervisor's process list (this host only);
- the **logical** view — the live roster reconstructed over the broker (a
  discovery ping; only agents that actually answer appear).

When a process is up *physically* but absent from the *logical* roster, the
agent booted but never successfully registered/connected to the broker — it's
running but not participating. That's drift, and it's why a bare process check
would lie.

**Resolution.**

```bash
calfcord logs <agent> -f          # find why it never registered (broker URL? auth? crash loop?)
calfcord agent restart <agent>    # reload it after fixing config / .md
```

Common roots: the agent points at the wrong broker (`CALF_HOST_URL` mismatch
between hosts), a crash loop right after boot, or broker auth that the agent
can't satisfy. Fix the cause, then `agent restart` to re-join. If you edited the
agent's `.md`, `agent restart <agent>` is also how you reload it.

> "Answering but not local" is **not** drift — on a multi-host setup it's
> expected for an agent to be live in the org while running on another box. See
> [the cross-host guard](#agent-start-refused-already-running-in-the-organization)
> and [distributed-deployment.md](./distributed-deployment.md).

### Nothing survives a reboot

**Symptom.** After a machine reboot (or logging out), `calfcord status` shows
everything down; the bot is offline and no agents are running. `calfcord start`
brings it all back.

**What it means.** This is expected. The substrate daemon is **session-scoped**,
not a system service — `calfcord start` launches it in the background of your
session, and `init` doesn't install anything that survives a reboot. There is no
hidden persistence; reboot non-survival is honest, and `status` reflects reality
(nothing is running).

**Resolution.**

- **Occasional / dev use:** just re-run `calfcord start`, then clock your agents
  back in with `calfcord agent start <name>`.
- **Always-on / production:** graduate to a system-managed unit so the substrate
  comes back automatically on boot. Generate a manifest:

  ```bash
  calfcord deploy systemd            # render a systemd unit to stdout
  calfcord deploy systemd -o calfcord.service   # ...or to a file
  ```

  `calfcord deploy` also renders `k8s` and `docker` manifests. This is the
  Altitude-3 / production path — see
  [distributed-deployment.md](./distributed-deployment.md).

### `agent start` refused: "already running in the organization"

**Symptom.** `calfcord agent start X` refuses to start and prints that agent
`X` is **already running in the organization** — even though `X` isn't running
on *this* host.

**What it means.** This is the **cross-host duplicate guard**, and it fired
correctly. Before starting a teammate, `agent start` runs a broker-wide
discovery probe and refuses if an agent with that name is already live
**anywhere in the org — including another host**. Two same-named agents would
both reply (double-reply) and split agent-to-agent RPC, so the guard prevents
it. The check is broker-wide on purpose: it's distributed-correct, catching a
duplicate on a second host without the bridge having to reject anything.

**Resolution.** Decide where the agent should actually run.

- **It should run here, not there:** stop it on the other host first
  (`calfcord agent stop X` on that host), then `calfcord agent start X` here.
- **It's already running where you want it:** nothing to do — it's live. Confirm
  with `calfcord agent ps` (the logical view is org-wide, so it lists the agent
  even though it runs on another box).
- **You genuinely want a second instance:** you can't share one agent *name*
  across hosts — give the second one a distinct name (`calfcord agent rename` /
  create a new agent) so each has its own identity in the org.

> **Known limitation.** The probe is point-in-time, so two *simultaneous*
> `agent start X` on different hosts could both see nothing and both start (a
> TOCTOU race). The guard covers the common case — X is already running and you
> try to start another. For the design rationale see
> [architecture.md](./architecture.md).

---

## Codex subscription auth

### `401 token_revoked` — agent stops mid-response

**Symptom.** A Codex-backed agent (`provider: openai-codex`) is part-way
through a reply when the agent service logs a traceback ending in:

```
File ".../calfcord/providers/codex/model_client.py", line 292, in _responses_create
    stream_obj = await super()._responses_create(
File ".../calfkit/_vendor/pydantic_ai/models/openai.py", line 1580, in _responses_create
    raise ModelHTTPError(status_code=status_code, model_name=self.model_name, body=e.body) from e
calfkit._vendor.pydantic_ai.exceptions.ModelHTTPError: status_code: 401, model_name: gpt-5.5,
body: {'message': 'Encountered invalidated oauth token for user, failing request',
       'type': None, 'code': 'token_revoked', 'param': None}
```

The response is aborted (no reply is posted) and the same error repeats on
every subsequent turn.

**What it means.** The OAuth **access token was invalidated server-side by
OpenAI**, but the local credential store still considered it valid, so the
client kept sending the dead token. `token_revoked` is a *revocation*, not a
clock expiry — a clock expiry would have been refreshed locally before the
request ever went out.

**Root cause.** Token refresh in the Codex provider is driven **only by local
clock expiry**, with no reaction to a server-side rejection:

1. `_CodexBearerAuth.async_auth_flow` runs on every outgoing request and calls
   OpenHands' `OpenAISubscriptionAuth.refresh_if_needed()`
   (`providers/codex/model_client.py`).
2. `refresh_if_needed()` is a **no-op unless the token is clock-expired** — it
   returns the cached token whenever `OAuthCredentials.is_expired()` is `False`
   (i.e. the stored `expires_at` is more than 60 seconds away). It never checks
   whether the token is still *valid*, only whether it has *expired*.
3. The auth flow yields the request and **never inspects the response**, so a
   `401` does not trigger a refresh-and-retry.

Net effect: once the token is revoked while still inside its clock window,
every request re-sends the dead bearer and fails with `401 token_revoked`. The
error surfaces as an uncaught `ModelHTTPError` — nothing in the Codex path
catches it — so the agent's response is aborted instead of recovered.

> The retry-with-feedback machinery in `bridge/outbox.py` only handles
> Discord-side HTTP errors on the *reply-send* path. It does not cover
> model-request failures like this one.

**Common triggers** (anything that revokes the grant server-side while the
service holds a still-"fresh" token):

- **Refresh-token rotation collision** (most common). OpenAI rotates refresh
  tokens; minting a new one invalidates the previously issued access + refresh
  pair. This happens if the *same ChatGPT account's* credentials are touched by
  anything else while the service is running:
  - the official `codex` CLI signed in to the same account,
  - a second service replica / a local dev run sharing the same
    `CALFCORD_AUTH_DIR` (`~/.calfcord/auth`),
  - a manual `calfkit-auth codex login --force` or `... refresh` against the
    shared credential file.
- **Operator revoked the app** in ChatGPT settings, or signed out everywhere.
- **OpenAI-side session invalidation** (password change, security event).

**Resolution.** Because the grant is revoked, the *refresh token is dead too* —
`calfkit-auth codex refresh` will **not** help. You must re-login:

```bash
# On the HOST (the source of truth for the credential file):
uv run calfkit-auth codex login --force

# Verify a fresh token was minted:
uv run calfkit-auth codex status
# → Logged in. ChatGPT account id: acct_...
#   Access token expires: <a time well in the future>
```

For containerized agents, the credential file is bind-mounted from the host
(see [codex-auth.md](./codex-auth.md#containerized-agents)), so re-running
`login --force` on the host fixes the running container without a rebuild. The
next request picks up the new token via the bind mount; restart the agent if it
has cached a failing state.

**Confirm the diagnosis.** The error body's `code` distinguishes the case:

| `code`              | Meaning                                  | Fix                              |
|---------------------|------------------------------------------|----------------------------------|
| `token_revoked`     | Grant invalidated server-side            | `login --force` (re-auth)        |
| *(expiry-related)*  | Token simply expired                     | usually self-heals on next request; else `refresh` |

If `status` shows the access token *not* expired but requests still 401 with
`token_revoked`, that is exactly this failure mode.

**Prevention.**

- **Single writer per account.** Don't run the official `codex` CLI, a second
  replica, or a local dev instance against the same ChatGPT account / same
  `~/.calfcord/auth` file while the service is live. Concurrent use of one grant
  is the most common way to trigger rotation revocation.
- After any deliberate `login --force` / `refresh` on a shared credential file,
  expect already-running consumers holding the old token to start failing until
  their next successful refresh — restart them or re-issue from a single owner.

> **Engineering note.** This is a known structural gap: the auth layer refreshes
> proactively (clock-based) but has no *reactive* path — it neither retries on a
> `401` nor surfaces an actionable "re-login required" signal. The module
> docstrings (`providers/codex/__init__.py`, `model_client.py`) and the unused
> `credentials_to_authlib_token` / `make_persist_callback` helpers in
> `token_store.py` still describe an earlier authlib-based design that *did*
> refresh-on-401; that approach was replaced by the `httpx.Auth` hook without
> re-adding the reactive behavior. The `REFRESH_LEEWAY_SECONDS = 300` constant in
> `model_client.py` is also dead — the live refresh trigger is OpenHands'
> 60-second `is_expired()` buffer, not 5 minutes. Hardening options (reactive
> refresh-and-retry; mapping auth 401s to an operator-facing message) are
> tracked separately.

---

### `CodexNotLoggedInError` at startup

**Symptom.** Bringing a Codex-backed agent online fails immediately with
`CodexNotLoggedInError`, before any Discord traffic is served — whether you
clocked it in natively (`calfcord agent start <name>`) or via a container
(`docker compose up agent`).

**What it means.** An agent declares `provider: openai-codex` but no cached
credentials exist at `~/.calfcord/auth/openai_oauth.json`.

**Resolution.** Run the one-time login on the **host**, then retry:

```bash
uv run calfkit-auth codex login
uv run calfkit-auth codex status   # confirm
```

Then bring the agent online again:

```bash
# Native (single-host) — clock the teammate back in:
calfcord agent start <name>        # or `calfcord agent restart <name>` if it's stuck

# Containerized — the credential file is bind-mounted from the host:
docker compose up agent
```

See [codex-auth.md](./codex-auth.md#one-time-setup-do-this-on-the-host-before-starting-any-container)
for the full setup, including the bind mounts that expose the host credential
file to containers.
