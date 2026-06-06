"""``calfcord deploy <systemd|k8s|docker>`` — Altitude-3 graduation manifests.

calfcord's day-one supervisor is Process Compose (single host, generated YAML the
operator never edits). ``deploy`` is the *graduation* surface (design §2 / §6 /
§11.5): it emits the heavier-tier artifacts an operator hand-validates before
running across boxes. The portable invariant is the **Kafka wire + ``.env`` +
``agents/*.md``**, NOT the Process Compose project — so these manifests re-derive
the same roster from the same seam (:func:`detect_agents`) and dial a shared
broker, exactly like the distributed primitive (§12.5).

Three targets, each with a different honesty posture (§11.6):

* **systemd** — a *real, correct-by-construction* unit for the single-host
  substrate. ``calfcord start`` forks a detached supervisor and returns 0 once the
  bridge is healthy (findings.start_model), which is precisely ``Type=forking``;
  the unit runs the install **shim** (``<shim> start`` / ``<shim> stop``) — the
  single seam that owns the ``up`` flags, the derived REST port, the priming
  reconcile and the readiness gate — never a reconstructed ``up`` argv (§12.3 /
  §13.2). It is the one artifact whose correctness we can vouch for, yet it is
  still headed as a reference because per-host paths/users vary.
* **k8s** — *reference* manifests (clearly annotated): a broker workload, a
  ConfigMap with the shared ``CALF_HOST_URL``, and one Deployment per process type
  (bridge / router / tools / mcp) plus one per *defined* agent, all on the shipped
  calfcord image running the ``calfkit-*`` console scripts the compose uses. NOT
  ``calfcord start`` (there is no in-pod supervisor); each process type is its own
  workload dialing the shared broker — the Altitude-3 distributed shape. Secrets
  arrive via a ``Secret`` reference, never inlined (§12.3).
* **docker** — the shipped ``docker-compose.yml`` is hand-tuned (Codex auth
  mounts, the A2A channel override, ``depends_on`` healthchecks); regenerating it
  would lose that nuance, so ``deploy docker`` *points at the real file* and emits
  an optional per-agent ``compose.override.yml`` snippet (``calfkit-agent
  <name>``) for crash isolation, derived from the roster.

Decoupling invariant (§12.3): this module renders text from a roster + paths and
inlines **no secret literal**; it imports only :func:`detect_agents` /
:func:`read_env` (schema/path-only seams) and never
:mod:`calfcord.mcp.config` (the bridge-only ``$VAR`` secrets loader). ``deploy``
reads the install's agents + ``.env`` off disk and never talks to the running
supervisor — so it needs no REST probe and no running supervisor to render. It
does, however, require a native install (``CALFCORD_HOME``): the emitted manifests
reference the install's shim launcher and home paths, so the :mod:`main` veneer
(:func:`_run_deploy`) refuses on a dev tree rather than rendering a manifest that
points at nothing.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import yaml

from calfcord.cli._agents import detect_agents
from calfcord.cli._envfile import read_env

# The shipped image tag the docker-compose build produces; the k8s reference
# manifests default to it so a freshly-built local image runs unchanged.
_DEFAULT_IMAGE = "calfcord:latest"

# The Kubernetes Secret the reference manifests pull env from. The operator
# creates it out-of-band (e.g. `kubectl create secret generic calfcord-secrets
# --from-env-file=.env`); we reference it, never inline the values (§12.3).
_K8S_SECRET_NAME = "calfcord-secrets"

# In-cluster broker Service name the ConfigMap points roster pods at when the
# bundled broker is used. An external/managed broker flows through verbatim; a
# localhost-ish server_urls (the default `CALF_HOST_URL or "localhost"` _run_deploy
# hands down) is rewritten to this Service so pods dial the bundled broker, not
# their own loopback.
_K8S_BROKER_SERVICE = "broker"

# The bundled broker's listener port (the Service/Deployment expose it). A
# localhost-ish server_urls with no explicit port resolves to this default.
_K8S_BROKER_PORT = 9092

# Hostnames that mean "this pod's own loopback" — useless across pods. server_urls
# resolving to one of these is rewritten to the in-cluster broker Service.
_LOOPBACK_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


def _resolve_config_host_url(server_urls: str) -> str:
    """Resolve the ConfigMap ``CALF_HOST_URL`` for the bundled-broker manifest.

    The default deploy path passes ``CALF_HOST_URL or "localhost"`` (single host),
    but the k8s reference ships a Service named ``broker`` and one pod per process
    type: a verbatim ``localhost`` would make every pod dial its own loopback and
    the manifest fails on apply. So a loopback host (``localhost`` / ``127.0.0.1`` /
    ``::1`` / empty, with or without ``:PORT``) resolves to the in-cluster Service
    name, preserving an explicit port (a custom loopback port is the broker's
    listener port). A real external/non-loopback broker passes through verbatim.
    """
    candidate = server_urls.strip()
    # urlsplit needs a scheme to populate .hostname/.port; the wire form is bare
    # host:port (e.g. "localhost:9092"), so prepend a dummy scheme to parse it.
    parsed = urlsplit(f"//{candidate}")
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return server_urls
    port = parsed.port or _K8S_BROKER_PORT
    return f"{_K8S_BROKER_SERVICE}:{port}"


# The console script each non-agent process type runs (design §6: the raw
# calfkit-* entry points are the Altitude-3 multi-host primitives). Agents use
# `calfkit-agent <name>` so a pod runs exactly one agent (crash isolation).
_PROCESS_COMMANDS = {
    "bridge": "calfkit-bridge",
    "router": "calfkit-router",
    "tools": "calfkit-tools",
    "mcp": "calfkit-mcp",
}

_VALID_TARGETS = ("systemd", "k8s", "docker")


# --- systemd -----------------------------------------------------------------


def render_systemd(*, home: str, launcher: str) -> str:
    """Render a systemd unit for the single-host substrate.

    Models how ``calfcord start`` actually launches the supervisor: it forks a
    detached process-compose that outlives the CLI and returns 0 once the bridge
    is healthy — exactly the ``Type=forking`` contract (findings.start_model). The
    unit runs the install **shim** (``<launcher> start`` / ``<launcher> stop``),
    the single seam that knows the ``up`` flags, the home-derived REST port, the
    priming reconcile and the readiness gate; reconstructing the ``up`` argv here
    would duplicate the §13.2 contract and drift. ``CALFCORD_HOME`` is exported so
    the shim and ``pc_port_for`` resolve the same home. ``Restart=on-failure`` only
    reacts to the *fork* (the ``calfcord start`` ExecStart) exiting non-zero — with
    ``Type=forking`` and no ``PIDFile=`` systemd cannot track the detached
    process-compose, so a crash *inside* the supervised tree is not auto-recovered
    here (process-compose's own per-process restarts cover that); an
    operator-commanded ``stop`` is left alone.

    Emitted as a *user* unit (``WantedBy=default.target``, ``systemctl --user``):
    the supervisor runs under the install owner's session, so the unit carries no
    system-only ``User=`` / ``Group=`` directives (a ``--user`` unit rejects them).
    Headed as a *reference* unit (§11.6): the shape is correct, but per-host paths
    vary, so the operator validates before enabling.
    """
    # Plain text (not configparser.write, which lowercases nothing but emits its
    # own quirks): a systemd unit is hand-readable and the operator edits it.
    return (
        "# calfcord substrate — systemd USER unit (REFERENCE: validate paths for your host).\n"
        "#\n"
        "# Models `calfcord start`: it forks a detached process-compose supervisor and\n"
        "# returns 0 once the bridge is healthy, so Type=forking is the faithful type.\n"
        "# ExecStart/ExecStop run the install shim — the single seam that owns the\n"
        "# process-compose `up` flags, the home-derived REST port, and the readiness gate.\n"
        "# Install for the current login session: systemctl --user enable --now calfcord\n"
        "# (a --user unit already runs as that login user — system-only owner directives\n"
        "# do not apply and are intentionally omitted).\n"
        "# Restart=on-failure only catches the `start` fork exiting non-zero; with\n"
        "# Type=forking and no PIDFile= systemd can't track the detached supervisor, so a\n"
        "# crash inside the tree is handled by process-compose's per-process restarts.\n"
        "[Unit]\n"
        "Description=calfcord substrate (broker + bridge)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=forking\n"
        f"Environment=CALFCORD_HOME={home}\n"
        f"WorkingDirectory={home}\n"
        f"ExecStart={launcher} start\n"
        f"ExecStop={launcher} stop\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# --- k8s ---------------------------------------------------------------------


def _k8s_container(
    *, name: str, image: str, command: list[str]
) -> dict:
    """One reference container: the shipped image, a calfkit-* command, env from
    the shared ConfigMap + the out-of-band Secret (never inlined, §12.3)."""
    return {
        "name": name,
        "image": image,
        "command": command,
        "envFrom": [
            {"configMapRef": {"name": "calfcord-config"}},
            {"secretRef": {"name": _K8S_SECRET_NAME}},
        ],
    }


def _k8s_deployment(*, name: str, image: str, command: list[str]) -> dict:
    """A single-replica Deployment running one calfcord process type/agent.

    One replica per workload: the bridge/router are singletons, and an agent
    Deployment runs exactly one agent id for crash isolation (a second replica of
    the same agent id would double-reply, §12.5).
    """
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app": "calfcord", "component": name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "calfcord", "component": name}},
            "template": {
                "metadata": {"labels": {"app": "calfcord", "component": name}},
                "spec": {"containers": [_k8s_container(name=name, image=image, command=command)]},
            },
        },
    }


def render_k8s(*, agent_ids: list[str], server_urls: str, image: str = _DEFAULT_IMAGE) -> str:
    """Render *reference* Kubernetes manifests for a distributed calfcord.

    A multi-document YAML stream: a comment header, a ConfigMap holding the shared
    ``CALF_HOST_URL`` (an external ``server_urls`` flows through verbatim; a
    localhost-ish one is rewritten to the bundled broker Service so pods don't dial
    their own loopback), a broker Service + Deployment, and one Deployment per
    process type (bridge / router / tools / mcp) plus one per *defined* agent —
    each running a ``calfkit-*`` console script on the shipped image, dialing the
    shared broker. This is the Altitude-3 distributed shape, NOT ``calfcord start``
    (no in-pod supervisor). The roster is sorted so the document order never
    depends on ``.md`` glob order.

    Annotated as a *reference* starting point (§11.6): cluster specifics
    (storage class for a durable broker, resource limits, ingress, the Secret's
    creation) are left to the operator. Secrets arrive via a ``Secret`` reference,
    never inlined (§12.3).
    """
    roster = sorted(agent_ids)

    header = (
        "# calfcord — REFERENCE Kubernetes manifests (Altitude-3 distributed deploy).\n"
        "# A starting point to validate per cluster — NOT a turnkey production install.\n"
        "# Each process type is its own Deployment dialing the shared broker (no in-pod\n"
        "# supervisor; this is the distributed primitive, not `calfcord start`).\n"
        "#\n"
        "# Before applying, create the secrets the bridge/agents need (out of band):\n"
        f"#   kubectl create secret generic {_K8S_SECRET_NAME} --from-env-file=.env\n"
        "# Tune replicas/resources/storage and swap the broker for your managed Kafka\n"
        "# (then drop the bundled broker workload and point CALF_HOST_URL at it).\n"
        "# Note: agents/*.md frontmatter the agent rewrites at runtime (e.g. thinking-\n"
        "# effort) is NON-PERSISTENT here — these pods get envFrom only, no writable\n"
        "# agents/ volume; mount a writable agents/ volume (PVC) if those edits must\n"
        "# survive a pod restart.\n"
    )

    # Resolve the shared CALF_HOST_URL once: an external/managed broker flows
    # through verbatim; a localhost-ish server_urls (the default _run_deploy hands
    # down) is rewritten to the bundled broker Service below — otherwise every pod
    # would dial its own loopback and the manifest would fail on apply.
    config_host_url = _resolve_config_host_url(server_urls)
    # The port the in-cluster roster dials for the bundled broker. When the resolved
    # URL targets our Service (localhost-ish input), the bundled broker Service +
    # advertised listener follow that port so the reference manifest is internally
    # consistent (a custom loopback port survives end-to-end). For an external
    # broker the bundled workload is moot (the header tells operators to drop it).
    cluster_broker_port = _K8S_BROKER_PORT
    if config_host_url.startswith(f"{_K8S_BROKER_SERVICE}:"):
        cluster_broker_port = int(config_host_url.rsplit(":", 1)[1])

    docs: list[dict] = []

    # Shared, non-secret config.
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "calfcord-config", "labels": {"app": "calfcord"}},
            "data": {"CALF_HOST_URL": config_host_url},
        }
    )

    # Broker: a single-instance workload + a ClusterIP Service the roster dials.
    # Memory storage (matching the shipped compose) — annotate durable storage as
    # an operator choice in the header. The container always listens on the broker's
    # native 9092; the Service maps the dialed port to it and the advertised listener
    # matches what clients dial (the resolved CALF_HOST_URL above).
    docs.append(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": _K8S_BROKER_SERVICE,
                "labels": {"app": "calfcord", "component": "broker"},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "calfcord", "component": "broker"}},
                "template": {
                    "metadata": {"labels": {"app": "calfcord", "component": "broker"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "broker",
                                "image": "ghcr.io/tansu-io/tansu:latest",
                                "args": [
                                    "broker",
                                    "--storage-engine=memory://tansu/",
                                    f"--listener-url=tcp://0.0.0.0:{_K8S_BROKER_PORT}",
                                    "--advertised-listener-url="
                                    f"tcp://{_K8S_BROKER_SERVICE}:{cluster_broker_port}",
                                ],
                                "ports": [{"containerPort": _K8S_BROKER_PORT}],
                            }
                        ]
                    },
                },
            },
        }
    )
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": _K8S_BROKER_SERVICE,
                "labels": {"app": "calfcord", "component": "broker"},
            },
            "spec": {
                "selector": {"app": "calfcord", "component": "broker"},
                "ports": [{"port": cluster_broker_port, "targetPort": _K8S_BROKER_PORT}],
            },
        }
    )

    # Fixed process-type Deployments (singletons), then one per defined agent.
    for component, script in _PROCESS_COMMANDS.items():
        docs.append(_k8s_deployment(name=component, image=image, command=[script]))
    for agent_id in roster:
        docs.append(
            _k8s_deployment(
                name=f"agent-{agent_id}",
                image=image,
                command=["calfkit-agent", agent_id],
            )
        )

    body = yaml.safe_dump_all(docs, sort_keys=False, explicit_start=True)
    return f"{header}{body}"


# --- docker ------------------------------------------------------------------


def render_docker(*, repo_compose_path: str, agent_ids: list[str]) -> str:
    """Point the operator at the shipped compose + an optional per-agent override.

    The shipped ``docker-compose.yml`` is hand-tuned — Codex auth mounts, the A2A
    channel override, ``depends_on`` healthchecks — so regenerating it would lose
    that nuance. ``deploy docker`` therefore names the real file and, when the
    roster has agents, emits a ``compose.override.yml`` snippet that *replaces* the
    single all-in-one ``agent`` service with one ``calfkit-agent <name>`` service
    per defined agent (crash isolation: a crash in one agent's container no longer
    takes the others down). The roster is sorted so the snippet is deterministic.

    The replacement is the load-bearing detail: an override that only *added*
    ``agent-<id>`` services would leave the base ``agent`` running too (it boots
    *all* agents in one process), so ``docker compose up`` would double-run every
    agent. Compose merges an override onto the base service-by-service, so the
    snippet pins ``agent: {scale: 0}`` — which ``docker compose up`` honours by
    starting 0 containers of the base — turning the additive merge into a genuine
    split.
    """
    roster = sorted(agent_ids)

    lines = [
        "# calfcord — Docker deployment.",
        "#",
        "# The shipped docker-compose.yml is hand-tuned (Codex auth mounts, the A2A",
        "# channel override, depends_on healthchecks) — do NOT regenerate it. Use it",
        "# directly:",
        f"#   docker compose -f {repo_compose_path} up --build",
        "#",
    ]

    if not roster:
        lines.append("# No agents are defined yet — `calfcord agent create` first, then re-run.")
        return "\n".join(lines) + "\n"

    lines += [
        "# For per-agent crash isolation, drop this compose.override.yml next to the",
        "# shipped file: it REPLACES the single all-in-one `agent` service with one",
        "# service per defined agent (each runs `calfkit-agent <name>`), so one agent",
        "# crashing no longer restarts the others. The base `agent` is disabled with",
        "# `scale: 0` (compose starts 0 of it) so it does not double-run every agent",
        "# alongside the per-agent services. Each per-agent service inherits the base",
        "# build/image/env via `extends` — keep the Codex mounts/CALF_HOST_URL there.",
        "#",
        "# --- compose.override.yml ---",
        "services:",
        # Disable the all-in-one base service: compose merges this override onto the
        # base `agent` (it does not remove it), so without scale: 0 `docker compose
        # up` would run the base (all agents in one) PLUS every agent-<id> below.
        "  agent:",
        "    scale: 0",
    ]
    for agent_id in roster:
        # Each per-agent service only sets `command:`; everything else (build,
        # image, env_file, CALF_HOST_URL, Codex mounts) is inherited from the base
        # `agent` service in the shipped file via `extends`, so secrets/mounts are
        # never duplicated here (and no secret is inlined, §12.3).
        lines += [
            f"  agent-{agent_id}:",
            "    extends:",
            "      file: docker-compose.yml",
            "      service: agent",
            f"    command: calfkit-agent {agent_id}",
        ]

    return "\n".join(lines) + "\n"


# --- run veneer --------------------------------------------------------------


def run(
    target: str,
    *,
    home: Path,
    env_path: Path,
    agents_dir: Path,
    server_urls: str,
    out_path: Path | None = None,
) -> int:
    """Render ``target``'s manifest to stdout (or ``out_path``) and return a code.

    The thin veneer over the pure render functions: it resolves the launcher
    (``<home>/shims/calfcord`` — the same shim every supervised ``command`` is
    built on), enumerates the roster via :func:`detect_agents` (the exact seam
    ``calfcord start`` uses, so the manifest's roster equals what ``start``
    declares), and reads ``.env`` only to *preflight* (never to inline secret
    values). Output goes to stdout by default; ``out_path`` writes the manifest to
    a file instead. An unknown ``target`` returns non-zero with an error rather
    than rendering garbage (argparse ``choices=`` guards the CLI, but a direct
    caller must fail cleanly too).
    """
    if target not in _VALID_TARGETS:
        print(f"error: unknown deploy target {target!r} (choose one of {', '.join(_VALID_TARGETS)})")
        return 1

    roster = detect_agents(agents_dir)
    launcher = str(home / "shims" / "calfcord")

    if target == "systemd":
        # Preflight: a substrate unit only makes sense once a broker is configured.
        # Read .env for the surfaced hint — never inline its values.
        if not read_env(env_path).get("CALF_HOST_URL"):
            print(
                "# note: CALF_HOST_URL is not set in this install's .env yet — the substrate "
                "won't start until it is (run `calfcord init` or `calfcord self set-broker`)."
            )
        manifest = render_systemd(home=str(home), launcher=launcher)
    elif target == "k8s":
        manifest = render_k8s(agent_ids=roster, server_urls=server_urls)
    else:  # docker — guarded by the membership check above
        manifest = render_docker(repo_compose_path="docker-compose.yml", agent_ids=roster)

    if out_path is not None:
        out_path.write_text(manifest, encoding="utf-8")
        print(f"wrote {target} manifest to {out_path}")
    else:
        print(manifest, end="" if manifest.endswith("\n") else "\n")
    return 0
