"""Golden tests for the ``calfcord deploy`` manifest generators.

The three render functions are pure: roster + home + launcher (+ broker URL) in,
manifest text out — no filesystem, no broker, no supervisor. So the tests assert
on the *parsed* structure (``configparser`` for the systemd INI, ``yaml`` for the
k8s docs) rather than brittle string matching, mirroring
``tests/supervisor/test_compose.py``. The ``run`` veneer is exercised against a
tmp_path roster so a 2-agent install renders 2 agent Deployments and stdout-vs-
file output are both covered.

The hard contracts pinned here (design §11.6 honesty + §12.3 secrets):

* the systemd unit models how ``calfcord start`` actually launches the
  supervisor (``Type=forking`` detached-return; ExecStart/ExecStop invoke the
  install shim, never a reconstructed ``up`` argv);
* the k8s manifests are *reference* artifacts (annotated as such) that honour the
  configured broker URL and render one Deployment per defined agent;
* NO secret literal is ever inlined into any rendered manifest.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest
import yaml

from calfcord.cli import deploy

_HOME = "/srv/calfcord"
_LAUNCHER = "/srv/calfcord/shims/calfcord"
_AGENTS = ["assistant", "scribe"]
_BROKER = "broker.example.com:9092"

# A fake bot token: the secrets invariant is that NO render ever inlines it.
_FAKE_TOKEN = "MTopSecretBotToken.deadbeef.shouldNeverAppearInAManifest"


# --- systemd -----------------------------------------------------------------


def _systemd_ini() -> configparser.ConfigParser:
    rendered = deploy.render_systemd(home=_HOME, launcher=_LAUNCHER)
    # systemd unit files are INI-shaped; configparser parses them faithfully once
    # comment lines (``#``/``;``) are recognised, which is the default.
    parser = configparser.ConfigParser()
    parser.read_string(rendered)
    return parser


def test_systemd_models_the_detached_return_as_forking() -> None:
    # `calfcord start` forks a detached supervisor and returns 0 once healthy
    # (findings.start_model), so Type=forking is the faithful service type.
    assert _systemd_ini()["Service"]["Type"] == "forking"


def test_systemd_execstart_and_execstop_invoke_the_shim() -> None:
    service = _systemd_ini()["Service"]
    # Never reconstruct the `up -f ... -D -t=false -p ... -L ...` argv: the unit
    # runs the shim, the single seam that owns the up flags + readiness gate.
    assert service["ExecStart"] == f"{_LAUNCHER} start"
    assert service["ExecStop"] == f"{_LAUNCHER} stop"


def test_systemd_sets_home_environment_and_working_dir() -> None:
    service = _systemd_ini()["Service"]
    # pc_port_for + the shim both resolve from $CALFCORD_HOME, so the unit must
    # export it (and run from the install home).
    assert service["Environment"] == f"CALFCORD_HOME={_HOME}"
    assert service["WorkingDirectory"] == _HOME


def test_systemd_restarts_on_failure() -> None:
    assert _systemd_ini()["Service"]["Restart"] == "on-failure"


def test_systemd_has_install_section_so_it_can_be_enabled() -> None:
    # Without [Install] `systemctl enable` is a no-op; a real unit needs it.
    assert "Install" in _systemd_ini()


def test_systemd_inlines_no_secret() -> None:
    rendered = deploy.render_systemd(home=_HOME, launcher=_LAUNCHER)
    assert _FAKE_TOKEN not in rendered


def test_systemd_is_annotated_as_reference() -> None:
    # Honesty (§11.6): real-world correctness can't be guaranteed, so the header
    # must flag it as a reference/starting point to validate per host.
    rendered = deploy.render_systemd(home=_HOME, launcher=_LAUNCHER)
    assert "reference" in rendered.lower()


def test_systemd_is_a_coherent_user_unit() -> None:
    # The unit is a USER unit (WantedBy=default.target + `systemctl --user`).
    # User=/Group= are SYSTEM-unit directives a --user unit rejects, so the header
    # must NOT tell the operator to set them alongside `systemctl --user enable`.
    rendered = deploy.render_systemd(home=_HOME, launcher=_LAUNCHER)
    assert "systemctl --user" in rendered
    assert _systemd_ini()["Install"]["WantedBy"] == "default.target"
    assert "User=" not in rendered, "a --user unit rejects User=; don't advise it"
    assert "Group=" not in rendered, "a --user unit rejects Group=; don't advise it"


def test_systemd_header_does_not_overclaim_crash_recovery() -> None:
    # Type=forking with no PIDFile cannot reliably track the detached supervisor, so
    # the header must not claim Restart=on-failure "recovers a crashed supervisor".
    rendered = deploy.render_systemd(home=_HOME, launcher=_LAUNCHER).lower()
    assert "recovers a crashed supervisor" not in rendered


def test_systemd_is_deterministic() -> None:
    a = deploy.render_systemd(home=_HOME, launcher=_LAUNCHER)
    b = deploy.render_systemd(home=_HOME, launcher=_LAUNCHER)
    assert a == b


# --- k8s ---------------------------------------------------------------------


def _k8s_docs(agent_ids: list[str] | None = None) -> list[dict]:
    rendered = deploy.render_k8s(
        agent_ids=_AGENTS if agent_ids is None else agent_ids,
        server_urls=_BROKER,
        image="calfcord:latest",
    )
    # The manifest is a multi-document YAML stream (--- separated); drop the
    # comment-only header doc (parses to None).
    return [doc for doc in yaml.safe_load_all(rendered) if doc is not None]


def _by_name(docs: list[dict], kind: str) -> dict[str, dict]:
    return {
        doc["metadata"]["name"]: doc
        for doc in docs
        if doc.get("kind") == kind
    }


def test_k8s_renders_a_broker_workload() -> None:
    docs = _k8s_docs()
    # The broker is a stateful single instance; a Deployment or StatefulSet named
    # for it must exist (the substrate root every process dials).
    broker_workloads = [
        d for d in docs
        if d.get("kind") in ("Deployment", "StatefulSet")
        and d["metadata"]["name"] == "broker"
    ]
    assert len(broker_workloads) == 1


def test_k8s_configmap_carries_the_configured_broker_url() -> None:
    configmaps = _by_name(_k8s_docs(), "ConfigMap")
    assert configmaps, "a ConfigMap must hold the shared CALF_HOST_URL"
    # The configured broker URL flows through verbatim (honours `server_urls`),
    # except the broker workload itself dials in-cluster — but the shared roster
    # config must reference what the operator configured.
    values = " ".join(str(v) for cm in configmaps.values() for v in cm["data"].values())
    assert _BROKER in values


def _k8s_configmap_host_url(server_urls: str) -> str:
    """Render with the given ``server_urls`` and return the ConfigMap CALF_HOST_URL."""
    rendered = deploy.render_k8s(agent_ids=_AGENTS, server_urls=server_urls, image="calfcord:latest")
    configmaps = _by_name(
        [doc for doc in yaml.safe_load_all(rendered) if doc is not None], "ConfigMap"
    )
    return configmaps["calfcord-config"]["data"]["CALF_HOST_URL"]


@pytest.mark.parametrize(
    "server_urls",
    # Includes the IPv6 loopback in both the bare ("::1") and bracketed
    # ("[::1]:9092") wire forms — the unbracketed form does not survive urlsplit
    # as a hostname, so it is normalised explicitly.
    ["localhost", "localhost:9092", "127.0.0.1", "127.0.0.1:9092", "", "::1", "[::1]:9092"],
)
def test_k8s_localhost_broker_resolves_to_in_cluster_service(server_urls: str) -> None:
    # The bundled-broker manifest ships a Service named `broker` at broker:9092, so a
    # localhost-ish server_urls (the default _run_deploy hands down `CALF_HOST_URL or
    # "localhost"`) must become the in-cluster Service name — otherwise every pod
    # dials its own loopback and the manifest fails on apply.
    assert _k8s_configmap_host_url(server_urls) == "broker:9092"


def test_k8s_localhost_broker_preserves_a_custom_port() -> None:
    # A non-default loopback port is the broker's listener port, so resolving to the
    # Service must keep it (the Service/Deployment port follow the same default).
    assert _k8s_configmap_host_url("localhost:1234") == "broker:1234"


def test_k8s_external_broker_passes_through_verbatim() -> None:
    # A real external/managed Kafka must flow through unchanged — only loopback is
    # rewritten to the in-cluster Service.
    assert _k8s_configmap_host_url(_BROKER) == _BROKER


@pytest.mark.parametrize(
    "server_urls",
    # A multi-broker bootstrap list is the operator's explicit cross-host target,
    # not a single loopback to rewrite — and is not a single URL `urlsplit` can
    # parse, so it must flow through verbatim (never crash `calfcord deploy k8s`).
    ["127.0.0.1:9092,host2:9092", "broker-a:9092,broker-b:9092"],
)
def test_k8s_multi_broker_bootstrap_passes_through_verbatim(server_urls: str) -> None:
    assert _k8s_configmap_host_url(server_urls) == server_urls


@pytest.mark.parametrize(
    "server_urls",
    # A loopback host with an UNPARSEABLE port — `urlsplit(...).port` raises
    # ValueError on a non-integer or out-of-range port. The resolver must fall
    # back to a verbatim passthrough, never let that ValueError crash
    # `calfcord deploy`. (The plain non-loopback `host:9092` case is covered by
    # the external-broker test; these pin the loopback ValueError branch.)
    ["localhost:abc", "localhost:65536", "localhost:-1"],
)
def test_k8s_unparseable_broker_port_passes_through_verbatim(server_urls: str) -> None:
    assert _k8s_configmap_host_url(server_urls) == server_urls


def test_k8s_renders_one_deployment_per_process_type() -> None:
    deployments = _by_name(_k8s_docs(), "Deployment")
    for name in ("bridge", "router", "tools"):
        assert name in deployments, f"missing a {name} Deployment"


def test_k8s_renders_one_deployment_per_defined_agent() -> None:
    # A 2-agent roster renders 2 agent Deployments (the design's distributed
    # primitive: each process type is its own workload dialing the shared broker).
    deployments = _by_name(_k8s_docs(["assistant", "scribe"]), "Deployment")
    assert "agent-assistant" in deployments
    assert "agent-scribe" in deployments


def test_k8s_agent_deployments_run_the_named_agent() -> None:
    deployments = _by_name(_k8s_docs(["assistant"]), "Deployment")
    container = deployments["agent-assistant"]["spec"]["template"]["spec"]["containers"][0]
    # The single-agent runner takes the agent name as an arg (calfkit-agent <name>),
    # so per-agent crash isolation maps to one container running just that agent.
    argv = [*container["command"], *container.get("args", [])]
    assert "calfkit-agent" in argv
    assert "assistant" in argv


def test_k8s_process_deployments_use_the_calfkit_console_scripts() -> None:
    deployments = _by_name(_k8s_docs(), "Deployment")
    expected = {
        "bridge": "calfkit-bridge",
        "router": "calfkit-router",
        "tools": "calfkit-tools",
    }
    for name, script in expected.items():
        container = deployments[name]["spec"]["template"]["spec"]["containers"][0]
        argv = [*container["command"], *container.get("args", [])]
        assert script in argv, f"{name} should run {script}"


def test_k8s_calfcord_workloads_use_the_shipped_image() -> None:
    # Every calfcord process workload runs the shipped image; the broker is the
    # one exception (it runs the tansu image), so it is excluded here.
    deployments = _by_name(_k8s_docs(), "Deployment")
    for name, dep in deployments.items():
        if name == "broker":
            continue
        container = dep["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "calfcord:latest"


def test_k8s_pulls_secrets_by_reference_not_literal() -> None:
    rendered = deploy.render_k8s(agent_ids=_AGENTS, server_urls=_BROKER, image="calfcord:latest")
    # The token must never be baked into the manifest; it must arrive via a Secret
    # reference (envFrom / secretKeyRef), so the operator supplies it out-of-band.
    assert _FAKE_TOKEN not in rendered
    assert "secretRef" in rendered or "secretKeyRef" in rendered


def test_k8s_no_agents_still_renders_the_substrate() -> None:
    deployments = _by_name(_k8s_docs([]), "Deployment")
    for name in ("bridge", "router", "tools"):
        assert name in deployments
    # ...and no orphan agent Deployment slipped in.
    assert not any(n.startswith("agent-") for n in deployments)


def test_k8s_is_annotated_as_reference() -> None:
    rendered = deploy.render_k8s(agent_ids=_AGENTS, server_urls=_BROKER, image="calfcord:latest")
    assert "reference" in rendered.lower()


def test_k8s_header_notes_runtime_agent_writes_are_non_persistent() -> None:
    # Honesty (§11.6): the shipped compose mounts ./agents read-WRITE because the
    # agent writes frontmatter overrides (e.g. thinking-effort) at runtime, but the
    # reference pods get only envFrom (no writable agents/ volume). The header must
    # say so — those runtime writes are lost on pod restart unless the operator
    # mounts a writable agents/ volume (PVC).
    rendered = deploy.render_k8s(agent_ids=_AGENTS, server_urls=_BROKER, image="calfcord:latest")
    header = rendered.split("---", 1)[0].lower()  # the comment header precedes the first YAML doc
    assert "frontmatter" in header
    assert "pvc" in header or "writable" in header


def test_k8s_is_deterministic_and_orders_agents_stably() -> None:
    a = deploy.render_k8s(agent_ids=["scribe", "assistant"], server_urls=_BROKER, image="calfcord:latest")
    b = deploy.render_k8s(agent_ids=["scribe", "assistant"], server_urls=_BROKER, image="calfcord:latest")
    assert a == b
    # The roster is sorted so the manifest order never depends on .md glob order.
    deployments = list(_by_name(_k8s_docs(["scribe", "assistant"]), "Deployment"))
    agent_order = [n for n in deployments if n.startswith("agent-")]
    assert agent_order == sorted(agent_order)


def test_k8s_round_trips_as_valid_yaml_documents() -> None:
    # Every document in the stream must be loadable (no malformed YAML).
    docs = _k8s_docs()
    assert all(isinstance(d, dict) for d in docs)


# --- docker ------------------------------------------------------------------


def test_docker_points_at_the_shipped_compose_file() -> None:
    rendered = deploy.render_docker(repo_compose_path="/repo/docker-compose.yml", agent_ids=_AGENTS)
    # The hand-tuned compose (codex mounts, A2A override, healthchecks) must NOT
    # be regenerated — deploy points the operator at the real file instead.
    assert "/repo/docker-compose.yml" in rendered


def test_docker_emits_a_per_agent_override_snippet() -> None:
    rendered = deploy.render_docker(repo_compose_path="/repo/docker-compose.yml", agent_ids=["assistant", "scribe"])
    # The optional override yields per-agent services for crash isolation
    # (`calfkit-agent <name>`), derived from the roster.
    assert "calfkit-agent assistant" in rendered
    assert "calfkit-agent scribe" in rendered


def _docker_override_services(rendered: str) -> dict:
    """Parse the emitted ``compose.override.yml`` snippet's ``services:`` map.

    The render wraps the override in comment framing + a ``# --- ... ---`` fence;
    the actual YAML is the non-comment tail. Strip the leading ``#`` lines so
    ``yaml.safe_load`` sees only the compose document.
    """
    yaml_lines = [line for line in rendered.splitlines() if not line.lstrip().startswith("#")]
    doc = yaml.safe_load("\n".join(yaml_lines))
    assert isinstance(doc, dict), "the override snippet must parse to a compose mapping"
    return doc["services"]


def test_docker_override_disables_the_base_agent_service() -> None:
    # The override must genuinely REPLACE the all-in-one `agent` service, not just
    # ADD `agent-<id>` ones — otherwise `docker compose up` runs BOTH the base
    # `agent` (all agents in one) AND every per-agent service, double-running each
    # agent. `scale: 0` tells compose to start 0 containers of the base service.
    services = _docker_override_services(
        deploy.render_docker(repo_compose_path="/repo/docker-compose.yml", agent_ids=["assistant", "scribe"])
    )
    assert "agent" in services, "the base `agent` service must be present to disable it"
    assert services["agent"].get("scale") == 0, (
        "the base `agent` service must carry scale: 0 so `docker compose up` runs "
        "0 of it (otherwise the base + per-agent services double-run every agent)"
    )


def test_docker_override_renders_one_service_per_agent_extending_the_base() -> None:
    # Each defined agent gets its own service running `calfkit-agent <name>`, and
    # inherits the hand-tuned base (build/image/env/Codex mounts) via `extends`
    # rather than duplicating it (so no secret/mount is re-declared, §12.3).
    services = _docker_override_services(
        deploy.render_docker(repo_compose_path="/repo/docker-compose.yml", agent_ids=["assistant", "scribe"])
    )
    for agent_id in ("assistant", "scribe"):
        svc = services[f"agent-{agent_id}"]
        assert svc["command"] == f"calfkit-agent {agent_id}"
        assert svc["extends"]["service"] == "agent"


def test_docker_with_no_agents_points_at_the_file_without_an_override() -> None:
    rendered = deploy.render_docker(repo_compose_path="/repo/docker-compose.yml", agent_ids=[])
    # Still names the real file, but with no agents there is no per-agent split to
    # emit — surface that explicitly rather than printing an empty `services:`.
    assert "/repo/docker-compose.yml" in rendered
    assert "compose.override.yml" not in rendered
    assert "calfkit-agent" not in rendered


def test_docker_inlines_no_secret() -> None:
    rendered = deploy.render_docker(repo_compose_path="/repo/docker-compose.yml", agent_ids=_AGENTS)
    assert _FAKE_TOKEN not in rendered


# --- run veneer --------------------------------------------------------------


def _install_home(tmp_path: Path, agents: list[str]) -> Path:
    home = tmp_path / "home"
    agents_dir = home / "agents"
    agents_dir.mkdir(parents=True)
    for name in agents:
        (agents_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndisplay_name: {name.title()}\n"
            f"provider: anthropic\nmodel: claude-x\n---\nbody\n",
            encoding="utf-8",
        )
    return home


@pytest.mark.parametrize("target", ["systemd", "k8s", "docker"])
def test_run_dispatches_each_target_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    home = _install_home(tmp_path, ["assistant"])
    env_path, agents_dir = (home / "config" / ".env", home / "agents")
    rc = deploy.run(
        target,
        home=home,
        env_path=env_path,
        agents_dir=agents_dir,
        server_urls="localhost:9092",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip(), f"{target} rendered nothing to stdout"


def test_run_k8s_renders_one_deployment_per_defined_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The veneer enumerates the roster via detect_agents (the same seam `start`
    # uses), so a 2-agent install yields 2 agent Deployments end to end.
    home = _install_home(tmp_path, ["assistant", "scribe"])
    rc = deploy.run(
        "k8s",
        home=home,
        env_path=home / "config" / ".env",
        agents_dir=home / "agents",
        server_urls="localhost:9092",
    )
    assert rc == 0
    docs = [d for d in yaml.safe_load_all(capsys.readouterr().out) if d is not None]
    agent_deps = [
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"].startswith("agent-")
    ]
    assert sorted(agent_deps) == ["agent-assistant", "agent-scribe"]


def test_run_writes_to_an_output_path_when_given(tmp_path: Path) -> None:
    home = _install_home(tmp_path, ["assistant"])
    out_file = tmp_path / "calfcord.service"
    rc = deploy.run(
        "systemd",
        home=home,
        env_path=home / "config" / ".env",
        agents_dir=home / "agents",
        server_urls="localhost:9092",
        out_path=out_file,
    )
    assert rc == 0
    assert out_file.read_text(encoding="utf-8").strip()
    assert "[Service]" in out_file.read_text(encoding="utf-8")


def test_run_unknown_target_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # argparse `choices=` guards the CLI, but the module must also fail cleanly if
    # called directly with a bad target rather than rendering garbage.
    home = _install_home(tmp_path, ["assistant"])
    rc = deploy.run(
        "mesos",
        home=home,
        env_path=home / "config" / ".env",
        agents_dir=home / "agents",
        server_urls="localhost:9092",
    )
    assert rc != 0
    assert "error" in capsys.readouterr().out.lower()




def test_k8s_renders_one_deployment_per_mcp_server() -> None:
    """Each mcp.json server gets its own Deployment running
    ``calfkit-mcp <server>`` — the same per-server isolation the local
    supervisor encodes."""
    rendered = deploy.render_k8s(
        agent_ids=_AGENTS,
        server_urls=_BROKER,
        image="calfcord:latest",
        mcp_servers=["github"],
    )
    docs = list(yaml.safe_load_all(rendered))
    mcp = [
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "mcp-github"
    ]
    assert len(mcp) == 1
    container = mcp[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["calfkit-mcp", "github"]


def test_k8s_no_mcp_servers_renders_no_mcp_deployments() -> None:
    rendered = deploy.render_k8s(agent_ids=_AGENTS, server_urls=_BROKER, image="calfcord:latest")
    docs = list(yaml.safe_load_all(rendered))
    assert not [
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"].startswith("mcp-")
    ]
