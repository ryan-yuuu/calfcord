# Tansu Broker — Native Kafka-Compatible Broker & S3-Backed Distributed Agents

**Status:** Blocked — gated on upstream [`calf-ai/calfkit-sdk#174`](https://github.com/calf-ai/calfkit-sdk/issues/174)
(broker topic auto-creation). The single-box switch is fully designed and validated; the
distributed S3 phase is exploratory. No code has landed.

## Goal

Replace Redpanda with [**Tansu**](https://github.com/tansu-io/tansu) as calfcord's broker, in two stages:

1. **Near term — a native, no-Docker broker.** Make the local broker a single Rust binary that runs
   natively on macOS *and* Linux, so the installer's "no Docker needed" promise holds end to end
   (today it breaks at the broker step on macOS).
2. **Long term — S3-backed distributed agent communication.** Use Tansu's coordinator-free,
   object-storage-backed multi-broker mode so agents and tools on different hosts/VMs can collaborate
   as one logical cluster with no single broker to stand up or babysit.

## Why Tansu

- **Redpanda's broker is Linux-only.** On macOS even Homebrew runs it inside Docker (`rpk container
  start`), so calfcord's native installer — which targets macOS users and bootstraps a private `uv`
  precisely to avoid prerequisites — still forces Docker just for the broker.
- **Tansu is a single static Rust binary**, Apache-2.0, Kafka-wire-compatible, that runs natively on
  macOS and Linux (~20 MB resident). It can be bootstrapped exactly the way the installer already
  bootstraps `uv`.
- **Pluggable storage** — `memory`, `libsql` (SQLite), `postgres`, `s3` — spanning the throwaway-local
  case through the distributed case with one binary.
- **Coordinator-free multi-broker** — multiple Tansu brokers can share one S3 bucket (same
  `--kafka-cluster-id`) and coordinate via S3 conditional writes, with no separate metadata service.
  This is the enabler for the distributed phase below.

## Guiding principle: keep calfcord's decoupling invariant intact

calfcord's defining property is that agents and tools are independently-deployable microservices that
share nothing but the broker. The broker choice must preserve that: a single shared endpoint today, a
shared object store tomorrow — never a topology that requires one process to know another's internals.

---

## Stage 1 — Switch the local broker to Tansu (near term)

### Decisions (agreed)

- **Delivery:** native binary is the primary path; a Docker option is kept for Docker users.
- **Default storage:** `memory` (ephemeral). Topics/messages reset on broker restart — a deliberate
  change from Redpanda's persistent volume. SQLite is documented as a one-line persistent upgrade.
- **Cutover:** full removal of Redpanda from the installer, init, compose, and docs.
- **Scope:** single-box only now; the distributed S3 path is deferred to Stage 2 (this doc).

### The blocker (why this is parked)

calfcord/calfkit rely on **broker-side topic auto-creation**: nothing in the
`calfcord → calfkit → FastStream → aiokafka` stack ever creates a topic. Tansu has **no**
auto-creation — confirmed three ways:

- **Empirically:** producing to / subscribing to a non-existent topic fails with
  `UnknownTopicOrPartitionError`.
- **Control:** the *same* test auto-creates cleanly on Redpanda (`--mode dev-container`), proving the
  dependency is on broker auto-create, not a client bug.
- **FastStream version-independent:** reproduced on FastStream `0.6.7` and the latest `0.7.1` —
  upgrading FastStream does not help.
- **Source:** Tansu's broker ignores the Kafka `allow_auto_topic_creation` flag and exposes no
  auto-create knob (docs + source); calfkit contains no topic-creation/admin calls.

Tansu *does* fully implement the explicit `CreateTopics` API, and once topics exist, produce/consume
and **consumer groups** work end-to-end through FastStream. So the gap is specifically topic creation.

The correct fix lives in **calfkit** (the layer that owns produce/subscribe I/O *and* its internal
reply/return topics), not in calfcord — handling a Kafka wire error in the application layer would be a
leaky abstraction and would force each process to know topics it doesn't own, breaking the decoupling
invariant. This is filed as [`calfkit-sdk#174`](https://github.com/calf-ai/calfkit-sdk/issues/174);
the design is intentionally left to the calfkit maintainers. **Stage 1 resumes once #174 lands.**

### Migration plan (ready to execute once unblocked)

1. **Native bootstrap + `calfcord broker`.** Add an `ensure_tansu` step to `scripts/install.sh`
   mirroring the existing `ensure_uv` (OS/arch detect, download the release binary to
   `~/.calfcord/bin/tansu`, strip the macOS quarantine attribute, verify). Add a `broker` verb to the
   `calfcord` shim that starts Tansu with calfcord defaults.
2. **init + config.** `calfcord init` offers "start a local Tansu broker"; default
   `CALF_HOST_URL=localhost:9092`; rewrite the `.env.example` Kafka block (Tansu, memory = ephemeral,
   SQLite upgrade, Docker option).
3. **Docker option.** Replace the `redpanda` compose service with a `tansu` service (memory storage,
   port 9092, no data volume).
4. **Docs sweep.** Remove Redpanda across README, `CLAUDE.md`, and `docs/`; update the broker default.
5. **Verification.** Native + Docker end-to-end (`@agent` reply + A2A thread), test suite, and a
   `grep -ri redpanda` completeness check.

### Validated facts (captured during the spike, for whoever executes this)

- Tansu **v0.6.0**; release assets `tansu-{aarch64,x86_64}-apple-darwin.tar.gz` and
  `tansu-{aarch64,x86_64}-unknown-linux-gnu.tar.gz`; binary at `bin/tansu` in the tarball.
- CLI: `tansu broker` (default command); `--storage-engine` default `memory://tansu/`;
  `--advertised-listener-url` default `tcp://localhost:9092`; `--cluster-id` (alias
  `--kafka-cluster-id`, default `tansu_cluster`); `tansu topic create <name> --partitions N`.
- Docker image `ghcr.io/tansu-io/tansu`; the tarball ships a reference `compose.yaml` + `example.env`.
- macOS: curl-downloaded binaries are quarantined and won't run until the attribute is stripped — the
  bootstrap must handle this.

---

## Stage 2 — S3-backed distributed agent communication (future / exploratory)

### Vision

Agents and tools running on different machines — a personal laptop, a work laptop, a cloud VM — that
collaborate **as one logical cluster**, with no central broker to provision or keep alive. Each host
runs its own lightweight Tansu broker; all of them are backed by **one shared S3 bucket** and joined by
the **same `--kafka-cluster-id`**. A message produced on one host is consumed on another through its
*local* broker, because the brokers share the object store as their source of truth.

### How it works (direction, not design)

- Tansu's brokers are **stateless compute**: the log lives in object storage, so any broker can serve
  any partition.
- Coordination is **coordinator-free** — Tansu uses **S3 conditional writes** (compare-and-swap) to
  sequence batches and agree on metadata, so multiple brokers sharing a bucket form one cluster
  *without* a separate metadata service (unlike most diskless Kafka designs).
- The unit of identity is the **`--kafka-cluster-id` + bucket**: same id + same bucket = one cluster;
  different ids = isolated clusters that merely share storage.

### Why it fits calfcord

This is the natural evolution of calfcord's "shared broker URL" federation: the shared *endpoint*
becomes a shared *bucket*. It removes the single-broker single-point-of-failure, enables
geo-distribution (a broker near each region), and keeps every process self-sufficient — fully in line
with the independently-deployable invariant.

### Open questions / what must be validated first

- **Consumer groups across brokers.** calfcord agents rely on consumer groups; Tansu documents a
  multi-broker consumer-group caveat for the PostgreSQL engine. The S3-engine behavior across brokers
  must be validated before this is viable — it is the load-bearing test.
- **Latency.** Every produce coordinates via an S3 conditional-write round-trip — materially higher
  than local-disk brokers. Acceptable for Discord-cadence chat, but a real trade-off to measure.
- **Shared dependency shifts, not disappears.** There is no single broker SPOF, but the S3 bucket
  becomes the central source of truth and consistency point; cross-region object-store latency applies.
- **Maturity.** Tansu is new (debuted 2026); the multi-broker S3 mode is its most advanced path.
- **Same auto-create gap.** Stage 2 inherits the `#174` topic-creation prerequisite.

### Dependencies

1. Stage 1 (the local switch) landed.
2. [`calfkit-sdk#174`](https://github.com/calf-ai/calfkit-sdk/issues/174) resolved.
3. A multi-broker / shared-S3 validation spike (consumer groups + latency) passing.

---

## References

- Upstream blocker: [`calf-ai/calfkit-sdk#174`](https://github.com/calf-ai/calfkit-sdk/issues/174)
- Tansu: [repo](https://github.com/tansu-io/tansu) · [docs](https://docs.tansu.io/)
- Related calfcord docs: [`docs/distributed-deployment.md`](../docs/distributed-deployment.md),
  [`docs/architecture.md`](../docs/architecture.md), [`docs/configuration.md`](../docs/configuration.md)
