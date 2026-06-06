# Discord setup

One-time, about 5 minutes. You'll create a Discord app, grab two values
(the bot token and application ID), enable two intents, and invite the bot to
your server. `calfcord init` takes it from there — it verifies the token, waits
for the invite, and **discovers your server and channel for you**, so these two
values are the only Discord IDs you ever paste.

**Before you start:** you need a Discord server you own (or have **Manage
Server** on).

## 1. Create the app

Grab two values to hand to `calfcord init` when it asks (it writes them to
`.env` for you):

1. Open the [Developer Portal](https://discord.com/developers/applications) →
   **New Application** → name it → **Create**.
2. On **General Information**, copy the **Application ID** — this is
   `DISCORD_APPLICATION_ID`.
3. Open the **Bot** tab → **Reset Token** → **Copy** — this is
   `DISCORD_BOT_TOKEN`. Treat it like a password; `init` verifies it on the spot
   when you paste it.

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

## 4. The wizard takes it from here

Discord setup is done — back in `calfcord init`, the wizard detects the moment
the bot joins, picks up your server and channel, brings your agent online, and
waits until it sees the first reply. When it finishes, confirm in a channel the
bot can see:

```
@assistant hello
```

A reply appears under the agent's persona. You're connected. (`@assistant` is the
default starter agent — use whatever name you gave yours in `init`.)

---

## Advanced: override what `init` auto-discovers

`calfcord init` discovers your server and channel automatically, so you don't
need these. They're here for cases where you want to set a value explicitly —
e.g. pin slash-command sync to one guild, or unlock owner-only commands. Turn on
**Developer Mode** (Discord → User Settings → Advanced), right-click to **Copy
ID**, and set the key in `~/.calfcord/config/.env`:

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
