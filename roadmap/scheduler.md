# Scheduler — Durable Reminders & Scheduled Tasks

**Status:** In design — core architecture agreed; one blocking decision open (the user write-access model).

## Goal

A durable reminder / cron capability for the bot: a user (or agent) can schedule a
one-shot reminder ("in 10h") or a recurring task, and it fires at the right time and
**survives bot restarts**.

## Guiding principle: Discord *is* the source of truth

Rather than a private database, scheduled tasks live in Discord itself, so the schedule is a
single, visible artifact that both **users** (who can see and refer to it) and the **app**
(which reads it and fires the tasks) treat as canonical. This shared-source-of-truth goal —
not dependency avoidance — is the primary design driver.

## Why the app owns the timer (not Discord)

Discord has no general-purpose timer/alarm. The only native time-trigger is **Guild Scheduled
Events** (an `EXTERNAL` event auto-transitions to `ACTIVE` at its start time and emits a
`GUILD_SCHEDULED_EVENT_UPDATE` gateway event). It is too constrained to be the primitive:
capped at 100 events/guild, a `DAILY` recurrence floor, user-visible in the Events tab, and
only approximate timing. So Discord is storage + change-notification; the app owns timing.

## Architecture (direction)

- **Store** — a dedicated channel; one message per task; the message snowflake is the primary
  key. CRUD maps to post / edit / delete.
- **Change bus** — the gateway delivers `MESSAGE_CREATE` / `MESSAGE_UPDATE` / `MESSAGE_DELETE`,
  so the app stays in sync with the truth in real time instead of polling. A boot-time history
  scan is the recovery backstop for events missed while offline.
- **In-app scheduler** — load pending tasks into an in-memory schedule at boot, then fire via
  in-memory timers (timer-per-item with a max-sleep cap). Discord is never the timer.
- **Idempotency** — mark a task "fired" in its own message (edit / reaction) as the commit
  point, and skip already-marked tasks on boot. Default to at-least-once + a recently-fired
  dedup set (a late duplicate beats a silent drop).
- **Catch-up** — tasks overdue at boot fire immediately; the policy for stale tasks is TBD
  (flag-as-missed vs. silent vs. skip-if-older-than-X).
- **Recurrence** — store the next-fire time + a recurrence descriptor. Interval-component
  fields (seconds / days / months) with calendar-correct, fast-forward advancement stays
  dependency-free; full cron / RRULE would add a parser dependency.

## Open decisions

1. **User write-access model (blocking).** How much can users mutate the truth?
   - *Read-only / app-mediated writes* — the app is the sole writer; users view the schedule
     and change it via commands/buttons. No privileged intent, no concurrency hazards.
     (Leading option.)
   - *Direct hand-edit* — users edit the record messages by hand. Requires the Message Content
     privileged intent, input validation/feedback, and concurrent-writer handling.
2. **Recurrence granularity** — one-shot + simple intervals (dependency-free) vs. full cron
   (adds a parser dependency).
3. **Delivery semantics** — at-least-once (+ dedup) vs. at-most-once.
4. **Catch-up policy** for tasks that came due while the bot was offline.

## Constraints

- Prefer no new runtime dependencies; any that are added go through `uv add`.
- Fits the existing bridge: scheduled-task records and firing live alongside the
  Discord-event normalization / outbox path under `src/calfkit_organization/bridge/`.

## References

- Native Discord scheduling limits: Guild Scheduled Events and Webhook Events have no
  general timer primitive.
- Surveyed open-source reminder bots for scheduler patterns: `python-discord/bot`,
  `reminder-bot` (JellyWX), `Mayerch1/RemindmeBot`, `dcwds/remind-bot`,
  `TwiN/discord-reminder-bot`.
- Discord-as-store precedent (key/value libraries, no scheduling): `ankushKun/DiscordDatabase`,
  `zajrik/discord-storage`.
