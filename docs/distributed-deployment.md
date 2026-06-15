# Distributed Deployments — Altitude-3 Graduation

Going multi-host is a **graduation, not a rewrite**. Nothing you built on
one machine changes: the same `agents/*.md`, the same `.env`, the same
`calfcord start` / `calfcord agent start <name>` commands all keep
working. The single thing that moves is where the broker lives — every
process just dials a remote `CALF_HOST_URL` instead of a local one.

That is the whole graduation. The rest of this page is the deep operator
reference for the heavier shapes (per-tool images, per-agent crash
isolation, broker auth) you reach for *after* the basic multi-host split
is running.

## The portable invariant

What makes calfcord portable across one host or twenty is **not** the
Process Compose YAML the single-host supervisor generates. That YAML is
host-local derived state — `calfcord start` regenerates it from your
config and you never edit it (see
[`architecture.md`](architecture.md#running-modes)). It does not travel.

What *does* travel — the contract every process agrees on regardless of
which box it runs on — is three things:

- **The Kafka wire.** Topic names are derived from agent and tool names
  (`tool.terminal.input`, `agent.scribe.in`, …), so any process on any host
  finds its peers by dialing the same broker. This is the seam.
- **`.env`.** The shared `CALF_HOST_URL` (broker address) plus the
  Discord credentials. Identical on every host except `CALF_HOST_URL`,
  which on the broker's own host may be `localhost:9092` and elsewhere is
  the broker's reachable address.
- **`agents/*.md`.** An agent's identity and instructions are the same
  file wherever it runs; the bridge learns about it from a runtime ping,
  never from a shared filesystem.

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
   calfcord router start           # ...or the receptionist
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
the broker (the control-plane `probe_live_roster` call reads the
`agent.state` topic org-wide) and refuses to start `<name>` if it is
already live on *any* host — printing "agent 'X' is already running in the
organization" instead of starting a duplicate. This is what stops a
double-reply / split-brain when two hosts each try to run `scribe`.

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
  `CALF_HOST_URL`, and one Deployment per process type (bridge / router /
  tools) plus one per *defined* agent **and one per configured MCP server**
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
  path does **not** yet cover MCP servers — run those on a native or systemd
  host alongside the Docker broker, or add `calfkit-mcp <server>` services to
  your compose file by hand.

`deploy` reads the install off disk and never talks to the running
supervisor, so it works whether or not the workspace is up. It does
require a native install (`$CALFCORD_HOME`) because the emitted manifests
reference the install's shim launcher and home paths.

The remaining sections are the **deep operator reference** for the
graduation: splitting tools across hosts with slim images, pinning a tool
to one box, mounting workspaces, and securing the broker.

## 1. Why split

Tools are location-transparent over Kafka. An agent declaring
`tools: [terminal]` can't tell whether the `calfkit-tools` process
answering on `tool.terminal.input` is on the same host or behind a
Tailscale exit node — calfkit's RPC layer abstracts the wire and
calfcord inherits the contract.

The killer use case is the **remote terminal tool**. Run
`calfcord-package-tools terminal` on a remote dev box with that box's
project bind-mounted at `/workspace`. Run the bridge + router + agent
on your laptop. When the agent's LLM runs `git log` through `terminal`,
the command executes against the remote box's filesystem — the LLM never
knew the difference, but the side effects landed on a different
machine.

When NOT to split: single-host deployments don't benefit. The shipped
`calfcord:latest` image hosts every tool and every agent on one
process; if you're on a laptop or a single VPS, `docker compose up` is
fine. Splitting only buys something when the tool surface needs to
reach resources on a different host than the agents.

## 2. Building a per-tool image

The two packaging CLIs (`calfcord-package-tools`,
`calfcord-package-agents`) are local-only — they build images, they don't
push them. Push to whichever registry you want via plain `docker push`
after the build. See `docs/security.md` § Distributed deployments for the
threat model that goes with running tools on multiple hosts.

The basic shape:

```bash
uv run calfcord-package-tools terminal --tag my-terminal:1.0
```

That writes a slim Dockerfile that narrows the hosted tool surface via
the `CALFCORD_TOOLS_INCLUDE=terminal` env var (applied at boot by
`src/calfcord/tools/deploy_filters.py` over the explicit `ALL_TOOLS`
list) and installs only the OS deps that `terminal` actually needs (none
beyond the always-on `ca-certificates` + `git` — the hermes terminal
uses bash + a PTY, so no `tmux`). The image is materially smaller than
`calfcord:latest`:

```bash
docker image inspect my-terminal:1.0 --format '{{.Size}}'
```

Verify the resulting registry contains only the tools you asked for:

```bash
docker run --rm --entrypoint /bin/sh my-terminal:1.0 \
  -c "python -c 'from calfcord.tools import TOOL_REGISTRY; print(sorted(TOOL_REGISTRY))'"
# ['terminal']
```

OS-dep slimming is per-tool: `search_files` brings `ripgrep`; `terminal`
and `execute_code` need nothing beyond the always-on `ca-certificates` +
`git` (no `tmux` anymore). Other tools bring nothing extra.

Multiple tools in one image:

```bash
uv run calfcord-package-tools terminal read_file write_file patch \
  --tag my-coding-tools:1.0
```

To inspect the generated Dockerfile without building anything, pass
`--dry-run`:

```bash
uv run calfcord-package-tools terminal --tag my-terminal:1.0 --dry-run
```

NOTE: v1 does no Python-dep slimming. Every generated image installs the
full `pyproject.toml` deps. The size win comes from OS-dep slimming and
from skipping unused tool *code paths* at runtime, not from a smaller
Python wheel set.

## 3. Deploying the same tool on multiple hosts

By default a tool's Kafka topic name is derived from its Python function
name — `patch` always subscribes to `tool.patch.input`. If you
deploy the `patch` worker on two hosts, both compete for messages
on the same topic and the broker round-robins between them: the agent
has no way to choose which host runs a given call.

The `--rename SRC=DST` flag on `calfcord-package-tools` lets you expose
the same Python tool body under a different schema name in the
resulting image. The renamed tool subscribes to `tool.<DST>.input`
instead, so two hosts can serve `patch` (one under its original
name on a workstation, one renamed to `patch_eu` on a remote VM)
and agents can call either by picking the appropriate name.

### Build the renamed image

```bash
uv run calfcord-package-tools patch \
  --rename patch=patch_eu \
  --tag patch-eu:1.0
```

The positional name (`patch`) tells the build which Python tool
body to include; `--rename` renames it in the resulting image. The
generated Dockerfile bakes two env vars:

- `CALFCORD_TOOLS_ALIAS=patch=patch_eu` — `deploy_filters` (the boot-time
  transform over `ALL_TOOLS`) clones the `patch` `ToolNodeDef` under the
  new name with all four name-bound fields rewritten (`tool_schema.name`,
  `subscribe_topics`, `publish_topic`, `node_id`).
- `CALFCORD_TOOLS_INCLUDE=patch_eu` — the filter drops the
  original so this host subscribes ONLY to `tool.patch_eu.input`.
  Without this, the host would subscribe to BOTH topics and race
  with the workstation on `tool.patch.input`.

The CLI translates positional names through the alias map before
baking `CALFCORD_TOOLS_INCLUDE`, so typing `patch` (the on-disk
source name) produces the correct filter for the post-rename name —
no operator math required.

### Configure the agent host

For an agent to call `patch_eu`, that name must exist in the
agent host's `TOOL_REGISTRY`. Set the same alias env on the agent
host:

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

## 4. Building a per-agent image

```bash
uv run calfcord-package-agents scribe --tag my-scribe:1.0
```

The generated Dockerfile `COPY`s only `agents/scribe.md` instead of the
full `agents/` directory. The selected agents are wired into the same
`calfkit-agent` entrypoint as the all-in-one image.

This is mostly useful for crash isolation per agent (a misbehaving
agent's container can restart without touching its peers). Most
deployments don't need it — the all-in-one `calfkit-agent` Worker hosts
every agent in one process without trouble. If you do split, list each
agent's image as its own compose service with `restart: unless-stopped`
so the supervisor recovers them independently.

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

Two hosts: **laptop** runs broker + bridge + router + agent; **builder**
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
calfcord router start                   # the receptionist (optional)
```

The native broker started by `calfcord start` advertises the address you
set in `CALF_HOST_URL`, so off-box clients on the tailnet resolve
`laptop:9092`. (If you run the broker under Docker Compose instead, the
equivalent is `TANSU_ADVERTISE=laptop docker compose up -d tansu bridge
router agent` — the `TANSU_ADVERTISE` override makes Tansu advertise
`laptop:9092` instead of the in-network default `tansu:9092`.)

**Remote host.** On builder, with the project directory at
`/home/me/my-project`:

```bash
docker run -d \
  --name calfcord-terminal \
  --restart unless-stopped \
  -e CALF_HOST_URL=laptop:9092 \
  -e DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN \
  -e DISCORD_APPLICATION_ID=$DISCORD_APPLICATION_ID \
  -e DISCORD_GUILD_ID=$DISCORD_GUILD_ID \
  -e CALFCORD_WORKSPACE_DIR=/workspace \
  -v /home/me/my-project:/workspace \
  my-terminal:1.0
```

The Discord env vars are required because `calfkit-tools` boots an
`A2AChannelResolver` for the audit channel even when no A2A tool is
hosted (see `src/calfcord/tools/runner.py`).

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

Per-tool images consume the same `CALFCORD_WORKSPACE_DIR` env var as
the all-in-one image. Bind-mount the project root into that path:

```bash
docker run -e CALFCORD_WORKSPACE_DIR=/workspace \
           -v /home/me/my-project:/workspace \
           my-terminal:1.0
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

**Remote tool host down.** Behavior depends on which tool the agent
invoked:

- **`private_chat`** has a 60-second default A2A timeout (overridable
  via `CALFKIT_TOOLS_TIMEOUT_SECONDS`). When the target agent is
  unreachable, the caller's LLM gets an `error: target 'X' did not
  reply within 60s` string and can adapt.
- **Every other tool** (`terminal`, `read_file`, etc.) currently
  has **no default per-call timeout** at the calfkit layer. If the
  tool's host is down, the calling agent's `execute` RPC blocks
  until the broker drops the connection or the operator restarts the
  agent. Mitigation: enforce a deadline inside the call itself where the
  tool exposes one (the vendored `terminal` tool takes a `timeout`
  argument the LLM can pass), or rely on Docker / supervisor
  health-restart of the calling agent process. A future calfkit release
  may add a default per-tool-call timeout; track that upstream.

In either case, the bridge and agent processes themselves stay up —
only the in-flight call hangs.

**Broker partition / network blip.** Standard Kafka semantics; aiokafka
reconnects automatically. `private_chat` calls in flight time out per
the 60s rule above; other tool calls remain blocked until reconnect
unless the tool body imposes its own deadline.

**Multiple tool hosts on the same `tool.<name>.input` topic.** Kafka
consumer-group semantics: each partition delivers to exactly one
consumer in the group. With calfcord's single-partition default,
exactly one consumer receives each call — effectively random per call.
To intentionally scale horizontally, bump the partition count; to pin
a tool to one host, see § 10.

**Empty registry.** A per-tool image where `CALFCORD_TOOLS_INCLUDE`
filters down to nothing fails fast in `runner.py` with `TOOL_REGISTRY
is empty; nothing to host`. Separately, the agent boot hard-fails with
a "known tools: …" error if its `tools:` list references a name not in
its agent-side registry — that's an agent-side check, not a tool-side
one.

## 9. What changes when you split

Almost nothing, by design.

- **Agent definitions stay identical.** An agent with
  `tools: [terminal, web_fetch]` works the same whether the tools are in
  one image, in two images on two hosts, or in the all-in-one
  `calfcord:latest`. Kafka topic names don't change.
- **Per-tool log isolation.** A misbehaving tool can crash its own
  container without taking down the others; compose's
  `restart: unless-stopped` recovers each independently.
- **Independent rollout.** Ship a new `terminal` by redeploying only
  the `my-terminal` image — the bridge never read the tool code, so it
  needs no restart.
- **No tool-authoring change.** The packaging CLIs change deployment
  topology, not the contract. `docs/authoring-tools.md`'s rules (the
  `agent_tool` decorator, the `"error: "` discriminator, the
  `RuntimeError` boundary) are unchanged.

## 10. Combining with all-in-one

You can run `calfcord:latest` on host A AND a per-tool image like
`my-terminal:1.0` on host B at the same time. Kafka load-balances calls to
`tool.terminal.input` between them via consumer-group semantics — but
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
docker run -e CALFCORD_TOOLS_INCLUDE=process,read_file,write_file,patch,search_files,todo,execute_code,web_search,web_extract,web_fetch,private_chat \
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
