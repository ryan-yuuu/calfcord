# Troubleshooting

Operational failure modes and how to resolve them. Each entry leads with the
**symptom** (the log line or error you'll actually see) so you can match it
quickly, then explains the cause and the fix.

- [Codex subscription auth](#codex-subscription-auth)
  - [`401 token_revoked` — agent stops mid-response](#401-token_revoked--agent-stops-mid-response)
  - [`CodexNotLoggedInError` at startup](#codexnotloggedinerror-at-startup)

For one-time Codex setup, see [codex-auth.md](./codex-auth.md).

---

## Codex subscription auth

### `401 token_revoked` — agent stops mid-response

**Symptom.** A Codex-backed agent (`provider: openai-codex`) is part-way
through a reply when the agent service logs a traceback ending in:

```
File ".../calfkit_organization/providers/codex/model_client.py", line 292, in _responses_create
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

**Symptom.** `docker compose up agent` (or runner bootstrap) fails immediately
with `CodexNotLoggedInError` before any Discord traffic is served.

**What it means.** An agent declares `provider: openai-codex` but no cached
credentials exist at `~/.calfcord/auth/openai_oauth.json`.

**Resolution.** Run the one-time login on the **host**, then retry:

```bash
uv run calfkit-auth codex login
uv run calfkit-auth codex status   # confirm
```

See [codex-auth.md](./codex-auth.md#one-time-setup-do-this-on-the-host-before-starting-any-container)
for the full setup, including the bind mounts that expose the host credential
file to containers.
