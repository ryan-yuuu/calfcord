# Codex subscription auth

The `openai-codex` provider lets calfcord agents call OpenAI's Codex models (`gpt-5.2-codex`, `gpt-5.3-codex`, etc.) through the same OAuth flow the official `codex` CLI uses, so requests are billed against an active **ChatGPT Plus or Pro subscription** rather than against OpenAI API credits.

This page covers the one-time setup required before an agent with `provider: openai-codex` can boot.

## Prerequisites

- An active ChatGPT Plus or Pro subscription
- Local browser access (for the one-time OAuth login)
- Internet egress from the host to:
  - `auth.openai.com` (OAuth flow)
  - `chatgpt.com/backend-api/codex` (inference)
  - `raw.githubusercontent.com/openai/codex` (live-fetched Codex CLI system prompts)

## One-time setup (do this on the HOST, before starting any container)

```bash
# 1. Authenticate. Opens a browser; sign in with your ChatGPT account.
uv run calfkit-auth codex login

# 2. (Optional but recommended) pre-fetch the upstream Codex system
#    prompts so the first container boot doesn't have to.
uv run calfkit-auth codex refresh-prompts

# 3. Verify
uv run calfkit-auth codex status
# → Logged in. ChatGPT account id: acct_...
#   Access token expires: 2026-05-28T01:23:45+00:00
```

Both commands persist data under `~/.calfcord/` on your host:

- `~/.calfcord/auth/openai_oauth.json` — OAuth tokens (0600 perms)
- `~/.calfcord/codex_prompts/` — cached `models.json` + `prompt.md` (ETag-conditional refresh)

The host CLI is the source of truth for both. Token rotation that happens later inside any container writes back through the bind mount to these same files, so the host CLI always sees the latest state.

## Containerized agents

`docker-compose.override.yml` bind-mounts those two host dirs into the agent container, so once you've completed the host login above, `docker compose up agent` works:

```yaml
agent:
  volumes:
    - ${HOME}/.calfcord/auth:/home/calfcord/.calfcord/auth
    - ${HOME}/.calfcord/codex_prompts:/home/calfcord/.calfcord/codex_prompts
```

If `docker compose up agent` reports `CodexNotLoggedInError`, it means the host hasn't been logged in yet — run `uv run calfkit-auth codex login` on the host, then retry.

## Declaring a Codex-backed agent

In `agents/<name>.md`:

```markdown
---
name: codex_demo
slash: /codex
display_name: Codex
description: Demonstration agent backed by ChatGPT subscription.
provider: openai-codex
model: gpt-5.2-codex
thinking_effort: medium
---

You are a helpful coding assistant.
```

Supported model names are whatever upstream `openai/codex` lists in [`codex-rs/models-manager/models.json`](https://github.com/openai/codex/blob/main/codex-rs/models-manager/models.json). Currently:

| Model | Resolves to |
|---|---|
| `gpt-5.2-codex` | longest-prefix match → `gpt-5.2` system prompt |
| `gpt-5.2` | exact match |
| `gpt-5.3-codex` | exact match |
| `gpt-5.1-codex-max`, `gpt-5.1-codex-mini` | falls back to bundled `prompt.md` |
| Anything else | falls back to bundled `prompt.md`; if the model doesn't exist server-side, Codex returns a 4xx |

No allowlist is enforced calfcord-side — we trust upstream and forward the model name as-is.

## Maintenance commands

All can be run on the host:

```bash
uv run calfkit-auth codex login [--device-code] [--no-browser] [--force]
uv run calfkit-auth codex logout
uv run calfkit-auth codex status
uv run calfkit-auth codex refresh           # force a token refresh now
uv run calfkit-auth codex refresh-prompts   # re-fetch upstream Codex prompts
uv run calfkit-auth codex prompt-status
uv run calfkit-auth codex clear-prompts
```

If you prefer device-code flow (no browser on the host — useful for SSH into a build server), pass `--device-code` to `login`. You'll get a URL and a code; open the URL on any device with a browser, enter the code, and the polling loop on the host completes the flow.

## What's actually happening on the wire

When a Codex-backed agent serves a request:

1. **Auth**: a fresh OAuth access token is injected per-request by [authlib](https://docs.authlib.org/)'s `AsyncOAuth2Client`, which auto-refreshes ~5 minutes before expiry and writes the new tokens back through the host-side credential file.
2. **Endpoint**: requests go to `https://chatgpt.com/backend-api/codex/responses` (the Codex CLI's backend), not the standard `api.openai.com`.
3. **Headers**: `originator: codex_cli_rs`, `chatgpt-account-id: <decoded from JWT>`, `OpenAI-Beta: responses=experimental` — same fingerprint the official Codex CLI produces.
4. **System prompt**: the verbatim official Codex CLI prompt for the requested model (fetched live from openai/codex with ETag caching) is sent as `instructions`. Your agent's own `system_prompt` body is smuggled into a leading synthetic user message as `input_text`. Required because the Codex backend explicitly validates `instructions` against a whitelist of Codex CLI prompts ([openai/codex#4433](https://github.com/openai/codex/issues/4433)).
5. **Body constraints**: `store: false`, `stream: true`, no `temperature`, no `max_tokens` — Codex backend rejects them.

## ToS and rate-limit notes

- ChatGPT subscription auth via this OAuth client is the same mechanism the official `codex` CLI uses and the same one [several other major open-source coding agents](https://github.com/search?q=app_EMoamEEZ73f0CkXaXp7hrann&type=code) use (Zed, Cline, Goose, etc.). OpenAI has not published explicit terms for third-party reuse, but the validator-side checks make tolerated use clearly distinguishable from disruptive abuse.
- Usage counts against your ChatGPT subscription's weekly Codex limits, not against API per-minute rate limits.
- The Codex endpoint is geo-fenced; expect "Workspace is not authorized in this region" errors from certain regions.
