"""Tests for ``calfcord doctor`` (src/calfcord/cli/doctor.py)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from calfcord.cli import doctor

# A recognizable bot token that must NEVER appear in doctor's output.
TOKEN = "SENTINEL_TOKEN_do_not_leak_42"


# --------------------------------------------------------------------- _parse_broker


@pytest.mark.parametrize(
    "url,expected",
    [
        ("localhost:19092", ("localhost", 19092)),
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
