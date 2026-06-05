# Roadmap

One-line summaries of planned and in-progress features. The details live in a per-feature
doc under [`roadmap/`](roadmap/) — keep specifics there, not here.

- [Scheduler](roadmap/scheduler.md) — durable reminders and recurring tasks, with Discord itself as the shared source of truth. *(In design)*
- [Tansu broker + S3-backed distributed agents](roadmap/tansu-broker.md) — native (no-Docker) Kafka-compatible broker now; shared-S3 multi-host agent communication later. *(Blocked on [calfkit#174](https://github.com/calf-ai/calfkit-sdk/issues/174))*
- [Onboarding & CLI UX](roadmap/onboarding-cli.md) — smoother quickstart: one-command startup, daemonized services, clearer commands. *(CLI command surface shipped in #34; supervisor/daemon + sequencing remain)*

To add a feature: write `roadmap/<feature>.md` with the details, then add a one-line row here
pointing to it.
