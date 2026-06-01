# Discord setup

One-time, about 5 minutes. You'll create a Discord app, copy two values into
`.env`, and invite the bot to your server.

**Before you start:** you need a Discord server you own (or have **Manage
Server** on).

## 1. Create the app

1. Open the [Developer Portal](https://discord.com/developers/applications) →
   **New Application** → name it → **Create**.
2. On **General Information**, copy the **Application ID** into `.env`:
   ```
   DISCORD_APPLICATION_ID=your-application-id
   ```
3. Open the **Bot** tab → **Reset Token** → **Copy** into `.env`. Treat it like
   a password and never commit `.env`:
   ```
   DISCORD_BOT_TOKEN=your-bot-token
   ```

## 2. Enable two intents

Still on the **Bot** tab, under **Privileged Gateway Intents**, switch on
**both** and click **Save Changes**:

- ✅ Message Content Intent
- ✅ Server Members Intent

> ⚠️ **Most-missed step.** Skip it and the bridge won't start — it exits with
> `PrivilegedIntentsRequired`.

## 3. Invite the bot

Replace `YOUR_APP_ID` with your Application ID, open the link in a browser,
pick your server, and click **Authorize**:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=292594732032
```

The link grants only the permissions calfcord needs. Invite it **only to
servers you trust** — agents can run code on the host (see
[`security.md`](./security.md)).

## 4. Start it and say hello

Launch calfcord (`docker compose up --build`, or see the
[README](../README.md#running)). Then, in a channel the bot can see:

```
@scribe hello
```

A reply appears under the agent's persona. You're connected.

---

## Optional `.env` values

Turn on **Developer Mode** (Discord → User Settings → Advanced), then
right-click to **Copy ID**:

| `.env` key | Copy ID from | What it does |
|---|---|---|
| `DISCORD_GUILD_ID` | your server | Slash commands appear instantly (otherwise ~1 h). |
| `DISCORD_OWNER_USER_ID` | yourself | Unlocks owner-only commands (`/clear`, `/thinking-effort`). |
| `DISCORD_DEFAULT_CHANNEL_ID` | a channel | Seeds the first agent's channel on boot. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bridge exits with `PrivilegedIntentsRequired` | Do step 2 — enable both intents. |
| Bot is online but never replies | Confirm Message Content intent (step 2); check it can **View Channel** + **Send Messages** in that channel. |
| Agent can't post / `Forbidden` on a webhook | Bot needs **Manage Webhooks** in that channel. |
| `/task` does nothing | Bot needs **Create Public Threads** in that channel — a channel override can block it even when the invite granted it. |
| "typing…" indicator never shows | The bot **user** needs **Send Messages** (and **Send Messages in Threads** for `/task` threads) in that channel — this is separate from Manage Webhooks, and a channel override can deny it. Typing is cosmetic, so it fails silently; the first denial is logged at WARNING. |
| Slash commands don't appear | Set `DISCORD_GUILD_ID` for instant sync (global takes ~1 h). |
