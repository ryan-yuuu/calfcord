"""Tests for ``calfcord doctor`` (src/calfcord/cli/doctor.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from calfcord.agents.definition import AgentDefinition
from calfcord.cli import doctor
from calfcord.health.heartbeat import Heartbeat

# A recognizable bot token that must NEVER appear in doctor's output.
TOKEN = "SENTINEL_TOKEN_do_not_leak_42"


# --------------------------------------------------------------------- _parse_broker


@pytest.mark.parametrize(
    "url,expected",
    [
        ("localhost:9094", ("localhost", 9094)),  # explicit non-default port is honored
        ("localhost", ("localhost", 9092)),  # bare host defaults to 9092
        ("b1:9092,b2:9093", ("b1", 9092)),  # first endpoint of a comma list
        ("kafka://h:9092", ("h", 9092)),  # scheme stripped
        ("[::1]:9092", ("::1", 9092)),  # ipv6 with port
        ("[::1]", ("::1", 9092)),  # ipv6 without port
        ("  host:9092  ", ("host", 9092)),  # surrounding whitespace
    ],
)
def test_parse_broker_valid(url, expected):
    assert doctor._parse_broker(url) == expected


@pytest.mark.parametrize("url", ["", "   ", "host:abc", ":9092", "host:99999", "host:0", "host:-1"])
def test_parse_broker_invalid(url):
    assert doctor._parse_broker(url) is None


# --------------------------------------------------------------------- helpers


def _seed_agent(agents_dir: Path, name: str, *, valid: bool = True) -> None:
    body = f"You are {name}." if valid else ""  # empty body -> parse fails
    (agents_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndisplay_name: {name.title()}\ndescription: a test agent\n---\n{body}\n",
        encoding="utf-8",
    )


def _factory(handler):
    """A client_factory yielding an httpx.Client backed by a MockTransport handler."""
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def _resp_ok(request):
    return httpx.Response(200, json={"username": "TestBot"})


def _resp_401(request):
    return httpx.Response(401, json={"message": "401: Unauthorized"})


def _raise_net(request):
    raise httpx.ConnectError("network down")


def _resp_non_json(request):
    return httpx.Response(200, text="<html>edge proxy interstitial</html>")


def _resp_non_dict(request):
    return httpx.Response(200, json=["not", "a", "dict"])


def _resp_403(request):
    return httpx.Response(403, json={"message": "403: Forbidden"})


def _resp_429(request):
    return httpx.Response(429, json={"message": "rate limited"})


def _resp_500(request):
    return httpx.Response(500, text="server error")


def _boom_factory():
    raise AssertionError("the network must not be called")


def _setup(
    monkeypatch,
    tmp_path,
    *,
    token=TOKEN,
    appid="123456789",
    broker="localhost:9092",
    reachable=True,
    make_env=True,
):
    """Build a healthy install layout + effective env; return (env_path, agents_dir)."""
    env_path = tmp_path / "config" / ".env"
    if make_env:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("DISCORD_BOT_TOKEN=seeded\n", encoding="utf-8")  # presence only
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    _seed_agent(agents_dir, "scribe")

    for key, val in (("DISCORD_BOT_TOKEN", token), ("DISCORD_APPLICATION_ID", appid), ("CALF_HOST_URL", broker)):
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)

    monkeypatch.setattr(doctor, "_tcp_reachable", lambda host, port, timeout=2.0: reachable)
    return env_path, agents_dir


# --------------------------------------------------------------------- run() behaviors


def test_all_pass_returns_0_and_shows_bot_name(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0
    assert "✗" not in out and "⚠" not in out
    assert "TestBot" in out


def test_missing_token_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, token=None)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    assert rc == 1
    assert "✗" in capsys.readouterr().out


def test_broker_unset_warns_not_fail(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, broker=None)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0  # a warning never fails the run
    assert "⚠" in out


def test_broker_unreachable_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, reachable=False)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    assert rc == 1
    assert "✗" in capsys.readouterr().out


@pytest.mark.parametrize(
    "handler,rc,needle",
    [
        (_resp_401, 1, "rejected"),      # token not accepted -> hard fail
        (_resp_403, 1, "rejected"),      # forbidden is also "won't boot" -> hard fail
        (_resp_429, 0, "rate-limited"),  # rate limited -> warn, never fail
        (_resp_500, 0, "⚠"),             # unexpected 5xx -> warn
        (_resp_non_json, 0, "⚠"),        # 200 + non-JSON body must not crash -> warn
        (_resp_non_dict, 0, "⚠"),        # 200 + non-dict JSON -> warn
        (_raise_net, 0, "⚠"),            # transport error -> warn
    ],
)
def test_token_check_classifies_response(monkeypatch, tmp_path, capsys, handler, rc, needle):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    assert doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(handler)) == rc
    out = capsys.readouterr().out
    assert needle in out.lower()  # ⚠ has no lowercase form, so the same check works for symbols
    assert TOKEN not in out


def test_offline_skips_network(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    # _boom_factory raises if called; offline must not call it.
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, offline=True, client_factory=_boom_factory)
    out = capsys.readouterr().out
    assert rc == 0
    assert "✗" not in out  # token is present; presence-only check passes


def test_unparseable_agent_fails_and_names_it(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    _seed_agent(agents_dir, "broken", valid=False)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "broken" in out


def test_no_agents_warns(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    (agents_dir / "scribe.md").unlink()  # remove the seeded agent -> empty dir
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0
    assert "⚠" in out


def test_missing_env_fails_with_init_hint(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, make_env=False)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "calfcord init" in out


def test_appid_non_numeric_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, appid="not-a-number")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    assert rc == 1
    assert "✗" in capsys.readouterr().out


def test_unreadable_env_fails_cleanly(monkeypatch, tmp_path, capsys):
    # A non-UTF-8 .env must be reported, not crash doctor with a UnicodeDecodeError.
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    env_path.write_bytes(b"\xff\xfe not utf-8")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "unreadable" in out


def test_empty_config_warns(monkeypatch, tmp_path, capsys):
    # A present-but-empty .env (the fresh-install state) is a warning, not a "no config" failure.
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    env_path.write_text("# only a comment\n", encoding="utf-8")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 0  # config-empty is a warn; the real values come from os.environ
    assert "⚠" in out


def test_appid_missing_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, appid=None)
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "not set" in out


def test_broker_unparseable_fails(monkeypatch, tmp_path, capsys):
    env_path, agents_dir = _setup(monkeypatch, tmp_path, broker="host:abc")
    rc = doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(_resp_ok))
    out = capsys.readouterr().out
    assert rc == 1
    assert "unparseable" in out


def test_token_never_leaks_across_paths(monkeypatch, tmp_path, capsys):
    handlers = (_resp_ok, _resp_401, _resp_403, _resp_429, _resp_500, _raise_net, _resp_non_json, _resp_non_dict)
    for handler in handlers:
        env_path, agents_dir = _setup(monkeypatch, tmp_path / handler.__name__)
        doctor.run(env_path=env_path, agents_dir=agents_dir, client_factory=_factory(handler))
        captured = capsys.readouterr()
        assert TOKEN not in captured.out
        assert TOKEN not in captured.err


# =================================================================== runtime section
#
# When the daemon is up (detected via the bridge heartbeat) doctor adds a RUNTIME
# section on top of the 5 STATIC checks: daemon-alive (heartbeat freshness), a deep
# control-plane probe (publish ping + collect responses → confirm the org answers
# and list registered agents), and drift (agents running per Process Compose vs.
# registered per the probe). Every world-touching dependency is injected so no real
# broker / supervisor / heartbeat file is needed (§4.4 / §12.1 / §13.3).

_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)


def _beat(component: str = "bridge", *, age_s: float = 1.0, identity: str = "TestBot") -> Heartbeat:
    """A bridge heartbeat ``age_s`` seconds old relative to ``_NOW``."""
    last = _NOW - timedelta(seconds=age_s)
    return Heartbeat(
        component=component,
        pid=4242,
        started_at=last - timedelta(seconds=60),
        last_beat=last,
        status="healthy",
        identity=identity,
    )


def _reader(beats: dict[str, Heartbeat]):
    """A ``read_beat_fn(home, component)`` stub backed by an in-memory beat map."""
    return lambda home, component: beats.get(component)


def _probe_err(exc: Exception):
    """An async ``probe_fn`` that raises (broker unreachable for the deep probe)."""

    async def _run(server_urls):
        raise exc

    return _run


def _agent(agent_id: str) -> AgentDefinition:
    return AgentDefinition(
        name=agent_id,
        display_name=agent_id.title(),
        description="a live agent",
        system_prompt=f"You are {agent_id}.",
    )


class _StubPCClient:
    """A minimal ProcessComposeClient stand-in exposing only ``list_processes``.

    ``raises`` models the supervisor being unreachable for the drift read — a REST
    miss that is a SEPARATE failure domain from the (alive) bridge, so doctor must
    degrade to a warn rather than crash on it.
    """

    def __init__(self, processes, *, raises: Exception | None = None):
        self._processes = processes
        self._raises = raises

    async def list_processes(self):
        if self._raises is not None:
            raise self._raises
        return self._processes


def _runtime_setup(monkeypatch, tmp_path):
    """A healthy STATIC layout plus an install ``home`` for the runtime section."""
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    home = tmp_path  # any path: the heartbeat reader is stubbed, so it is never read
    return env_path, agents_dir, home


def _run_runtime(
    monkeypatch,
    tmp_path,
    *,
    beats,
    probe_fn,
    processes=None,
):
    """Invoke doctor with the runtime seams wired; return (rc, stdout)."""
    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        server_urls="localhost:9092",
        now=_NOW,
        read_beat_fn=_reader(beats),
        probe_fn=probe_fn,
        pc_client=_StubPCClient(processes if processes is not None else []),
    )
    return rc, monkeypatch  # caller reads capsys separately


def test_daemon_down_skips_runtime_section(monkeypatch, tmp_path, capsys):
    # No bridge heartbeat at all -> the daemon is not running. doctor reports the
    # STATIC checks and explicitly notes the runtime section was skipped; it must
    # NOT fail solely because the workspace is closed (read-only, closed is valid).
    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        server_urls="localhost:9092",
        now=_NOW,
        read_beat_fn=_reader({}),  # no beats -> daemon down
        probe_fn=_probe_err(AssertionError("probe must not run when daemon is down")),
        pc_client=_StubPCClient([]),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "calfcord start" in out  # the next-step hint for a closed workspace
    # The deep-probe / drift lines never render when the daemon is down.
    assert "registered agent" not in out.lower()


def test_no_home_skips_runtime_section(monkeypatch, tmp_path, capsys):
    # A dev invocation (no install home) cannot locate heartbeats; the runtime
    # section is simply absent and the static contract is unchanged.
    env_path, agents_dir = _setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=None,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon" not in out.lower()


def test_stale_heartbeat_fails_as_zombie(monkeypatch, tmp_path, capsys):
    # A bridge heartbeat exists but is older than the TTL -> a wedged/zombie
    # daemon. That is a hard fail framed as a fix (restart the workspace).
    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat(age_s=600)},  # well past the 10s TTL
        probe_fn=_probe_err(AssertionError("probe must not run on a stale daemon")),
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "✗" in out
    assert "calfcord" in out  # a fix is named (restart)


def test_deep_probe_lists_registered_agents(monkeypatch, tmp_path, capsys):
    # Daemon fresh + the deep probe answers with two agents -> the runtime section
    # is all-green and the registered agents are named.
    async def probe(server_urls):
        return [_agent("scribe"), _agent("aksel")]

    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat(identity="MyBot")},
        probe_fn=probe,
        processes=[
            {"name": "bridge", "status": "Running"},
            {"name": "scribe", "status": "Running"},
            {"name": "aksel", "status": "Running"},
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "scribe" in out and "aksel" in out
    assert "MyBot" in out  # the bridge identity is surfaced


def test_deep_probe_empty_warns_no_agents_answering(monkeypatch, tmp_path, capsys):
    # Daemon up but zero agents answer the ping -> warn (the "green but no replies"
    # symptom only the deep probe can see). A warning never fails the run.
    async def probe(server_urls):
        return []

    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat()},
        probe_fn=probe,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "⚠" in out
    assert "agent start" in out  # the fix: bring an agent online


def test_deep_probe_error_warns_not_crashes(monkeypatch, tmp_path, capsys):
    # The bridge is alive (fresh beat) but the deep probe cannot reach the broker.
    # doctor must degrade to a warning, never crash, and never fail the run on it.
    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat()},
        probe_fn=_probe_err(RuntimeError("broker unreachable")),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "⚠" in out


def test_drift_process_up_but_not_registered_warns(monkeypatch, tmp_path, capsys):
    # `scribe` is Running in Process Compose but does NOT answer the probe -> drift
    # (process up but never joined / wedged). Surfaced as a warn naming the agent.
    async def probe(server_urls):
        return [_agent("aksel")]  # only aksel answers

    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat()},
        probe_fn=probe,
        processes=[
            {"name": "bridge", "status": "Running"},
            {"name": "scribe", "status": "Running"},  # up locally, not registered
            {"name": "aksel", "status": "Running"},
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "⚠" in out
    assert "scribe" in out  # the drifting process is named


def test_no_drift_when_running_matches_registered(monkeypatch, tmp_path, capsys):
    # Physical roster == logical roster -> no drift finding (ok).
    async def probe(server_urls):
        return [_agent("scribe")]

    rc, _ = _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat()},
        probe_fn=probe,
        processes=[
            {"name": "bridge", "status": "Running"},
            {"name": "scribe", "status": "Running"},
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "drift" in out.lower()  # the drift check renders, and is ok
    assert "✗" not in out


def test_drift_supervisor_unreachable_warns_not_crashes(monkeypatch, tmp_path, capsys):
    # Fix #6: the bridge is alive (fresh beat) and the deep probe answers, but the
    # supervisor (Process Compose) is a SEPARATE failure domain — its
    # `list_processes` can miss (REST down / wrong port). That read is NOT wrapped
    # like the deep-probe above it, so an unhandled raise would crash a READ-ONLY
    # doctor. doctor must degrade the drift check to a warn instead.
    async def probe(server_urls):
        return [_agent("scribe")]

    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        server_urls="localhost:9092",
        now=_NOW,
        read_beat_fn=_reader({"bridge": _beat()}),
        probe_fn=probe,
        pc_client=_StubPCClient([], raises=RuntimeError("supervisor REST down")),
    )
    out = capsys.readouterr().out
    assert rc == 0  # a warn never fails a read-only doctor
    assert "⚠" in out
    assert "drift" in out.lower()
    # The deep probe still rendered its result (the supervisor miss is isolated).
    assert "scribe" in out


def test_runtime_section_keeps_static_checks(monkeypatch, tmp_path, capsys):
    # The 5 STATIC checks must still run (and still fail the run) even when the
    # runtime section is active: a missing app id is still a hard ✗.
    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    monkeypatch.delenv("DISCORD_APPLICATION_ID", raising=False)

    async def probe(server_urls):
        return [_agent("scribe")]

    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        server_urls="localhost:9092",
        now=_NOW,
        read_beat_fn=_reader({"bridge": _beat()}),
        probe_fn=probe,
        pc_client=_StubPCClient([{"name": "scribe", "status": "Running"}]),
    )
    out = capsys.readouterr().out
    assert rc == 1  # the static app-id failure still fails the whole run
    assert "discord app id" in out


def test_runtime_token_never_leaks(monkeypatch, tmp_path, capsys):
    # The token must not leak through the new runtime paths either.
    async def probe(server_urls):
        return [_agent("scribe")]

    _run_runtime(
        monkeypatch,
        tmp_path,
        beats={"bridge": _beat()},
        probe_fn=probe,
        processes=[{"name": "scribe", "status": "Running"}],
    )
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


# --------------------------------------------------------- production seam defaults
#
# The tests above inject every runtime seam; these two exercise the production
# *defaults* (the real heartbeat reader, the probe adapter, the per-home PC client)
# without a real broker — so a wiring regression in the default-resolution path is
# caught, not silently un-covered.


def test_default_read_beat_resolves_from_disk_daemon_down(monkeypatch, tmp_path, capsys):
    # With `home` set but no seams injected, doctor must default to the real
    # `read_beat` and (finding no on-disk beat) report the workspace closed. This
    # covers the `now`/`server_urls`/`read_beat` default-resolution branches with no
    # broker — the probe/PC defaults never construct because the daemon is down.
    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,  # only home; every runtime seam defaults
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "calfcord start" in out  # closed-workspace next-step hint


def test_default_probe_and_pc_client_run_against_disk_beat(monkeypatch, tmp_path, capsys):
    # A real fresh on-disk beat + the default seams: doctor must build the default
    # probe adapter and the per-home ProcessComposeClient and run the deep probe.
    # We stub only the two boundaries that would otherwise hit a broker/supervisor:
    # `probe_live_roster` (the probe adapter wraps it) and the PC client's
    # `list_processes` — proving the default wiring is exercised end-to-end.
    from calfcord.health.heartbeat import write_beat

    env_path, agents_dir, home = _runtime_setup(monkeypatch, tmp_path)
    write_beat(home, "bridge", status="healthy", identity="DiskBot", now=_NOW)

    async def fake_probe_live_roster(urls, **kwargs):
        return [_agent("scribe")]

    monkeypatch.setattr(doctor, "datetime", _FrozenDatetime)
    monkeypatch.setattr(
        "calfcord.control_plane.probe.probe_live_roster", fake_probe_live_roster
    )

    async def fake_list_processes(self):
        return [{"name": "scribe", "status": "Running"}]

    monkeypatch.setattr(
        "calfcord.supervisor.client.ProcessComposeClient.list_processes",
        fake_list_processes,
    )

    rc = doctor.run(
        env_path=env_path,
        agents_dir=agents_dir,
        client_factory=_factory(_resp_ok),
        home=home,
        server_urls=None,  # exercises the CALF_HOST_URL default-resolution branch
        # now / read_beat_fn / probe_fn / pc_client all default
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "scribe" in out  # the default probe adapter returned the live roster
    assert "DiskBot" in out  # the real on-disk beat was read by the default reader


class _FrozenDatetime(datetime):
    """A ``datetime`` whose ``now()`` is pinned to ``_NOW`` so the default
    freshness clock in :func:`doctor._runtime_section` is deterministic."""

    @classmethod
    def now(cls, tz=None):  # matches datetime.now signature
        return _NOW
