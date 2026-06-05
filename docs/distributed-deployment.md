# Distributed Deployments

How to split calfcord across multiple hosts by building slim per-tool or
per-agent images and pointing them at a shared Kafka broker. This is the
operator reference for the packaging CLIs introduced in PR 5; for the
single-host default, the [README quick start](../README.md#quick-start)
and [`architecture.md`](architecture.md#running-modes) still apply.

The two CLIs are local-only — they build images, they don't push them.
Operators push to whichever registry they want via plain `docker push`
after the build. See `docs/security.md` § Distributed deployments for
the threat model that goes with this.

## 1. Why split

Tools are location-transparent over Kafka. An agent declaring
`tools: [shell]` can't tell whether the `calfkit-tools` process
answering on `tool.shell.input` is on the same host or behind a
Tailscale exit node — calfkit's RPC layer abstracts the wire and
calfcord inherits the contract.

The killer use case is the **remote shell tool**. Run
`calfcord-package-tools shell` on a remote dev box with that box's
project bind-mounted at `/workspace`. Run the bridge + router + agent
on your laptop. When the agent's LLM calls `shell("git log")`, the
command executes against the remote box's filesystem — the LLM never
knew the difference, but the side effects landed on a different
machine.

When NOT to split: single-host deployments don't benefit. The shipped
`calfcord:latest` image hosts every tool and every agent on one
process; if you're on a laptop or a single VPS, `docker compose up` is
fine. Splitting only buys something when the tool surface needs to
reach resources on a different host than the agents.

## 2. Building a per-tool image

The basic shape:

```bash
uv run calfcord-package-tools shell --tag my-shell:1.0
```

That writes a slim Dockerfile that filters tool discovery via the
`CALFCORD_TOOLS_INCLUDE=shell` env var (consumed by
`src/calfcord/tools/discovery.py`) and installs only the
OS deps that `shell` actually needs (`tmux` for persistent sessions,
plus the always-on `ca-certificates`). The image is materially smaller
than `calfcord:latest`:

```bash
docker image inspect my-shell:1.0 --format '{{.Size}}'
```

Verify the resulting registry contains only the tools you asked for:

```bash
docker run --rm --entrypoint /bin/sh my-shell:1.0 \
  -c "python -c 'from calfcord.tools import TOOL_REGISTRY; print(sorted(TOOL_REGISTRY))'"
# ['shell']
```

OS-dep slimming is per-tool: `shell` brings `tmux`; `grep` and `glob`
bring `ripgrep`; web tools (`web_fetch`, `web_search`) bring
`ca-certificates` (which is always on regardless of which tools you
pick). Other builtins bring nothing extra.

Multiple tools in one image:

```bash
uv run calfcord-package-tools shell read_file write_file edit_file \
  --tag my-coding-tools:1.0
```

To inspect the generated Dockerfile without building anything, pass
`--dry-run`:

```bash
uv run calfcord-package-tools shell --tag my-shell:1.0 --dry-run
```

NOTE: v1 does no Python-dep slimming. Every generated image installs the
full `pyproject.toml` deps. The size win comes from OS-dep slimming and
from skipping unused tool *code paths* at runtime, not from a smaller
Python wheel set.

## 3. Deploying the same tool on multiple hosts

By default a tool's Kafka topic name is derived from its Python function
name — `edit_file` always subscribes to `tool.edit_file.input`. If you
deploy the `edit_file` worker on two hosts, both compete for messages
on the same topic and the broker round-robins between them: the agent
has no way to choose which host runs a given call.

The `--rename SRC=DST` flag on `calfcord-package-tools` lets you expose
the same Python tool body under a different schema name in the
resulting image. The renamed tool subscribes to `tool.<DST>.input`
instead, so two hosts can serve `edit_file` (one under its original
name on a workstation, one renamed to `edit_file_eu` on a remote VM)
and agents can call either by picking the appropriate name.

### Build the renamed image

```bash
uv run calfcord-package-tools edit_file \
  --rename edit_file=edit_file_eu \
  --tag edit-file-eu:1.0
```

The positional name (`edit_file`) tells the build which Python tool
body to include; `--rename` renames it in the resulting image. The
generated Dockerfile bakes two env vars:

- `CALFCORD_TOOLS_ALIAS=edit_file=edit_file_eu` — the discovery loader
  clones the `edit_file` `ToolNodeDef` under the new name with all
  four name-bound fields rewritten (`tool_schema.name`,
  `subscribe_topics`, `publish_topic`, `node_id`).
- `CALFCORD_TOOLS_INCLUDE=edit_file_eu` — the filter drops the
  original so this host subscribes ONLY to `tool.edit_file_eu.input`.
  Without this, the host would subscribe to BOTH topics and race
  with the workstation on `tool.edit_file.input`.

The CLI translates positional names through the alias map before
baking `CALFCORD_TOOLS_INCLUDE`, so typing `edit_file` (the on-disk
source name) produces the correct filter for the post-rename name —
no operator math required.

### Configure the agent host

For an agent to call `edit_file_eu`, that name must exist in the
agent host's `TOOL_REGISTRY`. Set the same alias env on the agent
host:

```env
# .env on the agent host
CALFCORD_TOOLS_ALIAS=edit_file=edit_file_eu
```

The agent host doesn't have an include filter, so both names land in
its registry. An agent can now declare either or both in its
frontmatter:

```yaml
---
name: scribe
...
tools: [edit_file, edit_file_eu, shell]
---
```

The LLM picks `edit_file` to operate on workstation files; it picks
`edit_file_eu` for the EU VM. Routing happens at the broker — the
agent's process publishes to `tool.<name>.input` and the matching
worker (whichever host subscribed to that topic) services the call.

### v1 limitations

- **Tool descriptions match between original and clone.** The LLM
  differentiates by name suffix and by whatever routing instructions
  you write into the calling agent's system prompt
  (e.g. "when the user asks about EU files, prefer `edit_file_eu`").
  An operator-controllable description override is a clean follow-on
  if real usage shows this needs more signal.
- **One alias per source, one source per target.** The parser
  enforces this; multi-region from a single image isn't supported
  yet.
- **No transitive chains.** `edit_file=tmp,tmp=edit_file_eu` does not
  chase the chain — only `tmp` would be registered as a clone of
  `edit_file`, not `edit_file_eu`.

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

## 5. Worked example: remote shell tool

Two hosts: **laptop** runs broker + bridge + router + agent; **builder**
(a remote dev box) runs only the shell tool against its own project
directory.

**Network prereq.** Builder needs to reach the broker. For trusted
hosts, run **Tailscale** or **WireGuard** between them — broker stays
unauthenticated, the overlay is the perimeter. For public-internet
exposure, use SASL/SCRAM + TLS (see § 6). This walkthrough assumes
Tailscale with hostnames `laptop` and `builder`.

**Laptop side.** Bring up the broker plus everything EXCEPT the
all-in-one tools (the remote box owns `shell`; see § 10 for excluding
it from the local tools process if you keep one):

```bash
docker compose up -d redpanda bridge router agent
```

Redpanda's external listener is already published on `:19092`. Verify
from builder: `rpk cluster info --brokers=laptop:19092`.

**Remote host.** On builder, with the project directory at
`/home/me/my-project`:

```bash
docker run -d \
  --name calfcord-shell \
  --restart unless-stopped \
  -e CALF_HOST_URL=laptop:19092 \
  -e DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN \
  -e DISCORD_APPLICATION_ID=$DISCORD_APPLICATION_ID \
  -e DISCORD_GUILD_ID=$DISCORD_GUILD_ID \
  -e CALFCORD_WORKSPACE_DIR=/workspace \
  -v /home/me/my-project:/workspace \
  my-shell:1.0
```

The Discord env vars are required because `calfkit-tools` boots an
`A2AChannelResolver` for the audit channel even when no A2A tool is
hosted (see `src/calfcord/tools/runner.py`).

**Verify.** In Discord: `` @scribe please run `pwd && uname -a` via shell ``.
The reply should contain builder's hostname and `/workspace` — proving
the call landed on the remote box. If you see laptop's hostname, you
have a duplicate `shell` consumer; see § 10.

## 6. Broker authentication

The default Redpanda runs with `--mode=dev-container` (see
`docker-compose.yml`) — **no authentication, no TLS**. Fine on
localhost or a trusted overlay. NOT fine on the public internet.

**Trusted overlay (recommended for small teams).** Tailscale or
WireGuard between every host. The broker stays unauthenticated; the
overlay is the perimeter. No SASL config, no certs to rotate. Configure
the broker to advertise its tailnet address rather than `localhost`:

```yaml
# in docker-compose.yml's redpanda command, swap external advertise:
- --advertise-kafka-addr=internal://redpanda:9092,external://laptop:19092
```

**SASL/SCRAM + TLS.** Broker exposed on the public internet, gated by
auth. Redpanda configures this via `rpk cluster config set` plus
`rpk acl user create` — see the
[Redpanda security docs](https://docs.redpanda.com/current/manage/security/authentication/)
for the canonical setup. Two calfcord-specific notes:

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
           my-shell:1.0
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
- **Read/write reaches everything under the mount.** The shell tool
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
- **Every other builtin tool** (`shell`, `read_file`, etc.) currently
  has **no default per-call timeout** at the calfkit layer. If the
  tool's host is down, the calling agent's `execute_node` RPC blocks
  until the broker drops the connection or the operator restarts the
  agent. Mitigation: enforce a deadline inside the tool body itself
  (e.g. `shell`'s upstream openhands executor accepts a `timeout`
  arg), or rely on Docker / supervisor health-restart of the calling
  agent process. A future calfkit release may add a default
  per-tool-call timeout; track that upstream.

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
  `tools: [shell, web_fetch]` works the same whether the tools are in
  one image, in two images on two hosts, or in the all-in-one
  `calfcord:latest`. Kafka topic names don't change.
- **Per-tool log isolation.** A misbehaving tool can crash its own
  container without taking down the others; compose's
  `restart: unless-stopped` recovers each independently.
- **Independent rollout.** Ship a new `shell` by redeploying only
  the `my-shell` image — the bridge never read the tool code, so it
  needs no restart.
- **No tool-authoring change.** The packaging CLIs change deployment
  topology, not the contract. `docs/authoring-tools.md`'s rules (the
  `agent_tool` decorator, the `"error: "` discriminator, the
  `RuntimeError` boundary) are unchanged.

## 10. Combining with all-in-one

You can run `calfcord:latest` on host A AND a per-tool image like
`my-shell:1.0` on host B at the same time. Kafka load-balances calls to
`tool.shell.input` between them via consumer-group semantics — but
because each call lands on exactly ONE of the two consumers, the
effective behavior is "shell runs on either A or B, randomly per call".
This is almost never what you want.

To pin `shell` to the remote host:

```bash
# On host A (the all-in-one), exclude shell from the tool registry:
docker run -e CALFCORD_TOOLS_INCLUDE=read_file,write_file,edit_file,grep,glob,web_fetch,web_search,todo_view,todo_write,private_chat \
           calfcord:latest calfkit-tools
```

Now host A serves every tool *except* shell; host B serves shell only;
each tool has exactly one host responsible for it. This is the
intended distributed shape.

The same `CALFCORD_TOOLS_INCLUDE` env var is consumed by both the slim
images (where it's baked into the Dockerfile's `ENV`) and the all-in-one
image (where you set it at `docker run` time). The semantics are
identical: filter `tools/discovery.py`'s scan down to the listed names.

For the broader security model that goes with running tools on multiple
hosts, see `docs/security.md` § Distributed deployments. For the
tool-authoring contract those tools still need to follow, see
`docs/authoring-tools.md`.
