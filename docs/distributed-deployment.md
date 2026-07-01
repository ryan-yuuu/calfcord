# Distributed Deployments — Altitude-3 Graduation

Going multi-host is a **graduation, not a rewrite**. Nothing you built on
one machine changes: the same `agents/*.md`, the same `.env`, the same
`calfcord start` / `calfcord agent start <name>` commands all keep
working. The single thing that moves is where the broker lives — every
process just dials a remote `CALF_HOST_URL` instead of a local one.

That is the whole graduation. The rest of this page is the deep operator
reference for the heavier shapes (narrowing a host's tool surface, pinning
a tool to one box, per-agent crash isolation, broker auth) you reach for
*after* the basic multi-host split is running.

## The portable invariant

What makes calfcord portable across one host or twenty is **not** the
Process Compose YAML the single-host supervisor generates. That YAML is
host-local derived state — `calfcord start` regenerates it from your
config and you never edit it (see
[`architecture.md`](architecture.md#running-modes)). It does not travel.

What *does* travel — the contract every process agrees on regardless of
which box it runs on — is three things:

- **The Kafka wire.** Topic names are derived from agent and tool names
  (`tool.terminal.input`, `agent.scribe.private.input`, …), so any process on
  any host finds its peers by dialing the same broker. This is the seam.
- **`.env`.** The shared `CALF_HOST_URL` (broker address) plus the
  Discord credentials. Identical on every host except `CALF_HOST_URL`,
  which on the broker's own host may be `localhost:9092` and elsewhere is
  the broker's reachable address.
- **`agents/*.md`.** An agent's identity and instructions are the same
  file wherever it runs; the bridge learns about it from calfkit's live
  agent mesh (the `AgentCard` each agent advertises), never from a shared
  filesystem.

Hold those three constant and the supervisor underneath is an
implementation detail you can swap (Process Compose on a laptop, systemd
on a VM, K8s in a cluster) without touching a single user-facing command.

## Graduating to multiple hosts: same commands, remote broker

The minimal multi-host split needs no new concepts — only a broker
address every host can reach. On each host:

1. **Install calfcord** the same way (the one-line installer, then
   restart your shell). Each host gets its own `$CALFCORD_HOME`.
2. **Point it at the shared broker:**

   ```bash
   calfcord self set-broker laptop:9092
   ```

   This writes `CALF_HOST_URL=laptop:9092` into the install's config
   `.env`. Use the broker's reachable address — a tailnet hostname, a VPC
   private IP, etc. (see [Broker authentication](#6-broker-authentication)
   for why that network matters).

3. **On the host where the broker + bridge live, open the workspace:**

   ```bash
   calfcord start          # broker + bridge, detached, health-gated
   ```

   `start` brings up only the **substrate** (broker + bridge). It is the
   one host that owns the Discord gateway; the broker it starts must be
   advertised on an address the other hosts set in step 2.

4. **On each host, clock in the roster it owns:**

   ```bash
   calfcord agent start scribe     # this host runs the scribe agent
   calfcord tools start            # ...or this host runs the tools process
   calfcord mcp start github       # ...or an MCP server (one per mcp.json entry)
   ```

   A roster member started on host B joins the *same live org* as the
   substrate on host A — it just dials the remote broker. There is no
   per-host config beyond `CALF_HOST_URL`: an agent's `.md` is identical
   wherever it runs.

That is the graduation. `calfcord status` on any host shows the org board
(the substrate health plus the live roster, reconstructed broker-wide),
so you see every agent regardless of which host it clocked in from.

### The cross-host duplicate-name guard is CLI-side

`calfcord agent start <name>` first probes the **whole organization** over
the broker (the live-roster probe reads calfkit's native agent mesh —
`client.mesh.get_agents()` over `calf.agents` — org-wide) and refuses to
start `<name>` if it is already live on *any* host — printing "agent 'X' is
already running in the organization" instead of starting a duplicate. This
is what stops a double-reply / split-brain when two hosts each try to run
`scribe`.

Because the mesh is **online-only and heartbeat-staleness-filtered** rather
than an active ping, liveness is now passive: a *gracefully* stopped agent
tombstones immediately, but a *crashed* one lingers as "online" until its
heartbeat lapses (~90s), so a restart fired inside that window can be
refused as a duplicate. Wait out the stale window, or restart it on the host
that owned it.

The guard is **CLI-side only** — the bridge is unchanged and treats a
re-registration as a benign re-announce. Because the probe is broker-wide
(not the local supervisor's process list), it is distributed-correct: a
duplicate on a second host is caught without any bridge enforcement. The
one gap left to the bridge (deferred) is the simultaneous-start race:
two `agent start scribe` fired at the same instant on two hosts can both
see nothing and both start. The common "X is already running, you try to
start another" case is fully covered.

## Generating deployment manifests: `calfcord deploy`

For the heavier tiers, `calfcord deploy <systemd|k8s|docker>` renders a
starting-point manifest from the same seam everything else uses — your
`agents/*.md` roster and `.env` broker address — and never inlines a
secret. Output goes to stdout by default; `-o PATH` writes a file:

```bash
calfcord deploy systemd                 # print a systemd unit
calfcord deploy k8s -o calfcord.yaml    # write reference K8s manifests
calfcord deploy docker                  # point at the shipped compose + per-agent override
```

What each target renders:

- **`systemd`** — a *real, correct-by-construction* unit for the
  single-host substrate. It runs the install **shim** (`<shim> start` /
  `<shim> stop`) as `Type=forking` — matching how `calfcord start`
  actually forks a detached supervisor and returns once the bridge is
  healthy — so the unit reuses the one seam that owns the `up` flags, the
  derived REST port, and the readiness gate rather than reconstructing
  them. It is the one artifact whose correctness is vouched for, yet it is
  still headed *reference* because the `User=` and paths vary per host:
  validate before `systemctl --user enable --now`.
- **`k8s`** — **reference** manifests (clearly annotated as such): a
  bundled broker workload + Service, a ConfigMap with the shared
  `CALF_HOST_URL`, and one Deployment per process type (bridge / tools) plus
  one per *defined* agent **and one per configured MCP server**
  (`calfkit-mcp <server>`) — each running a `calfkit-*` console script on the
  shipped image, dialing the shared broker. This is the Altitude-3 distributed
  shape, **not** `calfcord start` (there is no in-pod supervisor; each process
  type is its own workload). Secrets arrive via a `Secret` you create out of
  band (`kubectl create secret generic calfcord-secrets --from-env-file=.env`),
  never inlined. Tune replicas, resource limits, ingress, and durable broker
  storage per cluster.
- **`docker`** — the shipped `docker-compose.yml` is hand-tuned (Codex
  auth mounts, the A2A channel override, `depends_on` healthchecks), so
  regenerating it would lose that nuance. `deploy docker` therefore
  *points at the real file* and, when you have agents, emits an optional
  `compose.override.yml` snippet that splits the single all-in-one `agent`
  service into one `calfkit-agent <name>` service per agent (crash
  isolation), each inheriting the base build/env via `extends`. The Docker
  path does **not** auto-generate MCP services: the shipped
  `docker-compose.yml` carries a commented `mcp-<server>` template you
  uncomment per server (one `calfkit-mcp <server>` process each), or run
  those on a native / systemd host alongside the Docker broker.

`deploy` reads the install off disk and never talks to the running
supervisor, so it works whether or not the workspace is up. It does
require a native install (`$CALFCORD_HOME`) because the emitted manifests
reference the install's shim launcher and home paths.

The remaining sections are the **deep operator reference** for the
graduation: narrowing a host's tool surface, pinning a tool to one box,
running the same tool on several hosts, per-agent crash isolation,
mounting workspaces, and securing the broker. The mechanism throughout is
**environment variables on the canonical `calfcord:latest` image** (set at
`docker run`, in compose, in K8s, or in a bare-metal env) — there is no
separate per-tool or per-agent image to build.

## 1. Why split

Tools are location-transparent over Kafka. An agent declaring
`tools: [terminal]` can't tell whether the `calfkit-tools` process
answering on `tool.terminal.input` is on the same host or behind a
Tailscale exit node — calfkit's RPC layer abstracts the wire and
calfcord inherits the contract.

The killer use case is the **remote terminal tool**. On a remote dev box,
run the canonical image narrowed to just `terminal` —
`docker run -e CALFCORD_TOOLS_INCLUDE=terminal calfcord:latest calfkit-tools`
(or, on a bare-metal install, `CALFCORD_TOOLS_INCLUDE=terminal uv run
calfkit-tools`) — with that box's project bind-mounted at `/workspace`. Run
the bridge + agent on your laptop. When the agent's LLM runs
`git log` through `terminal`, the command executes against the remote box's
filesystem — the LLM never knew the difference, but the side effects landed
on a different machine.

When NOT to split: single-host deployments don't benefit. The shipped
`calfcord:latest` image hosts every tool and every agent on one
process; if you're on a laptop or a single VPS, `docker compose up` is
fine. Splitting only buys something when the tool surface needs to
reach resources on a different host than the agents.

## 2. Narrowing a tools host to a subset

There is no separate per-tool image to build. The canonical
`calfcord:latest` ships every tool, and you narrow what a given host
actually hosts at boot with the `CALFCORD_TOOLS_INCLUDE` env var — a
comma-separated allow-list applied by
`src/calfcord/tools/deploy_filters.py` (`apply_deploy_filters`) over the
explicit `ALL_TOOLS` surface. Unset means "host every tool"; set, the host
subscribes to only the `tool.<name>.input` topics it was told to keep. See
`docs/security.md` § Distributed deployments for the threat model that goes
with running tools on multiple hosts.

The basic shape — host only `terminal`:

```bash
docker run -d \
  --name calfcord-terminal \
  --restart unless-stopped \
  -e CALFCORD_TOOLS_INCLUDE=terminal \
  calfcord:latest calfkit-tools
```

The trailing `calfkit-tools` overrides the image's default `calfkit-bridge`
command so this container runs the tools process. On a bare-metal install
the equivalent narrows the same surface from the real process environment:

```bash
CALFCORD_TOOLS_INCLUDE=terminal uv run calfkit-tools
```

> **Read at import, not from in-process dotenv.** `CALFCORD_TOOLS_INCLUDE`
> (and `CALFCORD_TOOLS_ALIAS`, below) are consumed when
> `calfcord.tools` is *imported* — `TOOL_REGISTRY = apply_deploy_filters(...)`
> runs at module load, before the runner's own `load_dotenv()`. So they must
> be in the **real environment** of the process: pass them with
> `--env-file config/.env` (or `export` them, or `-e` on `docker run`), not
> a value the runner would have to read out of a dotenv file at runtime.
> Inside a container `env_file:`/`-e` already puts them in the environment,
> so the Docker path needs nothing extra.

Verify the registry contains only the tools you asked for:

```bash
docker run --rm -e CALFCORD_TOOLS_INCLUDE=terminal --entrypoint /bin/sh calfcord:latest \
  -c "python -c 'from calfcord.tools import TOOL_REGISTRY; print(sorted(TOOL_REGISTRY))'"
# ['terminal']
```

Host several tools by listing them:

```bash
docker run -d --restart unless-stopped \
  -e CALFCORD_TOOLS_INCLUDE=terminal,read_file,write_file,patch \
  calfcord:latest calfkit-tools
```

A typo in the allow-list that filters down to nothing fails fast — see
§ 8, Empty registry.

## 3. Deploying the same tool on multiple hosts

By default a tool's Kafka topic name is derived from its Python function
name — `patch` always subscribes to `tool.patch.input`. If you
deploy the `patch` worker on two hosts, both compete for messages
on the same topic and the broker round-robins between them: the agent
has no way to choose which host runs a given call.

The `CALFCORD_TOOLS_ALIAS=src=dst` env var lets you expose the same Python
tool body under a different schema name on a given host. The aliased clone
subscribes to `tool.<dst>.input` instead, so two hosts can serve `patch`
(one under its original name on a workstation, one aliased to `patch_eu` on
a remote VM) and agents can call either by picking the appropriate name.

**Manage it with the CLI.** Rather than hand-editing the env var, use
`calfcord tools alias` — it validates against the live tool surface (rejecting
an unknown or non-aliasable tool, a name collision, etc.) and writes the
install `.env` on the host you run it on:

```bash
calfcord tools alias add patch patch_eu   # run on EACH host that should know the name
calfcord tools alias list
calfcord tools alias remove patch_eu      # by the new name
```

Add `--restart` to apply it to a running workspace immediately (otherwise it
takes effect on the next start, since `.env` is read at boot). The rest of this
section shows the raw `CALFCORD_TOOLS_ALIAS` env var the CLI manages — the same
value you'd set directly in a Dockerfile `ENV`, a compose `environment:`, or a
k8s ConfigMap.

### Run the aliased host

On the remote VM, set both env vars on the canonical image:

```bash
docker run -d \
  --name calfcord-patch-eu \
  --restart unless-stopped \
  -e CALFCORD_TOOLS_ALIAS=patch=patch_eu \
  -e CALFCORD_TOOLS_INCLUDE=patch_eu \
  calfcord:latest calfkit-tools
```

What each env var does, both applied by `deploy_filters` at boot over
`ALL_TOOLS`:

- `CALFCORD_TOOLS_ALIAS=patch=patch_eu` — clones the `patch` `ToolNodeDef`
  under the new name with all four name-bound fields rewritten
  (`tool_schema.name`, `subscribe_topics`, `publish_topic`, `node_id`).
- `CALFCORD_TOOLS_INCLUDE=patch_eu` — the include filter is applied *after*
  alias expansion, so it drops the original and this host subscribes ONLY
  to `tool.patch_eu.input`. Without it, the host would subscribe to BOTH
  topics and race with the workstation on `tool.patch.input`.

(Aliasing requires a stateless tool body. A tool that registers node-scoped
resources or lifecycle hooks — only `todo` today — can't be cloned under a
second wire identity and `deploy_filters` raises rather than build a broken
clone.)

### Configure the agent host

For an agent to call `patch_eu`, that name must exist in the
agent host's `TOOL_REGISTRY`. Set the same alias on the agent host —
`calfcord tools alias add patch patch_eu` (or the raw env directly):

```env
# .env on the agent host
CALFCORD_TOOLS_ALIAS=patch=patch_eu
```

The agent host doesn't have an include filter, so both names land in
its registry. An agent can now declare either or both in its
frontmatter:

```yaml
---
name: scribe
...
tools: [patch, patch_eu, terminal]
---
```

The LLM picks `patch` to operate on workstation files; it picks
`patch_eu` for the EU VM. Routing happens at the broker — the
agent's process publishes to `tool.<name>.input` and the matching
worker (whichever host subscribed to that topic) services the call.

### v1 limitations

- **Tool descriptions match between original and clone.** The LLM
  differentiates by name suffix and by whatever routing instructions
  you write into the calling agent's system prompt
  (e.g. "when the user asks about EU files, prefer `patch_eu`").
  An operator-controllable description override is a clean follow-on
  if real usage shows this needs more signal.
- **One alias per source, one source per target.** The parser
  enforces this; multi-region from a single image isn't supported
  yet.
- **No transitive chains.** `patch=tmp,tmp=patch_eu` does not
  chase the chain — only `tmp` would be registered as a clone of
  `patch`, not `patch_eu`.

## 4. Per-agent crash isolation

The all-in-one `calfkit-agent` Worker hosts every agent in one process,
which is fine for most deployments. If you want a misbehaving agent to be
able to restart without touching its peers, split that single process into
one per agent — all on the same `calfcord:latest` image, no custom build.

`calfcord deploy docker` emits exactly this as a `compose.override.yml`
snippet: one `calfkit-agent <name>` service per defined agent, each
inheriting the base build/env from the shipped compose via `extends`. Run
each with `restart: unless-stopped` so the supervisor recovers them
independently.

```bash
calfcord deploy docker            # points at the real compose + prints the per-agent override
```

Bare metal, the equivalent is running one `calfkit-agent` process per agent
host rather than one Worker hosting all of them; every process dials the
same broker.

## 4a. Running MCP servers on a separate host

MCP servers split cleanly because of their **secrets boundary**: only the
`mcp-<server>` processes read `mcp.json` and hold the credentials. Agents
resolve their `mcp/...` selectors from the broker's `mcp.capabilities` view, so
an agent host needs **no `mcp.json` and no MCP secrets** — just the selector
string in the agent's `.md`. A common shape is one host that owns all the MCP
servers (and their tokens) and agents anywhere else.

**MCP host** — where `mcp.json` and its secrets live:

```bash
calfcord self set-broker laptop:9092
calfcord start                                  # this host can be the substrate, or just dial the shared broker
calfcord mcp add github \
  --command "npx -y @modelcontextprotocol/server-github" --env GITHUB_TOKEN
calfcord stop && calfcord start                 # declare the new mcp-github slot (added after start)
calfcord mcp start github                       # toolbox connects + advertises on the bus
```

Set `GITHUB_TOKEN` in *this* host's `config/.env` only. The toolbox advertises
the github server's tools onto `mcp.capabilities`, reachable org-wide.

**Agent host** — no `mcp.json`, no `GITHUB_TOKEN`:

```bash
# scribe.md already declares  tools: [..., mcp/github]
calfcord agent restart scribe                   # picks the tools up from the advertisement
```

Because the capability view is org-wide, `calfcord agent tools` on the agent
host still surfaces the github server's live per-tool rows even though this host
runs no MCP server. The toolbox is an ordinary calfkit node, so running the same
server on two hosts makes them competing consumers on one dispatch topic — a
legitimate scale-out, not the agent split-brain the duplicate-name guard
prevents. See [`mcp-tools.md`](./mcp-tools.md) for the full MCP story.

## 5. Worked example: remote terminal tool

Two hosts: **laptop** runs broker + bridge + agent; **builder**
(a remote dev box) runs only the terminal tool against its own project
directory.

**Network prereq.** Builder needs to reach the broker. For trusted
hosts, run **Tailscale** or **WireGuard** between them — broker stays
unauthenticated, the overlay is the perimeter. For public-internet
exposure, require client auth + TLS (see § 6). This walkthrough assumes
Tailscale with hostnames `laptop` and `builder`.

**Laptop side.** Set the broker to its tailnet hostname so builder can
reach it, then open the workspace (substrate) and clock in everything
EXCEPT the all-in-one tools — the remote box owns `terminal` (see § 10 for
excluding it from a local tools process if you keep one):

```bash
calfcord self set-broker laptop:9092   # advertise the broker on the tailnet
calfcord start                          # broker + bridge (substrate)
calfcord agent start scribe             # the agent
```

The native broker started by `calfcord start` advertises the address you
set in `CALF_HOST_URL`, so off-box clients on the tailnet resolve
`laptop:9092`. (If you run the broker under Docker Compose instead, the
equivalent is `TANSU_ADVERTISE=laptop docker compose up -d tansu bridge
agent` — the `TANSU_ADVERTISE` override makes Tansu advertise
`laptop:9092` instead of the in-network default `tansu:9092`.)

**Remote host.** On builder, with the project directory at
`/home/me/my-project`:

```bash
docker run -d \
  --name calfcord-terminal \
  --restart unless-stopped \
  -e CALF_HOST_URL=laptop:9092 \
  -e CALFCORD_WORKSPACE_DIR=/workspace \
  -e CALFCORD_TOOLS_INCLUDE=terminal \
  -v /home/me/my-project:/workspace \
  calfcord:latest calfkit-tools
```

The tools process needs **no Discord credentials**: since the calfkit-012
migration it is pure compute on the Kafka wire — it opens no Discord
connection and no longer boots an `A2AChannelResolver` (the A2A audit
channel is bridge-hosted now; see `src/calfcord/tools/runner.py`).

**Verify.** In Discord: `` @scribe please run `pwd && uname -a` via terminal ``.
The reply should contain builder's hostname and `/workspace` — proving
the call landed on the remote box. If you see laptop's hostname, you
have a duplicate `terminal` consumer; see § 10.

## 6. Broker authentication

The default Tansu broker runs with **memory storage, no authentication,
no TLS** (see `docker-compose.yml`). Fine on localhost or a trusted
overlay. NOT fine on the public internet.

**Trusted overlay (recommended for small teams).** Tailscale or
WireGuard between every host. The broker stays unauthenticated; the
overlay is the perimeter. No auth config, no certs to rotate. Configure
the broker to advertise its tailnet address rather than the in-network
default via the `TANSU_ADVERTISE` env the compose `tansu` service reads:

```bash
# advertise the broker on its tailnet hostname instead of `tansu`:
TANSU_ADVERTISE=laptop docker compose up -d tansu
```

**Client auth + TLS.** Broker exposed on the public internet, gated by
auth. Tansu offers `--authentication` (require client auth), TLS via
`--cert` / `--key`, and `tansu user create` for users. Production-grade
SASL/ACL hardening with Tansu is still maturing (it is a newer broker),
so follow [Tansu's docs](https://docs.tansu.io/) and the
[upstream repo](https://github.com/tansu-io/tansu) for the current,
canonical setup rather than a fixed command set here. Two
calfcord-specific notes:

- **The Kafka client is `aiokafka`** (via calfkit). The standard env
  vars (`KAFKA_SASL_MECHANISM`, `KAFKA_SASL_USERNAME`,
  `KAFKA_SASL_PASSWORD`, plus TLS cert paths) are the upstream contract.

  > **Planned enhancement — not yet wired.** Broker authentication
  > (SASL/SCRAM + TLS) is **not plumbed through calfcord today**.
  > `Client.connect` already accepts a `security=` kwarg, but the
  > calfcord runners only forward `CALF_HOST_URL` and never pass
  > credentials. So at present every cross-host / cross-env split
  > requires a broker URL reachable **without auth** — keep it on a
  > private network or VPC (the trusted-overlay path above), never the
  > raw public internet. Threading SASL/TLS credentials through the
  > runners is tracked as a fast-follow; file a calfkit issue describing
  > what you need.
- **Broker auth IS the perimeter.** Anyone who can publish to
  `tool.<name>.input` can invoke that tool with arbitrary arguments.
  Rotate broker credentials like you rotate the Discord bot token.

For most ops teams on shared infra, the overlay path is the lazier-but-
secure default; SASL is the option when overlay networking isn't
available.

## 7. Workspace mount semantics

A narrowed tools host consumes the same `CALFCORD_WORKSPACE_DIR` env var as
the all-in-one image. Bind-mount the project root into that path:

```bash
docker run -e CALFCORD_TOOLS_INCLUDE=terminal \
           -e CALFCORD_WORKSPACE_DIR=/workspace \
           -v /home/me/my-project:/workspace \
           calfcord:latest calfkit-tools
```

Once mounted, `read_file("foo.py")` inside the container operates on
`/workspace/foo.py`, which is `/home/me/my-project/foo.py` on the host.
Same path-resolution semantics as the default compose deployment (see
`docs/security.md` § 1).

Caveats:

- **UID alignment matters on Linux.** The shipped Dockerfile runs as
  UID 1000; if the host directory is owned by a different UID, the
  tool process sees permission errors on writes. Rebuild with
  `--build-arg UID=$(id -u) GID=$(id -g)` or chown to match. macOS
  Docker Desktop handles this automatically.
- **Read/write reaches everything under the mount.** The `terminal` tool
  can `rm -rf /workspace`. Pick the mount target deliberately.
- **Symlinks follow.** A symlink inside the workspace that points to
  `$HOME/.ssh` will be dereferenced inside the container.

## 8. Failure modes

**Remote tool host down.** Tools currently have **no default per-call
timeout** at the calfkit layer. If a tool's host is down, the calling
agent's tool call blocks until the broker drops the connection or the
operator restarts the agent. Mitigation: enforce a deadline inside the call
itself where the tool exposes one (the vendored `terminal` tool takes a
`timeout` argument the LLM can pass), or rely on Docker / supervisor
health-restart of the calling agent process. A future calfkit release may
add a default per-tool-call timeout; track that upstream.

(Agent-to-agent messaging is no longer a tool: since the calfkit-012
migration A2A is calfkit's native `message_agent`, invoked inside the agent
runtime and projected by the bridge — not the deleted first-party
`private_chat` tool — so there is no `CALFKIT_TOOLS_TIMEOUT_SECONDS` knob
any more.)

The bridge and agent processes themselves stay up — only the in-flight
call hangs.

**Broker partition / network blip.** Standard Kafka semantics; aiokafka
reconnects automatically. In-flight tool calls remain blocked until
reconnect unless the tool body imposes its own deadline.

**Multiple tool hosts on the same `tool.<name>.input` topic.** Kafka
consumer-group semantics: each partition delivers to exactly one
consumer in the group. With calfcord's single-partition default,
exactly one consumer receives each call — effectively random per call.
To intentionally scale horizontally, bump the partition count; to pin
a tool to one host, see § 10.

**Empty registry.** A tools host whose `CALFCORD_TOOLS_INCLUDE`
filters down to nothing fails fast in `runner.py` with `TOOL_REGISTRY
is empty; nothing to host`. Separately, the agent boot hard-fails with
a "known tools: …" error if its `tools:` list references a name not in
its agent-side registry — that's an agent-side check, not a tool-side
one.

## 9. What changes when you split

Almost nothing, by design.

- **Agent definitions stay identical.** An agent with
  `tools: [terminal, web_fetch]` works the same whether the tools run in
  one process, on two narrowed hosts, or in the all-in-one
  `calfcord:latest`. Kafka topic names don't change.
- **Per-host log isolation.** A misbehaving tool host can crash and
  restart without taking down the others; compose's
  `restart: unless-stopped` recovers each independently.
- **Independent rollout.** Ship a new `terminal` by restarting only the
  tools host that serves it — the bridge never read the tool code, so it
  needs no restart.
- **No tool-authoring change.** Narrowing/aliasing change deployment
  topology, not the contract. `docs/authoring-tools.md`'s rules (the
  `agent_tool` decorator, the `"error: "` discriminator, the
  `RuntimeError` boundary) are unchanged.

## 10. Combining with all-in-one

You can run the full `calfcord:latest` tools host on host A AND a host B
narrowed to `terminal` (`CALFCORD_TOOLS_INCLUDE=terminal`) at the same time.
Kafka load-balances calls to `tool.terminal.input` between them via
consumer-group semantics — but
because each call lands on exactly ONE of the two consumers, the
effective behavior is "terminal runs on either A or B, randomly per call".
This is almost never what you want — and for the stateful tools
(`terminal`, `process`, `todo`, `execute_code`, in-flight file edits) it
is actively wrong: their per-agent session state is in-memory on whichever
replica served the last call, so splitting them across hosts fragments an
agent's session (see [`security.md`](./security.md) § 1.1).

To pin `terminal` to the remote host:

```bash
# On host A (the all-in-one), exclude terminal from the tool registry:
docker run -e CALFCORD_TOOLS_INCLUDE=process,read_file,write_file,patch,search_files,todo,execute_code,web_search,web_extract,web_fetch \
           calfcord:latest calfkit-tools
```

Now host A serves every tool *except* `terminal`; host B serves `terminal`
only; each tool has exactly one host responsible for it. This is the
intended distributed shape.

The same `CALFCORD_TOOLS_INCLUDE` env var is consumed by both the slim
images (where it's baked into the Dockerfile's `ENV`) and the all-in-one
image (where you set it at `docker run` time). The semantics are
identical: `deploy_filters` narrows the explicit `ALL_TOOLS` surface down
to the listed names.

For the broader security model that goes with running tools on multiple
hosts, see `docs/security.md` § Distributed deployments. For the
tool-authoring contract those tools still need to follow, see
`docs/authoring-tools.md`.
