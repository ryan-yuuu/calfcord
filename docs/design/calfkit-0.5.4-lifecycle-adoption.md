# Adopting calfkit 0.5.4 Worker lifecycle (closing the lifecycle gaps)

> **Status:** Proposed — design ready for implementation (converged over three
> adversarial review rounds). R1: API verified against the released `v0.5.4` tag
> (the `@resource`/`on_startup` exclusivity is **real in the shipped tag** — a
> "fabricated" finding came from a pre-release PR-#165 branch); test-impact,
> teardown-ordering, error-handling fixes. R2: R1 fixes verified resolved, API
> re-confirmed; supervisor-invariant justification made deployment-independent
> (no compose coupling). R3: `_announce_up` simplified to **just raise on failure**
> (the R2 re-fire/degrade guard was dead code — `after_startup` fires once per
> single-use boot, and calfcord has no presence reconciliation to lean on); §5's
> broker-death wording corrected (FastStream logs/retries serving errors; only
> startup failures propagate). R1/R2 reviewers reported CONVERGED; R3 found only
> these two justification-level fixes, now applied.
> **Against:** calfkit `0.5.4` (project currently pins `~=0.5.1`).
> **Closes downstream:** the adoption half of
> [`calfkit-worker-lifecycle-gaps.md`](./calfkit-worker-lifecycle-gaps.md)
> (Gaps 1–4 → calfkit-sdk [#165]/[#166]/[#167]/[#168], shipped in 0.5.2 as
> [#175]).
> **Scope:** worker/broker lifecycle, provisioning, and the five runner
> processes only. Agent-POV history projection (calfkit [#154], 0.5.4) and the
> `calfkit run` dev CLI ([#181], 0.5.3) are **explicitly out of scope** — see
> [§9](#9-explicitly-out-of-scope-features-a-and-c).

[#154]: https://github.com/calf-ai/calfkit-sdk/issues/154
[#165]: https://github.com/calf-ai/calfkit-sdk/issues/165
[#166]: https://github.com/calf-ai/calfkit-sdk/issues/166
[#167]: https://github.com/calf-ai/calfkit-sdk/issues/167
[#168]: https://github.com/calf-ai/calfkit-sdk/issues/168
[#175]: https://github.com/calf-ai/calfkit-sdk/pull/175
[#180]: https://github.com/calf-ai/calfkit-sdk/issues/180
[#181]: https://github.com/calf-ai/calfkit-sdk/pull/181

---

## 1. Summary

`calfkit-worker-lifecycle-gaps.md` filed four gaps in the `Worker.run()`
contract that forced calfcord to hand-roll **three divergent run loops across
five processes**. calfkit `0.5.4` (via 0.5.2 [#175]) closes all four. This
document is the *adoption* plan: how to give each lifecycle responsibility back
to calfkit and delete the workarounds, while keeping the one sliver calfcord
legitimately owns.

The change is deliberately **subtractive**. Net effect:

- **Delete** `src/calfcord/_worker_runtime.py` entirely (the shared
  `run_worker_until_signal` + supervisor-invariant helper).
- **Delete** the bespoke agents run loop (`agents/runner.py:_run_worker`, the
  manual register → provision → `broker.start()` → presence sequence).
- **Delete** the bridge's manual `broker.start()` / `broker.running` plumbing
  (`bridge/gateway.py`).
- **Collapse** `_provisioning.provision_and_start_broker` to a small
  `provision_infra` helper (provision only; the worker now owns `broker.start()`).
- **Replace** imperative presence publishing with declarative
  `@worker.after_startup` / `@worker.on_shutdown` hooks.

What stays (and why) is just as important — see [§7](#7-what-stays-and-why).

### What 0.5.4 actually shipped vs. what the gaps doc proposed

The gaps doc proposed `run(after_startup=…, on_shutdown=…)` kwargs and a
`serving()` context manager. 0.5.4 shipped the same capabilities through a
cleaner, more idiomatic surface — this doc targets the shipped API:

| Gap | Proposed in gaps doc | Shipped in 0.5.4 (what we use) |
|---|---|---|
| 1 — lifecycle hooks | `run(after_startup=…, on_shutdown=…)` kwargs | decorators on the Worker: `@worker.after_startup`, `@worker.on_shutdown` (also `on_startup`/`after_shutdown`, `@resource`) |
| 2 — embeddable, non-blocking | `worker.serving()` CM | `await worker.start()` / `await worker.stop()` **and** `async with worker:` |
| 3 — opt-out signals | `run(install_signal_handlers=False)` | `start()`/`stop()`/`async with` install **no** signal handlers (only `run()` does) |
| 4 — documented contract | docs + `is_running` | [`docs/worker-lifecycle.md`](https://github.com/calf-ai/calfkit-sdk/blob/v0.5.4/docs/worker-lifecycle.md); `start()` returns only after consumer groups join; single-use guard; failed-boot teardown |

---

## 2. The one thing 0.5.4 did **not** fix: #180

Before designing anything, the load-bearing caveat: **calfkit-sdk [#180] is
still OPEN as of 0.5.4** (verified). The client registers a reply-dispatcher
subscriber on `client.reply_topic` at `Client.connect`, but provisions that
topic only lazily on first invoke. A direct `broker.start()` — which **both**
`worker.run()` and `worker.start()` perform — activates that subscriber before
any invoke, so on a no-auto-create broker (Tansu) `broker.start()` **hangs
forever**. `Worker.provision_topics()` walks only node
`subscribe_topics`/`publish_topic`, never the client reply topic.

**Consequence:** moving onto the managed `run()`/`start()` path does *not*
remove the reply-topic workaround. It must survive — relocated, not deleted (see
[§3](#3-the-provisioning-seam-provision_infra)). The canary that flips when #180
finally lands is
`tests/integration/test_broker_startup_provisioning.py::test_direct_start_succeeds_without_reply_topic_provisioning`.

---

## 3. The provisioning seam: `provision_infra`

`provision_and_start_broker` does two jobs today: provision blind-spot topics
**and** `broker.start()` (`_provisioning.py:123-158`). In 0.5.4 the worker owns
`broker.start()`, so the helper sheds that half and becomes purely "fill the two
provisioning blind spots calfkit can't see":

```python
# _provisioning.py — replaces provision_and_start_broker
async def provision_infra(client: Client, *, extra_topics: Iterable[str] = ()) -> None:
    """Create the topics calfkit's node-walking provisioner can't discover, before broker start.

    Two blind spots: the client's reply topic (calf-ai/calfkit-sdk#180 — still open in
    0.5.4) and calfcord's raw-subscriber / boot-publish / no-subscriber topics. Call once
    before worker.run()/start(); the worker provisions its own node topics during startup.
    Idempotent (already-existing topics are reported, not recreated) — but always does one
    admin create_topics round-trip, since the reply topic is always in the list.
    """
    await provision_extra_topics(client, [client.reply_topic, *extra_topics])
```

`PROVISIONING`, `provision_extra_topics`, and the three `*_infra_topics()` sets
(`_provisioning.py:52-120`) are unchanged — they encode genuine calfcord
knowledge calfkit cannot derive (single-partition ordering for `agent.steps`;
the raw control-plane topics; the no-subscriber ambient-discard topic).
**`provision_infra` is the entire surface of calfcord's deviation from vanilla
calfkit lifecycle.**

> **Why a plain pre-call, not an `@worker.on_startup` hook?** Three reasons, in
> order of importance:
> 1. **It needs no broker** — provisioning uses a separate admin client
>    (`TopicProvisioner.from_connection`), so there is nothing to gain from
>    running it inside the worker's startup; a pre-call reads top-to-bottom.
> 2. **It keeps the seam usable from every surface** (`run()`, `async with`)
>    without caring which one the runner picked.
> 3. **It sidesteps a real footgun:** in the **released v0.5.4 tag**, `@resource`
>    and the `on_startup`/`after_shutdown` callbacks are **mutually exclusive on
>    one owner** — if both are present the `@resource` brackets win and the
>    callbacks are dropped with a warning (`worker.py:_owner_cms`, lines 387-411;
>    documented in calfkit's `docs/worker-lifecycle.md` "One pattern per owner").
>    The `after_startup`/`on_shutdown` *serving* phase is **not** subject to this
>    (so presence hooks in §4 are unaffected). Putting provisioning in
>    `on_startup` would foreclose ever adding a worker-level `@resource` later.
>
> *(Provenance note: this exclusivity holds in the released `v0.5.4` tag and its
> docs. An earlier PR-#165 development branch built both unconditionally; that is
> pre-release and not what we install. Decisions 1–2 above stand regardless of
> the footgun, so the choice is robust to any further upstream change here.)*

---

## 4. The four standalone runners become declarative

`worker.run()` now does, in order: register handlers → `provision_topics()`
(node topics) → `broker.start()` (consumers join) → `after_startup` → block on
SIGINT/SIGTERM → drain → `on_shutdown` → `after_shutdown`. So each standalone
runner collapses to **build → provision_infra → run**.

### tools / mcp

```python
# tools/runner.py _amain — after
worker = Worker(client, tool_nodes)
logger.info("starting calfkit-tools worker tools=%s ...", ...)   # keep the existing boot log
await provision_infra(client)        # reply topic only
await worker.run()
```

Removed vs. today (`tools/runner.py:172-213`): the eager
`provision_and_start_broker(client)` + `broker.start()`, the local `_run_worker`
wrapper (`tools/runner.py:143-152`), and the `run_worker_until_signal` import.

**mcp** is the same shape with one difference: it has **no** local `_run_worker`
wrapper — it calls `run_worker_until_signal(worker, drain_label="mcp bridge worker")`
**directly** (`mcp/runner.py:117`). So its migration drops that direct call (and
the `provision_and_start_broker(client)` at `mcp/runner.py:107`) for
`await worker.run()`; there is no wrapper to delete.

### router

```python
# router/runner.py _amain — after
worker = Worker(client, nodes)
await provision_infra(client, extra_topics=router_infra_topics())
await worker.run()
```

### agents (presence via hooks)

The agents runner is the biggest deletion. Today it abandons `run()` to publish
presence at precise lifecycle points (`agents/runner.py:573-617`). 0.5.4 makes
those two points declarative. The two hooks are inlined directly in `_amain`
(single-site, two-line closures — a separate `control_plane/presence.py` module
would be unnecessary abstraction for one caller and would risk re-triggering the
deferred-import cycle the agents runner guards at `runner.py:79-95`; the *publish*
helpers already live in `control_plane/publish.py`):

```python
# agents/runner.py _amain — after
worker = Worker(calfkit_client, nodes)
for ref in definition_refs:
    register_control_sink(calfkit_client, ref)   # the agents process's ONLY raw subscriber — still before run()

@worker.after_startup                            # broker live, consumers joined
async def _announce_up(_ctx):
    for ref in definition_refs:
        # Raise on failure (do NOT swallow) — exactly as today's pre-run publish
        # (runner.py:602-607). A failed announce here means the producer failed right after a
        # successful broker.start: a real infra fault that leaves the agent invisible, and
        # calfcord has NO presence reconciliation (the bridge's discovery ping is one-shot at
        # on_ready; no periodic ping, no TTL). So fail the boot loudly; recovery is process
        # restart → fresh boot → fresh announce (§10 risk 4). after_startup fires once per
        # (single-use) Worker boot, so there is no steady-state re-fire to handle.
        await publish_state_event(calfkit_client, build_state_event(ref.current, cause="startup"))
        logger.info("announced startup for agent=%s", ref.current.agent_id)

@worker.on_shutdown                              # broker still up, before drain
async def _announce_down(_ctx):
    await _publish_departures_best_effort(calfkit_client, definition_refs)  # best-effort: logs + swallows

logger.info("starting worker with %d agent(s): %s", len(nodes), ...)   # keep the boot log
await provision_infra(calfkit_client, extra_topics=agent_infra_topics(ids))
await worker.run()
```

- `_run_worker` (the agents copy, `runner.py:405-456`) is **deleted**.
- `_publish_departures_best_effort` (`runner.py:458-495`) is **kept** — it's the
  best-effort *policy* (logs + swallows, correct for a shutdown goodbye), now
  invoked from the `on_shutdown` hook.
- **Error-handling (CLAUDE.md hard rule):** `_announce_up` **raises** on publish
  failure — exactly as today's pre-run publish (`runner.py:602-607`); an agent
  that can't announce is invisible and calfcord has no presence reconciliation,
  so fail the boot loudly (the 0.5.4 contract that an `after_startup` exception
  unwinds the boot makes this clean). `_announce_down` swallows (a missed goodbye
  == a hard crash). `after_startup` fires **once** per single-use Worker boot, so
  there is no re-fire to special-case; calfkit's docs note it *could* re-fire on
  a rebalance in principle — if that ever becomes real, revisit (calfcord would
  need presence reconciliation first). See [§10](#10-risks--verification) risk 4.
- `after_startup`/`on_shutdown` are the *serving* phase, which does **not**
  collide with `@resource`. They preserve today's exact ordering: announce only
  after the producer is live (the Gap-1 `IncorrectState` constraint is now a
  structural guarantee), depart before drain — and the per-agent announce log is
  carried forward.
- The raw control sink (`register_control_sink` — the agents process's **only**
  raw subscriber; covers `agent.{id}.control.in` + `bridge.discovery`; there is
  **no** peer-roster subscriber) stays registered at `_amain` scope before
  `run()`, exactly as today — `register_handlers()` only registers the worker's
  own nodes and leaves pre-registered raw subscribers on `client._connection`
  intact, so `broker.start()` connects them all together. Its topics line up with
  `agent_infra_topics(ids)` (`agent.state`, `bridge.discovery`, one
  `agent.{id}.control.in` per agent).

---

## 5. Delete `_worker_runtime.py` (native `run()`)

`_worker_runtime.run_worker_until_signal` (used by tools/mcp/router) exists only
because pre-0.5.4 `run()` didn't surface a "clean-exit-without-signal is a crash"
contract (`_worker_runtime.py:15-23`). With native `worker.run()`:

- it installs SIGINT/SIGTERM and blocks; a clean return happens **only** on a
  signal;
- a **startup** failure (e.g. `broker.start()` can't reach Kafka) propagates out
  of `run()` as an exception → non-zero exit → supervisor restarts. (In-flight
  *serving* errors are handled by FastStream itself — a handler exception is
  logged and swallowed, a mid-serving broker blip is retried — so the process
  stays up, degraded, rather than returning cleanly. Either way, `run()` never
  returns *cleanly* without a signal. This degraded-survival behavior is
  FastStream's and is unchanged by this migration — the deleted module never
  caught it either, since it only surfaced what `run()` raised.)

So the supervisor invariant's specific case (a *clean* return with *no* signal)
is **unreachable** via native `run()`, verified against the v0.5.4 source:
`worker.run()` → `app.run()` busy-waits on an internal `_should_exit` flag that
**only** the SIGINT/SIGTERM handler sets (calfcord never calls `app.exit()`), and
calfcord's consumers are infinite (they never complete on their own). So `run()`
returns `None` **iff** it was signalled (graceful → exit 0, correct everywhere),
and every abnormal mode (broker death, handler-task crash) propagates as an
exception → non-zero exit. **Decision: delete the module** and replace its three
call sites with `await worker.run()`. No replacement guard is needed.

**Why no `raise SystemExit` guard, and no restart-policy dependency.** Because a
clean return *is* a graceful signalled shutdown, it should exit 0 — at **every**
deployment surface (bare `uv run`, systemd, k8s, Docker), independent of restart
policy. A blanket `raise SystemExit(1)` after `run()` would be *wrong*: it would
turn a normal operator `docker stop`/SIGTERM into a non-zero exit and a spurious
restart under `Restart=on-failure`/systemd. And there is no spurious-clean-exit
case left to cover. This keeps the guarantee self-contained in the runtime
contract rather than coupling it to a `docker-compose.yml` key (which would also
not cover the `calfkit-mcp` runner — a standalone runner with no compose service
in the default stack — nor any non-Docker host). Net: the deleted module guarded
an unreachable case; native `run()`'s own semantics are the guarantee.

---

## 6. Bridge: manual broker plumbing → `async with worker`

The bridge legitimately embeds a foreground (the Discord gateway WebSocket owns
the loop), so it keeps its own signal handling and the gateway/stop race — but
everything it reaches into broker internals for goes away
(`bridge/gateway.py:779-845`):

**Resource nesting is load-bearing** — the persona sender, client, and
transcript store wrap the worker so they stay open while the broker drains
consumers that use them:

```python
# bridge/gateway.py _run — after  (nesting shown explicitly)
async with DiscordPersonaSender(settings) as persona_sender:
  async with Client.connect(server_urls, reply_topic=_REPLY_TOPIC, provisioning=PROVISIONING) as calfkit_client:
    async with _open_transcript_store(settings) as transcript_store:
        # ... build ingress, gateway, the three consumer nodes, typing_notifier ...
        worker = Worker(calfkit_client, [consumer_node, synthesized_node, steps_node])
        register_state_consumer(calfkit_client, registry,
                                on_first_seen=gateway._slash.schedule_resync,
                                on_departed=gateway._slash.schedule_resync)   # raw sub, before start
        await provision_infra(calfkit_client, extra_topics=bridge_infra_topics())

        try:                                # outer try → typing_notifier.aclose() in its finally
            async with worker:              # start(): register → provision node topics → broker.start (all subs join)
                # consumers are joined BEFORE we accept Discord events — the Gap-2
                # join-before-serve correctness is now a guarantee of start(), not hand-built.
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, stop.set)   # bridge still owns signals (start/stop install none)
                gateway_task = asyncio.create_task(gateway.start())
                stop_task = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait({gateway_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                    # A fatal gateway crash (not a signal) must propagate → non-zero exit;
                    # asyncio.wait won't surface it. On the signal path gateway_task is still running.
                    if gateway_task in done and not gateway_task.cancelled() and gateway_task.exception():
                        raise gateway_task.exception()
                finally:
                    for t in (gateway_task, stop_task):
                        if not t.done():
                            t.cancel()
                    await gateway.close()    # stop the DISCORD ingress (the synthesized consumer is a 2nd
                                             # ingress; it drains with the broker — safe, transcript_store closes last)
            # worker.stop() drains the broker at the `async with worker` exit — the steps/outbox
            # consumers finish their in-flight hops, still using persona_sender + typing_notifier.
        finally:
            await typing_notifier.aclose()   # AFTER the drain (so an in-flight steps hop never calls a
                                             # cancelled notifier) AND unconditional (runs on crash/cancel too)
    # transcript_store / client / persona close here, after the drain — outermost-last
```

Removed: `worker.register_handlers()`, the explicit `worker.provision_topics()`,
the `broker.running` guard, the manual `broker.start()`, and the now-false
"Worker.run would call register_handlers twice" comment (`gateway.py:773-824`).

**Teardown order (the correction from review):** ingress stops first
(`gateway.close()` in the inner `finally`), then the broker drains
(`worker.stop()` at the `async with worker` exit) **while persona_sender +
typing_notifier are still open**, then `typing_notifier.aclose()`, then the outer
resources. Today's code closes the typing notifier *before* the broker drains;
because `TypingNotifier.aclose()` cancels in-flight tasks
(`discord/typing.py:128`), a steps hop draining after that could fire into a
cancelled notifier. The new order eliminates that race. A regression test should
assert a steps hop in flight at SIGTERM completes without hitting a closed
notifier (see [§10](#10-risks--verification)).

Notes:
- The discovery ping stays in the gateway's `on_ready` (it fires after the
  Discord handshake, not at broker startup) — unaffected.
- For the bridge, `provision_infra`'s leading `client.reply_topic` is
  `discord.outbox`, which is **also the outbox node's inbox** — so it is
  **redundant-by-construction** (already covered by `worker.provision_topics()`
  inside `start()`), an idempotent no-op, **not** the #180 fix the same line is
  for the four standalone runners. Kept for one uniform `provision_infra` call
  across all five processes; the redundancy is documented so a future reader
  doesn't mistake it for load-bearing. (If `discord.outbox` ever stops being the
  outbox inbox, this line silently becomes the real reply-topic provision — note
  it there.)

---

## 7. What stays (and why)

| Kept | Why |
|---|---|
| The raw control plane (`control_plane/publish.py`, `sink.py`, `state_consumer.py`) | Uses `client._connection` because calfkit still has **no public publish / non-node-subscriber API**. Presence *publishes* move into hooks; the raw *subscribers* stay registered before `run()`/`start()`. A one-file swap if calfkit ships a public `Client.publish`. |
| `ProvisioningConfig` (`PROVISIONING`) + `provision_extra_topics` + `*_infra_topics()` | calfcord domain knowledge calfkit can't derive (`num_partitions=1` is load-bearing for `agent.steps` ordering; the raw-subscriber/boot-publish/discard topics are invisible to `topics_for_nodes()`). |
| The #180 reply-topic workaround | Confirmed still required on 0.5.4 ([§2](#2-the-one-thing-054-did-not-fix-180)). Relocated into `provision_infra`. |
| The bridge's own signal handling + gateway foreground race | The bridge embeds a foreign foreground; `start()/stop()` install no signals **by design**, so the bridge keeps owning shutdown ordering. |

---

## 8. Deliberate simplifications — what we do NOT do

Two tempting moves rejected in favor of a simpler, more elegant result:

- **No `@resource` migration.** calfcord's resources (persona sender,
  `DiscordSender`, transcript store) are injected **by construction** into nodes
  *and* non-worker components (the gateway, the ingress, `private_chat.init`).
  `ctx.resources["k"]` is a stringly-typed lookup, narrower than the current
  typed injection, and it reintroduces the `@resource`-vs-`on_startup` footgun.
  Keeping `async with` for resources is simpler and more type-safe. Revisit only
  if a resource ever becomes purely worker-handler-scoped.
- **No single "generic deployer."** The earlier deploy-unification idea
  (one provider-pattern function for all four standalone runners) made sense when
  the shared run loop was large. With 0.5.4 the shared skeleton is two lines
  (`provision_infra` + `worker.run()`), so a unifying function would only absorb
  the genuinely *divergent* parts (which nodes, which resources, codex prewarm,
  presence) as a pile of flags — a god-function, not a simplification. **Share
  one small primitive (`provision_infra`) and inline the rest** (the two presence
  hooks live directly in the agents `_amain`; the publish helpers they call are
  already shared in `control_plane/publish.py`). Each `_amain` stays a short,
  linear, readable composition.

The separation of concerns this lands on:

| Concern | Owner |
|---|---|
| Register handlers, provision node topics, start/stop broker, signals | calfkit `Worker` |
| Provision non-node topics + client reply topic (#180) | `_provisioning.provision_infra` |
| Publish agent presence / departure | control-plane `after_startup`/`on_shutdown` hooks |
| Compose the above | each thin runner |

---

## 9. Explicitly out of scope: Features A and C

- **Feature A — agent-POV history projection ([#154], 0.5.4).** calfkit now
  projects POV *inside the agent loop* over a cumulative, `name`-stamped
  `message_history`. calfcord's projection is **bridge-side and
  fetch-per-turn** (`bridge/history.py:project_history`), reused by the router
  (`self_agent_id=None`) and the registry-free A2A tools process, and depends on
  bridge-only stages (Discord fetch, webhook→agent-id resolution, `/clear`
  truncation, step-transcript replay) that cannot move into the agent loop
  without violating the distributed-deploy invariants. calfkit's always-on
  projection is a verified **transparent no-op** for calfcord's pre-projected
  wire histories (self turns carry no `name`; human attribution lives in
  *content*, not the `name` field → multi-participant detection never triggers),
  so the version bump is safe — it simply does not let calfcord delete code.
  Add a guard test asserting that no-op holds.
- **Feature C — `calfkit run` dev CLI ([#181], 0.5.3).** Dev-only. calfcord
  ships its own product-branded `calfcord run` with onboarding/native-install
  UX. Not applicable.

---

## 10. Risks & verification

1. **#180 canary, made self-enforcing** — the existing canary
   `test_broker_startup_provisioning.py::test_direct_start_succeeds_without_reply_topic_provisioning`
   asserts the *broken* behavior still reproduces, so when #180 is fixed it goes
   **red**, not green — a red test after a calfkit bump is the kind of noise that
   gets `xfail`'d and forgotten, leaving the workaround as permanent cruft.
   **Add a self-announcing exit gate instead:** an `@pytest.mark.xfail(strict=True)`
   test asserting that a direct `start()` *succeeds without* the reply-topic
   pre-provision — it stays xfail while #180 is open and flips to "unexpectedly
   passing" (a hard failure) the moment calfkit fixes it, forcing removal of
   `provision_infra`'s reply-topic line. Pair it with a `TODO(calfkit#180)`
   comment on that line. **This canary needs a real no-auto-create broker
   (Tansu), so it lives in the gated integration lane — that lane MUST run on the
   calfkit-version-bump PR (Phase 1), or the strict-xfail signal stays silent
   exactly when #180 would land.**
2. **Gap-2 join-before-serve** — `worker.start()` must return only after
   consumer groups join (asserted by calfkit's `docs/worker-lifecycle.md`).
   Keep/extend a bridge test that a reply published immediately after `start()`
   is not dropped.
3. **Bridge teardown ordering (new test)** — assert a steps hop in flight at
   SIGTERM completes during `worker.stop()` drain and never calls a cancelled
   `TypingNotifier` — i.e. `typing_notifier.aclose()` runs after the drain
   ([§6](#6-bridge-manual-broker-plumbing--async-with-worker)).
4. **Agent boot-failure matrix (verify + note)** — `_announce_up` raises on a
   failed publish. Confirm 0.5.4's `after_startup`-exception path aborts startup
   and unwinds (it does: `worker.py` `_hook_after_startup` runs serving teardown
   → MCP close → `broker.stop()` → resource teardown before re-raising). Note the
   one residual edge: if `_announce_up` raises mid-loop (agent *k* of *n*), agents
   `1..k-1` published "startup" but the failing hook's own `on_shutdown` does not
   fire (its serving CM never fully entered), so no matching "departure" goes out
   — the boot then fails and the process exits non-zero → the supervisor restarts
   it (per the deployment's restart policy) and the next boot re-announces.
   Acceptable; documented so it isn't a surprise. (Mitigation if ever needed: make
   `_announce_up` all-or-nothing, or flush departures for already-announced
   agents on boot failure.)
5. **Provisioning idempotency** — `provision_infra` (pre-run) and
   `worker.provision_topics()` (in-run) may touch overlapping topics; both are
   idempotent, but confirm no double-create error against Tansu.
6. **Single-use Worker** — 0.5.4 Workers are single-use (a second
   `start()`/`run()` raises). All five runners are build-once/run-once, so this is
   low-risk; the one rule to honor is **any boot retry must construct a new
   `Worker`** (re-entering `async with worker:` on the same instance raises).

### Test impact

| Test | Change |
|---|---|
| `tests/test_worker_runtime.py` | **Deleted** with the module. |
| `tests/test_provisioning_wiring.py` | Drop the provision-then-`start` ordering assertions; add `provision_infra` coverage; keep policy + infra-topic-set + reply-topic-distinct tests. |
| `tests/agents/test_runner.py` | **Remove the top-level `_run_worker` import** (`test_runner.py:25`) or the whole module fails to collect (≈40 unrelated tests). `TestPublishDeparturesBestEffort` is **kept unchanged** (the helper survives); only `TestRunWorkerShutdownCallback` retargets to the `on_shutdown` hook. Add tests for `_announce_up` raising and the boot-failure edge (risk 4). |
| `tests/tools/test_runner.py`, `tests/router/test_runner.py` | Remove the `runner._run_worker(...)` call-site tests (`tools:241,254`, `router:78,90`); assert `provision_infra` + `worker.run()` wiring instead. |
| `tests/mcp/test_runner.py` | mcp migrates too (drops the direct `run_worker_until_signal` call at `mcp/runner.py:117` + `provision_and_start_broker` at `:107`). Its existing load-error/empty-registry guards on `_amain` don't reference the deleted symbols, but the runner change is real work. |
| `tests/integration/test_broker_startup_provisioning.py` | **Edit (not unchanged):** it imports `provision_and_start_broker` (`:34`) and exercises the removed `worker=` param (`:68`) — drop that import + the `test_provision_and_start_broker_*` test; retain the canary body (it calls `worker.provision_topics()` + `broker.start()` directly) and convert it to the self-enforcing xfail (risk 1). |
| Bridge run-path tests | Assert `async with worker` ordering: state consumer registered + infra provisioned before `start()`; broker drained on exit; the teardown-ordering test (risk 3). |

A pre-flight `grep` for `_run_worker`, `run_worker_until_signal`,
`provision_and_start_broker`, `_select_exit_exception` is the cheapest way to
catch every collection-breaking import before each phase.

### Docs to update on landing

- `docs/architecture.md` §"Known calfkit lifecycle limitations" — remove/rewrite
  (the limitation is gone).
- `docs/design/calfkit-worker-lifecycle-gaps.md` — flip status header: Gaps 1–4
  closed by 0.5.4 ([#175]); update the "three run loops" evidence table.
- `docs/ambient-routing.md` (the `_run_worker`-has-three-copies note) — resolved.

---

## 11. Suggested phasing (smallest blast radius first)

1. Bump `calfkit[mcp-codegen]~=0.5.4` (`uv add`); run the full suite + the gated
   Tansu integration test to establish a green baseline.
2. Add `provision_infra`; migrate **tools + mcp** (lowest risk, no presence).
3. Migrate **router**; then delete `_worker_runtime.py` (all three consumers
   gone).
4. Migrate **agents** (inline presence hooks; delete the bespoke loop).
5. Migrate **bridge** (`async with worker`, with the corrected teardown order).
6. Docs + memory update (see "Docs to update").

**Each phase leaves a green, runnable tree** because 0.5.4 retains the manual
`register_handlers()` + `provision_topics()` + `broker.start()` path (verified
present in the tag) — so the *un-migrated* runners keep working after the Phase-1
bump, and the migration can proceed one runner at a time. (The phasing is an
ordering convenience, not a correctness boundary; the branch still ships as one
release per the rollback note.)

**Per-phase exit criteria:** TDD per the project's `/test-driven-development`
convention, and **`make check` clean (ruff + mypy) on every changed file** — the
migration touches five runners and adds/deletes modules, so the lint/type gate is
not optional (CLAUDE.md: "Ruff clean for new/changed files").

**No entry-point / packaging changes.** Only the `_amain` internals change; every
`main()` signature and the `[project.scripts]` map (`calfkit-agent`,
`calfkit-tools`, `calfkit-router`, `calfkit-mcp`, `calfkit-bridge`) are untouched,
so the Dockerfile, `docker-compose.yml`, and the `calfcord run` UX need no edits.

**Rollback / coupling.** The 0.5.4 bump (Phase 1) and the runner migrations are
coupled — a migrated runner cannot run on 0.5.1. Keep the whole sequence on this
`calfkit-0.5.4-adoption` branch and **do not merge until Phase 5 is green**, so a
regression found late is a branch-level revert, not a half-migrated `main`. Ship
it as one release.
