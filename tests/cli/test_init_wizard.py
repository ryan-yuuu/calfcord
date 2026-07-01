"""Tests for the resumable, ends-live ``disco init`` wizard (§4.6 / §11 / §12.6).

The reworked ``init`` is *one continuous, resumable* guided session that COMPOSES
the existing seams (provider/model/agent via ``agent_create``, Discord
auto-discovery via ``discord_discovery``, the substrate/roster orchestration via
``lifecycle.start`` + ``roster.agent_start``, and first-reply detection) and
persists progress through the §12.7 checkpoint. Every world-touching dependency
(the Discord HTTP calls, the start/agent-start coroutines, the first-reply
watcher, the clock) is INJECTED, so the whole wizard is exercised here with no
TTY, no Discord, no broker, and no process supervisor.

The provider sub-flow is delegated to ``agent_create.create_agent`` (where
``configure_provider`` lives), so — exactly like ``test_init.py`` — these tests
stub ``configure_provider`` to a fixed ``(provider, model)`` and never touch a
provider SDK / network / key.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from calfkit.exceptions import MeshUnavailableError

from calfcord.agents.definition import parse_agent_md
from calfcord.cli import agent_create, discord_discovery, init, setup_state
from calfcord.cli._envfile import read_env, upsert
from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli.discord_discovery import BotIdentity, ChannelListing, Guild, PostableChannel

_FIXED_PROVIDER = ("anthropic", "claude-haiku-4-5")
_FIXED_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return _FIXED_NOW


class FakePrompter:
    """Scripted :class:`Prompter`: each method pops the next queued answer.

    Mirrors the fake in ``test_init.py`` — answers are queued per prompt kind so
    a test scripts only the kinds its path hits, in call order; an exhausted
    queue raises rather than hanging, surfacing a miscounted script as a clear
    failure.
    """

    def __init__(
        self,
        *,
        selects: list[str] | None = None,
        texts: list[str] | None = None,
        secrets: list[str] | None = None,
        confirms: list[bool] | None = None,
        checkboxes: list[list[str]] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self._selects = deque(selects or [])
        self._texts = deque(texts or [])
        self._secrets = deque(secrets or [])
        self._confirms = deque(confirms or [])
        self._checkboxes = deque(checkboxes or [])
        self._events = events
        self.last_checkbox_choices: list[Choice] = []
        self.select_choices_log: list[list[Choice]] = []

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        self.select_choices_log.append(choices)
        if not self._selects:
            raise AssertionError(f"unexpected select(): {message!r}")
        return self._selects.popleft()

    def text(self, message: str, *, default: str = "") -> str:
        if not self._texts:
            raise AssertionError(f"unexpected text(): {message!r}")
        return self._texts.popleft()

    def secret(self, message: str) -> str:
        if not self._secrets:
            raise AssertionError(f"unexpected secret(): {message!r}")
        return self._secrets.popleft()

    def confirm(self, message: str, *, default: bool = False) -> bool:
        if self._events is not None:
            self._events.append("prompted")
        if not self._confirms:
            raise AssertionError(f"unexpected confirm(): {message!r}")
        return self._confirms.popleft()

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        self.last_checkbox_choices = choices
        if not self._checkboxes:
            return [c.value for c in choices if c.checked]
        return self._checkboxes.popleft()


@pytest.fixture(autouse=True)
def _stub_configure_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the provider sub-flow with a fixed ``(provider, model)`` (no SDK)."""

    def _fixed(prompter: object, **_: object) -> tuple[str, str]:
        return _FIXED_PROVIDER

    monkeypatch.setattr(agent_create, "configure_provider", _fixed)


# --------------------------------------------------------------------------- #
# Stub seams for the Discord sub-flow and the live finish
# --------------------------------------------------------------------------- #


class _DiscordStub:
    """A record-and-reply stand-in for the injected ``discord_discovery`` calls.

    Each method matches the corresponding ``discord_discovery`` function's
    signature shape closely enough for the wizard to call it the same way it
    would the real module, but returns scripted values (or raises a scripted
    error) with no network.
    """

    def __init__(
        self,
        *,
        identity: BotIdentity | Exception | None = None,
        guilds: list[Guild] | None = None,
        channels: ChannelListing | Exception | None = None,
        join_result: list[Guild] | Exception | None = None,
    ) -> None:
        self._identity = identity if identity is not None else BotIdentity(id="botid", username="MyBot")
        self._guilds = (
            guilds if guilds is not None else [Guild(id="g1", name="My Server", owner=True, base_permissions=0)]
        )
        self._channels = (
            channels
            if channels is not None
            else ChannelListing(postable=[PostableChannel(id="c1", name="general")], unpostable=[])
        )
        self._join_result = join_result if join_result is not None else self._guilds
        self.verify_calls = 0
        self.poll_calls = 0

    def verify(self, token: str, *, client_factory=None) -> BotIdentity:
        self.verify_calls += 1
        if isinstance(self._identity, Exception):
            raise self._identity
        return self._identity

    def poll(self, token: str, **_: object) -> list[Guild]:
        self.poll_calls += 1
        if isinstance(self._join_result, Exception):
            raise self._join_result
        return self._join_result

    def guilds(self, token: str, *, client_factory=None) -> list[Guild]:
        return self._guilds

    def channels(self, token: str, guild_id, *, client_factory=None) -> ChannelListing:
        if isinstance(self._channels, Exception):
            raise self._channels
        return self._channels


class _FinishStub:
    """Records the live-finish orchestration calls and returns scripted codes.

    ``events`` (optional) is a shared ordered log: each orchestration call appends
    its name so a test can assert relative ordering against the prompter's prompts
    (used to prove the first-reply watcher joins BEFORE the human is prompted).
    The injected ``ready`` :class:`asyncio.Event` (the watcher-joined signal) is
    set the instant ``first_reply`` is entered, mirroring how the real watcher
    signals readiness after ``worker.start()`` joins the consumer group.
    """

    def __init__(
        self,
        *,
        start_rc: int = 0,
        agent_rc: int = 0,
        reply: bool = True,
        pc_binary: str | Exception = "/usr/bin/process-compose",
        events: list[str] | None = None,
    ) -> None:
        self._start_rc = start_rc
        self._agent_rc = agent_rc
        self._reply = reply
        self._pc_binary = pc_binary
        self._events = events
        self.start_calls: list[dict] = []
        self.agent_calls: list[dict] = []
        self.reply_calls: list[dict] = []

    async def start(self, home, **kwargs) -> int:
        self.start_calls.append({"home": home, **kwargs})
        return self._start_rc

    async def agent_start(self, home, **kwargs) -> int:
        self.agent_calls.append({"home": home, **kwargs})
        return self._agent_rc

    async def first_reply(self, server_urls, *, ready=None, **kwargs) -> bool:
        if self._events is not None:
            self._events.append("watcher_started")
        # The real watcher sets this after ``worker.start()`` joins the group; the
        # stub signals readiness immediately so the caller can proceed to prompt.
        if ready is not None:
            ready.set()
        self.reply_calls.append({"server_urls": server_urls, **kwargs})
        return self._reply

    def pc_binary(self) -> str:
        if isinstance(self._pc_binary, Exception):
            raise self._pc_binary
        return self._pc_binary


def _run(
    prompter: FakePrompter,
    tmp_path: Path,
    *,
    agents_dir: Path | None = None,
    env_path: Path | None = None,
    home: Path | None = None,
    discord: _DiscordStub | None = None,
    finish: _FinishStub | None = None,
    server_urls: str = "localhost:9092",
    open_url: Callable[[str], None] | None = None,
) -> int:
    """Drive ``init.run`` with all world-touching seams stubbed.

    ``open_url`` defaults to a no-op so no test ever pops a real browser.
    """
    discord = discord or _DiscordStub()
    finish = finish or _FinishStub()
    return init.run(
        prompter,
        env_path=env_path or (tmp_path / ".env"),
        agents_dir=agents_dir or (tmp_path / "agents"),
        home=home,
        server_urls=server_urls,
        now=_now,
        open_url_fn=open_url or (lambda url: None),
        verify_identity_fn=discord.verify,
        poll_joined_fn=discord.poll,
        list_guilds_fn=discord.guilds,
        list_channels_fn=discord.channels,
        start_fn=finish.start,
        agent_start_fn=finish.agent_start,
        first_reply_fn=finish.first_reply,
        pc_binary_fn=finish.pc_binary,
    )


def _prompter(
    *,
    name: str = "scribe",
    description: str = "d",
    discord_token: str = "tok-abc",
    app_id: str = "12345",
    guild: str = "g1",
    channel: str = "c1",
    broker: str = "native",
    broker_url: str = "broker:9092",
    say_hello: bool = True,
    checkboxes: list[list[str]] | None = None,
    extra_selects: list[str] | None = None,
    extra_texts: list[str] | None = None,
    extra_secrets: list[str] | None = None,
) -> FakePrompter:
    """Build a prompter scripting one full native happy-path pass.

    Consumed prompts (provider sub-flow stubbed out):
      text(name), text(description), checkbox(tools),
      secret(discord token),
      select(guild), select(channel),
      select(broker) [+ text(broker_url) on the ``url`` branch],
      confirm(say-hello-now).
    ``app_id`` is a text prompt for the application id (needed for the invite URL).
    """
    texts = [name, description, app_id]
    if broker == "url":
        texts.append(broker_url)
    texts += extra_texts or []
    selects = [guild, channel, broker]
    selects += extra_selects or []
    secrets = [discord_token]
    secrets += extra_secrets or []
    confirms = [say_hello]
    return FakePrompter(
        selects=selects,
        texts=texts,
        secrets=secrets,
        confirms=confirms,
        checkboxes=checkboxes,
    )


# --------------------------------------------------------------------------- #
# Protocol guard
# --------------------------------------------------------------------------- #


def test_fake_prompter_satisfies_protocol() -> None:
    assert isinstance(FakePrompter(), Prompter)


# --------------------------------------------------------------------------- #
# Agent + provider step (reused create flow)
# --------------------------------------------------------------------------- #


def test_native_happy_path_creates_agent_and_persists_default_provider(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    rc = _run(_prompter(name="scribe", description="Takes notes"), tmp_path, agents_dir=agents_dir, home=tmp_path)
    assert rc == 0

    agent = parse_agent_md(agents_dir / "scribe.md")
    assert agent.agent_id == "scribe"
    assert agent.provider == "anthropic"
    assert agent.model == "claude-haiku-4-5"
    assert read_env(tmp_path / ".env")["CALFKIT_AGENT_DEFAULT_PROVIDER"] == "anthropic"


def test_agent_write_failure_aborts_before_discord(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed agent write returns non-zero and never reaches Discord/finish."""
    from calfcord.cli import _agents

    def _boom(path: Path, payload: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_agents, "atomic_write", _boom)
    discord = _DiscordStub()
    rc = _run(_prompter(), tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert rc != 0
    assert "error: could not create agent" in out
    assert discord.verify_calls == 0  # never reached the Discord step


# --------------------------------------------------------------------------- #
# Discord sub-flow: token echo, invite + intents, poll, pick guild/channel
# --------------------------------------------------------------------------- #


def test_token_verified_and_identity_echoed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    discord = _DiscordStub(identity=BotIdentity(id="42", username="ScribeBot"))
    assert _run(_prompter(), tmp_path, home=tmp_path, discord=discord) == 0
    out = capsys.readouterr().out
    assert discord.verify_calls >= 1
    assert "Connected as ScribeBot" in out


def test_invite_url_and_intents_reminder_and_ctrlc_banner_printed_before_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    discord = _DiscordStub()
    assert _run(_prompter(app_id="999"), tmp_path, home=tmp_path, discord=discord) == 0
    out = capsys.readouterr().out
    # The invite URL is built from the application id.
    assert "client_id=999" in out
    # The two privileged intents are named inline (§12.6).
    assert discord_discovery.INTENTS_REMINDER in out
    # And the resumability banner appears before the poll (Ctrl-C is safe).
    assert "Ctrl-C" in out
    idx_banner = out.index("Ctrl-C")
    idx_connected = out.index("Connected as")
    assert idx_connected < idx_banner  # token echoed before the wait banner


def test_invite_step_opens_browser_and_still_prints_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The browser pop is a best-effort extra: the opener gets the exact invite
    URL, and the URL is printed regardless so a wrong platform guess can never
    hide the link."""
    opened: list[str] = []
    discord = _DiscordStub()
    rc = _run(
        _prompter(app_id="999"), tmp_path, home=tmp_path, discord=discord, open_url=opened.append
    )
    assert rc == 0
    assert opened == [discord_discovery.invite_url("999")]
    out = capsys.readouterr().out
    assert discord_discovery.invite_url("999") in out  # printed even though it opened


def test_invite_step_browser_open_failure_is_swallowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken opener (no browser, weird platform) must not derail the wizard."""

    def boom(url: str) -> None:
        raise RuntimeError("no browser here")

    discord = _DiscordStub()
    assert _run(_prompter(app_id="999"), tmp_path, home=tmp_path, discord=discord, open_url=boom) == 0
    assert discord_discovery.invite_url("999") in capsys.readouterr().out


def _force_tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Pin the TTY guard: pytest captures stdout, so isatty() is False by
    default and every guard test must state the terminal state it assumes."""
    monkeypatch.setattr(init.sys.stdout, "isatty", lambda: value)


def test_try_open_browser_skips_when_stdout_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Piped/captured runs (pytest included) must never pop a browser tab —
    this is the guard that keeps the test suite itself side-effect free."""
    calls: list[str] = []
    monkeypatch.setattr(init.webbrowser, "open", calls.append)
    _force_tty(monkeypatch, False)
    monkeypatch.setattr(init.sys, "platform", "darwin")
    init._try_open_browser("https://example.test")
    assert calls == []


def test_try_open_browser_skips_over_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(init.webbrowser, "open", calls.append)
    _force_tty(monkeypatch, True)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22")
    init._try_open_browser("https://example.test")
    assert calls == []


def test_try_open_browser_skips_headless_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(init.webbrowser, "open", calls.append)
    _force_tty(monkeypatch, True)
    for var in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(init.sys, "platform", "linux")
    init._try_open_browser("https://example.test")
    assert calls == []


def test_try_open_browser_opens_on_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(init.webbrowser, "open", calls.append)
    _force_tty(monkeypatch, True)
    for var in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(init.sys, "platform", "darwin")
    init._try_open_browser("https://example.test")
    assert calls == ["https://example.test"]


def test_try_open_browser_swallows_webbrowser_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str) -> bool:
        raise RuntimeError("webbrowser exploded")

    monkeypatch.setattr(init.webbrowser, "open", boom)
    _force_tty(monkeypatch, True)
    for var in ("SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(init.sys, "platform", "darwin")
    init._try_open_browser("https://example.test")  # must not raise


def test_guild_and_channel_persisted_from_pick_lists(tmp_path: Path) -> None:
    discord = _DiscordStub(
        guilds=[Guild(id="g7", name="Server7", owner=True, base_permissions=0)],
        channels=ChannelListing(postable=[PostableChannel(id="c9", name="lobby")], unpostable=[]),
    )
    assert _run(_prompter(guild="g7", channel="c9"), tmp_path, home=tmp_path, discord=discord) == 0
    env = read_env(tmp_path / ".env")
    assert env["DISCORD_BOT_TOKEN"] == "tok-abc"
    assert env["DISCORD_GUILD_ID"] == "g7"
    assert env["DISCORD_DEFAULT_CHANNEL_ID"] == "c9"


def test_bad_token_reprompts_for_a_new_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A rejected token (fatal) re-prompts; re-prompting the SAME token is pointless."""
    # First identity call raises auth error, second succeeds.
    calls = {"n": 0}

    def verify(token: str, *, client_factory=None) -> BotIdentity:
        calls["n"] += 1
        if calls["n"] == 1:
            raise discord_discovery.DiscordAuthError("token rejected by Discord (401)")
        return BotIdentity(id="1", username="OK")

    discord = _DiscordStub()
    discord.verify = verify  # type: ignore[assignment]
    # Two secret prompts: the rejected token, then a fresh one.
    p = _prompter(extra_secrets=["tok-good"])
    assert _run(p, tmp_path, home=tmp_path, discord=discord) == 0
    out = capsys.readouterr().out
    assert "rejected" in out.lower() or "token" in out.lower()
    assert calls["n"] == 2


def test_join_timeout_surfaces_common_causes_and_no_server_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A poll timeout (user never authorized) surfaces actionable causes (§12.6)."""
    discord = _DiscordStub(
        join_result=discord_discovery.DiscordJoinTimeoutError("bot did not join a server within 300s")
    )
    # The wizard cannot pick a guild/channel after a join timeout; it degrades
    # the Discord step but still runs the (Discord-independent) live finish.
    p = FakePrompter(
        selects=["native"],  # no guild/channel pick after the timeout
        texts=["scribe", "d", "12345"],
        secrets=["tok-abc"],
        confirms=[True],  # the in-flow say-hello prompt
    )
    rc = _run(p, tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    # Non-fatal: the wizard still configured what it could and surfaced the hint.
    assert "Authorize" in out or "authorize" in out
    assert "server" in out.lower()
    assert rc == 0


def test_zero_postable_channels_surfaced_explicitly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A guild with channels the bot can SEE but not POST in is explained, not hidden."""
    discord = _DiscordStub(
        channels=ChannelListing(
            postable=[],
            unpostable=[PostableChannel(id="c1", name="general")],
        )
    )
    p = FakePrompter(
        selects=["g1", "native"],  # guild pick, then broker (no channel pick possible)
        texts=["scribe", "d", "12345"],
        secrets=["tok-abc"],
        confirms=[True],  # the in-flow say-hello prompt (live finish still runs)
    )
    rc = _run(p, tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert "can't post" in out.lower() or "cannot post" in out.lower() or "no channel" in out.lower()
    assert rc == 0


def test_transient_token_verify_error_continues(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A transient Discord error verifying the token must not block setup — the
    token is accepted (the bridge re-validates at boot) and the flow continues."""
    discord = _DiscordStub()
    discord.verify = lambda token, **_: (_ for _ in ()).throw(  # type: ignore[assignment]
        discord_discovery.DiscordUnavailableError("could not reach Discord (/users/@me)")
    )
    rc = _run(_prompter(), tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not verify token" in out
    # The token was still persisted despite the verify blip.
    assert read_env(tmp_path / ".env")["DISCORD_BOT_TOKEN"] == "tok-abc"


def test_poll_transient_error_degrades(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A non-timeout poll error (rate-limited / unreachable) surfaces and degrades
    the Discord step rather than aborting the wizard."""
    discord = _DiscordStub(join_result=discord_discovery.DiscordUnavailableError("could not reach Discord"))
    p = FakePrompter(
        selects=["native"],  # no guild/channel pick after the poll error
        texts=["scribe", "d", "12345"],
        secrets=["tok-abc"],
        confirms=[True],
    )
    rc = _run(p, tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not confirm the bot joined" in out


def test_zero_guilds_surfaced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A poll that returns zero guilds is explained, not offered as an empty menu."""
    discord = _DiscordStub(join_result=[])
    p = FakePrompter(
        selects=["native"],  # no guild pick possible
        texts=["scribe", "d", "12345"],
        secrets=["tok-abc"],
        confirms=[True],
    )
    rc = _run(p, tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert rc == 0
    assert "isn't in any server" in out


def test_channel_list_error_degrades(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An error listing channels surfaces and degrades (the channel is unset)."""
    discord = _DiscordStub(channels=discord_discovery.DiscordUnavailableError("could not reach Discord (/channels)"))
    p = FakePrompter(
        selects=["g1", "native"],  # guild pick, then broker (channel list failed)
        texts=["scribe", "d", "12345"],
        secrets=["tok-abc"],
        confirms=[True],
    )
    rc = _run(p, tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not list channels" in out


def test_no_text_channels_at_all_surfaced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A guild with neither postable nor unpostable text channels is explained."""
    discord = _DiscordStub(channels=ChannelListing(postable=[], unpostable=[]))
    p = FakePrompter(
        selects=["g1", "native"],
        texts=["scribe", "d", "12345"],
        secrets=["tok-abc"],
        confirms=[True],
    )
    rc = _run(p, tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no text channels the bot can post in" in out


def test_unpostable_channels_noted_alongside_postable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When some channels are postable and some aren't, the gap is noted (not hidden)."""
    discord = _DiscordStub(
        channels=ChannelListing(
            postable=[PostableChannel(id="c1", name="general")],
            unpostable=[PostableChannel(id="c2", name="locked")],
        )
    )
    assert _run(_prompter(guild="g1", channel="c1"), tmp_path, home=tmp_path, discord=discord) == 0
    out = capsys.readouterr().out
    assert "can see but can't post in" in out
    assert "#locked" in out


def test_owner_guild_labelled_in_pick_list(tmp_path: Path) -> None:
    """An owned guild is labelled '(owner)' in the pick-list (cosmetic but real)."""
    discord = _DiscordStub(guilds=[Guild(id="g1", name="Mine", owner=True, base_permissions=0)])
    p = _prompter(guild="g1", channel="c1")
    assert _run(p, tmp_path, home=tmp_path, discord=discord) == 0
    # The guild select (the first select) offered the owner-labelled choice.
    guild_labels = [c.label for c in p.select_choices_log[0]]
    assert any("(owner)" in label for label in guild_labels)


def test_empty_app_id_keeps_existing(tmp_path: Path) -> None:
    """An empty application-id answer keeps the existing one (re-run safe)."""
    env_path = tmp_path / ".env"
    upsert(env_path, {"DISCORD_APPLICATION_ID": "55555"})
    p = _prompter(app_id="")  # blank → keep existing
    assert _run(p, tmp_path, env_path=env_path, home=tmp_path) == 0
    assert read_env(env_path)["DISCORD_APPLICATION_ID"] == "55555"


def test_no_token_skips_discovery(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With no token at all, Discord discovery is skipped (the rest still lands)."""
    discord = _DiscordStub()
    p = FakePrompter(
        selects=["native"],  # no guild/channel pick (discovery skipped)
        texts=["scribe", "d"],  # name, description (no app-id prompt without a token)
        secrets=[""],  # no token
        confirms=[True],
    )
    rc = _run(p, tmp_path, home=tmp_path, discord=discord)
    out = capsys.readouterr().out
    assert rc == 0
    assert discord.verify_calls == 0
    assert "skipping discovery" in out


# --------------------------------------------------------------------------- #
# Broker step
# --------------------------------------------------------------------------- #


def test_broker_native_sets_local_url(tmp_path: Path) -> None:
    assert _run(_prompter(broker="native"), tmp_path, home=tmp_path) == 0
    assert read_env(tmp_path / ".env")["CALF_HOST_URL"] == "localhost:9092"


def test_broker_url_sets_given_url(tmp_path: Path) -> None:
    assert _run(_prompter(broker="url", broker_url="my-broker:9092"), tmp_path, home=tmp_path) == 0
    assert read_env(tmp_path / ".env")["CALF_HOST_URL"] == "my-broker:9092"


def test_broker_url_empty_on_fresh_install_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A fresh install ending with no broker can't start, so the URL branch warns
    when nothing is typed and nothing is already on disk."""
    rc = _run(_prompter(broker="url", broker_url=""), tmp_path, home=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert f"warning: no {init._BROKER_VAR} is set" in out
    assert init._BROKER_VAR not in read_env(tmp_path / ".env")


def test_non_numeric_application_id_warns_without_blocking(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A non-numeric DISCORD_APPLICATION_ID is flagged but still written + used for
    the invite URL (the typo is surfaced, not blocking)."""
    rc = _run(_prompter(app_id="not-a-number"), tmp_path, home=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DISCORD_APPLICATION_ID should be numeric" in out
    assert read_env(tmp_path / ".env")["DISCORD_APPLICATION_ID"] == "not-a-number"


# --------------------------------------------------------------------------- #
# Live finish: native happy path
# --------------------------------------------------------------------------- #


def test_live_finish_starts_substrate_then_agent_then_watches_reply(tmp_path: Path) -> None:
    finish = _FinishStub(reply=True)
    rc = _run(_prompter(name="scribe"), tmp_path, home=tmp_path, finish=finish)
    assert rc == 0
    # Substrate first, then the agent clocks in, then we watch for the reply.
    assert len(finish.start_calls) == 1
    assert len(finish.agent_calls) == 1
    assert len(finish.reply_calls) == 1
    assert finish.agent_calls[0]["name"] == "scribe"
    assert finish.reply_calls[0]["agent_id"] == "scribe"


def test_live_finish_uses_broker_url_written_by_wizard_not_pre_wizard(tmp_path: Path) -> None:
    """The live finish must use the broker URL the *wizard* wrote in its broker
    phase, NOT the pre-wizard ``server_urls`` captured before the wizard ran.

    The operator can change the broker inside the wizard (the ``url`` branch), so
    the effective ``CALF_HOST_URL`` on disk after the broker phase — not the value
    main.py sampled before — must be what ``lifecycle.start``'s broker probe and
    the first-reply watcher connect to (otherwise a wizard-configured broker is
    silently ignored)."""
    finish = _FinishStub(reply=True)
    # Pre-wizard server_urls is the stale default; the wizard's broker phase
    # writes a DIFFERENT url that the finish must pick up.
    rc = _run(
        _prompter(name="scribe", broker="url", broker_url="wizard-broker:9092"),
        tmp_path,
        home=tmp_path,
        finish=finish,
        server_urls="stale-prewizard:9092",
    )
    assert rc == 0
    assert finish.start_calls[0]["server_urls"] == "wizard-broker:9092"
    assert finish.agent_calls[0]["server_urls"] == "wizard-broker:9092"
    assert finish.reply_calls[0]["server_urls"] == "wizard-broker:9092"


def test_live_finish_native_broker_url_used_when_wizard_picks_native(tmp_path: Path) -> None:
    """Picking the native broker in the wizard makes the finish use the local
    ``localhost:9092`` the broker phase wrote — again from the .env, not the
    pre-wizard value."""
    finish = _FinishStub(reply=True)
    rc = _run(
        _prompter(name="scribe", broker="native"),
        tmp_path,
        home=tmp_path,
        finish=finish,
        server_urls="stale-prewizard:9092",
    )
    assert rc == 0
    assert finish.start_calls[0]["server_urls"] == init._LOCAL_BROKER_URL
    assert finish.reply_calls[0]["server_urls"] == init._LOCAL_BROKER_URL


def test_live_finish_broker_url_defaults_to_localhost_when_env_unset(tmp_path: Path) -> None:
    """If the broker phase leaves ``CALF_HOST_URL`` unset (the url-branch-with-no-
    input degrade), the finish falls back to the SAME default the runners use
    (``localhost``), re-read from the effective config — never the stale
    pre-wizard value."""
    finish = _FinishStub(reply=True)
    rc = _run(
        _prompter(name="scribe", broker="url", broker_url=""),
        tmp_path,
        home=tmp_path,
        finish=finish,
        server_urls="stale-prewizard:9092",
    )
    assert rc == 0
    assert finish.start_calls[0]["server_urls"] == "localhost"
    assert finish.reply_calls[0]["server_urls"] == "localhost"


def test_live_finish_first_reply_success_celebrates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    finish = _FinishStub(reply=True)
    assert _run(_prompter(), tmp_path, home=tmp_path, finish=finish) == 0
    out = capsys.readouterr().out
    assert "🎉" in out


def test_live_finish_reply_timeout_downgrades_to_try_yourself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a clean reply timeout the wizard downgrades honestly (§12.6) — it never
    promises more than it detected, and points at ``doctor``."""
    finish = _FinishStub(reply=False)
    rc = _run(_prompter(name="scribe"), tmp_path, home=tmp_path, finish=finish)
    out = capsys.readouterr().out
    assert rc == 0
    assert "@scribe hello" in out
    assert "disco doctor" in out


def test_live_finish_watcher_failure_degrades_to_live_org_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A NON-timeout watcher failure (broker drop / transient connect blip) must
    degrade into the bounded live-org fallback, not crash the wizard AFTER the
    org is already live (minor #8).

    The substrate and agent already started successfully — the org IS live — so a
    first-reply detection error is advisory: the watcher's ``RuntimeError`` is
    caught and mapped to the same honest "org is live; try @agent hello; if
    nothing run doctor" downgrade a clean timeout takes, and the run returns 0."""

    class _RaisingFinish(_FinishStub):
        async def first_reply(self, server_urls, *, ready=None, **kwargs) -> bool:
            # Signal readiness so the wizard proceeds to the prompt, then fail the
            # detection itself — exactly a broker drop mid-watch after a live org.
            if ready is not None:
                ready.set()
            self.reply_calls.append({"server_urls": server_urls, **kwargs})
            raise RuntimeError("broker connection dropped while watching")

    finish = _RaisingFinish()
    rc = _run(_prompter(name="scribe"), tmp_path, home=tmp_path, finish=finish)
    out = capsys.readouterr().out
    assert rc == 0  # clean return — the org is live, detection is advisory
    assert "@scribe hello" in out
    assert "disco doctor" in out


def test_live_finish_prompts_hello_in_flow(tmp_path: Path) -> None:
    """The ``@agent hello`` prompt happens INSIDE init (fixes the §12.6 step3/4
    contradiction) — the human is asked to send it, then we watch the outbox."""
    finish = _FinishStub(reply=True)
    p = _prompter(name="scribe", say_hello=True)
    assert _run(p, tmp_path, home=tmp_path, finish=finish) == 0
    # The confirm was consumed (the in-flow "say hello" gate).
    assert len(finish.reply_calls) == 1


def test_first_reply_watcher_joins_before_user_is_prompted(tmp_path: Path) -> None:
    """The first-reply watcher (a ``latest``-offset consumer) must START — and be
    READY (group joined) — BEFORE the human is asked to send ``@agent hello``.

    Otherwise a fast human can post the message before the consumer group joins
    at ``latest`` offset, so the reply lands before the watcher is listening and
    is missed (a false negative → the wizard wrongly downgrades on a live org).
    The shared ``events`` log records the relative order: ``watcher_started`` must
    precede ``prompted``."""
    events: list[str] = []
    finish = _FinishStub(reply=True, events=events)
    p = _prompter(name="scribe", say_hello=True)
    p._events = events  # share the ordering log with the finish stub
    assert _run(p, tmp_path, home=tmp_path, finish=finish) == 0
    assert events == ["watcher_started", "prompted"]


def test_finish_does_not_hang_when_watcher_completes_without_readiness(tmp_path: Path) -> None:
    """If the watcher coroutine finishes WITHOUT ever signalling readiness (an
    early return / broker-unreachable error), the wizard must still proceed to the
    prompt instead of blocking on a readiness that will never come — it races the
    readiness signal against task completion. We supply a ``first_reply`` that
    returns immediately and never sets ``ready``; the run must finish promptly."""

    class _NoReadyFinish(_FinishStub):
        async def first_reply(self, server_urls, *, ready=None, **kwargs) -> bool:
            # Deliberately ignore ``ready`` (never set it) and return at once.
            self.reply_calls.append({"server_urls": server_urls, **kwargs})
            return self._reply

    finish = _NoReadyFinish(reply=True)
    # A generous-but-bounded wall-clock guard: if the wizard regressed to waiting
    # on the never-set readiness it would block for the full _FIRST_REPLY_TIMEOUT_S.
    import time

    t0 = time.monotonic()
    rc = _run(_prompter(name="scribe"), tmp_path, home=tmp_path, finish=finish)
    elapsed = time.monotonic() - t0
    assert rc == 0
    assert len(finish.reply_calls) == 1
    assert elapsed < init._FIRST_REPLY_TIMEOUT_S / 2  # did not block on readiness


def test_substrate_start_failure_does_not_start_agent_or_watch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the substrate fails its readiness gate, the wizard stops there — it does
    not clock the agent in against a substrate that never came up."""
    finish = _FinishStub(start_rc=1)
    rc = _run(_prompter(), tmp_path, home=tmp_path, finish=finish)
    assert rc != 0
    assert len(finish.start_calls) == 1
    assert len(finish.agent_calls) == 0
    assert len(finish.reply_calls) == 0


def test_substrate_start_failure_points_at_logs_and_doctor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A substrate-start failure points at the diagnostics (§12.6) rather than
    misattributing the cause — a cold-broker failure, an intents gap, and a bad
    config all land here, so name `disco logs` / `disco doctor`, not one guess."""
    finish = _FinishStub(start_rc=1)
    _run(_prompter(), tmp_path, home=tmp_path, finish=finish)
    out = capsys.readouterr().out.lower()
    assert "disco logs" in out
    assert "disco doctor" in out


def test_agent_start_failure_skips_reply_watch(tmp_path: Path) -> None:
    finish = _FinishStub(agent_rc=1)
    rc = _run(_prompter(), tmp_path, home=tmp_path, finish=finish)
    assert rc != 0
    assert len(finish.start_calls) == 1
    assert len(finish.agent_calls) == 1
    assert len(finish.reply_calls) == 0


def test_live_finish_resolves_real_orchestration_when_seams_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the orchestration coroutines are NOT injected, the finish resolves the
    real ``lifecycle.start`` / ``roster.agent_start`` / ``_wait_for_agent_online``
    (the production wiring). We monkeypatch those module attributes to fast
    no-ops so the resolution path runs without launching a supervisor or broker.

    calfkit 0.12 replaced the deleted control-plane first-reply watcher with the
    local ``init._wait_for_agent_online`` mesh presence-poll, so the default
    first-reply seam now resolves to that module function."""
    from calfcord.supervisor import lifecycle, roster

    async def _ok_start(home, **_):
        return 0

    async def _ok_agent(home, **_):
        return 0

    async def _ok_reply(server_urls, **_):
        return True

    monkeypatch.setattr(lifecycle, "start", _ok_start)
    monkeypatch.setattr(roster, "agent_start", _ok_agent)
    monkeypatch.setattr(init, "_wait_for_agent_online", _ok_reply)

    # Inject only the pc-binary probe (so the native gate passes); leave the
    # start/agent/reply seams as None so the lazy production resolution runs.
    rc = init.run(
        _prompter(name="scribe"),
        env_path=tmp_path / ".env",
        agents_dir=tmp_path / "agents",
        home=tmp_path,
        server_urls="localhost:9092",
        now=_now,
        verify_identity_fn=_DiscordStub().verify,
        poll_joined_fn=_DiscordStub().poll,
        list_guilds_fn=_DiscordStub().guilds,
        list_channels_fn=_DiscordStub().channels,
        pc_binary_fn=lambda: "/usr/bin/process-compose",
    )
    assert rc == 0


def test_default_pc_binary_delegates_to_supervisor_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default process-compose probe delegates to the supervisor's resolver."""
    from calfcord.supervisor import lifecycle

    monkeypatch.setattr(lifecycle, "resolve_pc_binary", lambda: "/opt/pc")
    assert init._default_pc_binary() == "/opt/pc"


# --------------------------------------------------------------------------- #
# Dev-mode degrade (no native install): configure, then print manual next steps
# --------------------------------------------------------------------------- #


def test_dev_mode_degrades_to_manual_next_steps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With ``home=None`` (dev run, no install/shim) the wizard cannot orchestrate
    the supervisor, so it configures everything then prints the honest manual
    next-steps instead of starting the substrate (§12.6)."""
    finish = _FinishStub()
    # No say-hello confirm in dev mode (nothing was started to reply).
    p = _prompter()
    rc = _run(p, tmp_path, home=None, finish=finish)
    out = capsys.readouterr().out
    assert rc == 0
    # Nothing was orchestrated.
    assert finish.start_calls == []
    assert finish.agent_calls == []
    # The honest manual path is named.
    assert "disco start" in out
    assert "disco agent start scribe" in out


def test_missing_process_compose_binary_degrades(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A native install missing the process-compose binary also degrades to the
    manual next-steps rather than crashing (§12.6 honest-failure-path)."""
    finish = _FinishStub(pc_binary=RuntimeError("process-compose binary not found"))
    p = _prompter()
    rc = _run(p, tmp_path, home=tmp_path, finish=finish)
    out = capsys.readouterr().out
    assert rc == 0
    assert finish.start_calls == []
    assert "disco start" in out


def test_dev_mode_states_reboot_non_survival(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The session-scoped (not reboot-surviving) nature is stated honestly (§12.6)."""
    rc = _run(_prompter(), tmp_path, home=tmp_path, finish=_FinishStub())
    out = capsys.readouterr().out
    assert rc == 0
    assert "reboot" in out.lower()


# --------------------------------------------------------------------------- #
# Checkpoint: persisted after each step, resume from each, re-verify (advisory)
# --------------------------------------------------------------------------- #


def test_checkpoint_persisted_after_native_run(tmp_path: Path) -> None:
    """A completed run leaves a checkpoint recording the steps + non-secret IDs."""
    assert _run(_prompter(name="scribe", guild="g1", channel="c1"), tmp_path, home=tmp_path) == 0
    cp = setup_state.load(setup_state.checkpoint_path(tmp_path))
    assert cp is not None
    assert cp.provider_done is True
    assert cp.agent_name == "scribe"
    assert cp.discord_done is True
    assert cp.broker_done is True
    assert cp.guild_id == "g1"
    assert cp.channel_id == "c1"


def test_resume_welcome_back_when_agent_already_done(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A checkpoint with the agent done greets 'Welcome back' on the next run."""
    agents_dir = tmp_path / "agents"
    # Seed an existing, parseable agent + a checkpoint marking it done.
    assert _run(_prompter(name="scribe"), tmp_path, agents_dir=agents_dir, home=tmp_path) == 0
    capsys.readouterr()  # drop the first run's output

    # Second run: agent + provider already done → script only the remaining steps,
    # plus the re-verified agent step (advisory re-walk is harmless/idempotent).
    p2 = _prompter(name="scribe")
    assert _run(p2, tmp_path, agents_dir=agents_dir, home=tmp_path) == 0
    out = capsys.readouterr().out
    assert "Welcome back" in out


def test_resume_threads_checkpoint_agent_name_into_create_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On resume the advisory agent re-walk must default to the checkpointed agent
    name (nit #18b).

    Without threading the name, a 2+-agent install hits the
    ``len(existing) != 1 → STARTER_AGENT_NAME`` branch in ``create_agent`` and the
    re-walk defaults to the wrong (starter) name instead of the agent the operator
    was mid-creating. We capture the ``name_default`` ``init.run`` passes to
    ``create_agent`` and assert it carries the checkpoint's agent name."""
    agents_dir = tmp_path / "agents"
    # First full run binds a real, parseable agent + a checkpoint naming it.
    assert _run(_prompter(name="scribe"), tmp_path, agents_dir=agents_dir, home=tmp_path) == 0

    captured: dict[str, str | None] = {}
    real_create = init.create_agent

    def _spy(prompter, **kwargs):
        captured["name_default"] = kwargs.get("name_default")
        return real_create(prompter, **kwargs)

    monkeypatch.setattr(init, "create_agent", _spy)
    # Resume run: the checkpoint says scribe is done + the .md still parses.
    assert _run(_prompter(name="scribe"), tmp_path, agents_dir=agents_dir, home=tmp_path) == 0
    assert captured["name_default"] == "scribe"


def test_fresh_run_passes_no_name_default_to_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh (non-resume) run must NOT pin a name default — ``create_agent``'s own
    lone-existing / starter default logic owns the fresh case (nit #18b)."""
    captured: dict[str, str | None] = {}
    real_create = init.create_agent

    def _spy(prompter, **kwargs):
        captured["name_default"] = kwargs.get("name_default")
        return real_create(prompter, **kwargs)

    monkeypatch.setattr(init, "create_agent", _spy)
    assert _run(_prompter(name="scribe"), tmp_path, home=tmp_path) == 0
    assert captured["name_default"] is None


def test_resume_greeting_reflects_advisory_rewalk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The resume greeting must not claim the agent step is finished while it then
    unconditionally re-walks the create flow (nit #18a). It keeps the test-asserted
    'Welcome back' substring but softens the wording to reflect the advisory
    re-walk — it must NOT assert the step is 'done; let's finish setup'."""
    agents_dir = tmp_path / "agents"
    assert _run(_prompter(name="scribe"), tmp_path, agents_dir=agents_dir, home=tmp_path) == 0
    capsys.readouterr()  # drop the first run's output

    assert _run(_prompter(name="scribe"), tmp_path, agents_dir=agents_dir, home=tmp_path) == 0
    out = capsys.readouterr().out
    assert "Welcome back" in out
    # The old wording wrongly implied the agent step was settled; soften it.
    assert "are done; let's finish setup" not in out


def test_resume_reverifies_agent_artifact_not_just_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Advisory contract (§12.7): a checkpoint says the agent is done, but the
    real ``.md`` is gone — the wizard RE-WALKS the agent step (re-verify the
    artifact), it does not trust the stale flag and skip into Discord."""
    agents_dir = tmp_path / "agents"
    # Write a checkpoint claiming the agent is done, but never create the .md.
    setup_state.save(
        setup_state.checkpoint_path(tmp_path),
        setup_state.SetupCheckpoint(provider_done=True, agent_name="ghost"),
        now=_now,
    )
    # The wizard must re-run the agent create flow (it will prompt for it again).
    p = _prompter(name="scribe")
    rc = _run(p, tmp_path, agents_dir=agents_dir, home=tmp_path)
    assert rc == 0
    # The artifact now really exists (re-walked, not skipped on the stale flag).
    assert (agents_dir / "scribe.md").is_file()


def test_resume_with_corrupt_agent_md_does_not_greet_welcome_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A checkpoint says the agent is done but the ``.md`` no longer parses — the
    re-verify gate rejects it, so the wizard does NOT greet 'Welcome back' and
    re-walks the create step (advisory contract, §12.7)."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "ghost.md").write_text("not valid frontmatter at all\n", encoding="utf-8")
    setup_state.save(
        setup_state.checkpoint_path(tmp_path),
        setup_state.SetupCheckpoint(provider_done=True, agent_name="ghost"),
        now=_now,
    )
    rc = _run(_prompter(name="scribe"), tmp_path, agents_dir=agents_dir, home=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Welcome back" not in out  # stale flag + unparseable artifact → fresh


def test_resume_defaults_to_kept_guild_channel_binding(tmp_path: Path) -> None:
    """A re-run must not clobber a working guild/channel — it defaults to keep (§12.7)."""
    # First full run binds g1/c1.
    assert _run(_prompter(name="scribe", guild="g1", channel="c1"), tmp_path, home=tmp_path) == 0
    env_first = read_env(tmp_path / ".env")
    assert env_first["DISCORD_GUILD_ID"] == "g1"

    # Second run keeps the binding (the select defaults to the saved guild/channel).
    assert _run(_prompter(name="scribe", guild="g1", channel="c1"), tmp_path, home=tmp_path) == 0
    env_second = read_env(tmp_path / ".env")
    assert env_second["DISCORD_GUILD_ID"] == "g1"
    assert env_second["DISCORD_DEFAULT_CHANNEL_ID"] == "c1"


# --------------------------------------------------------------------------- #
# Keep-existing-on-empty (re-run safety) for secrets
# --------------------------------------------------------------------------- #


def test_empty_token_on_rerun_keeps_existing_secret(tmp_path: Path) -> None:
    """An empty Discord token answer must not clobber an existing one (re-run safety)."""
    env_path = tmp_path / ".env"
    upsert(env_path, {"DISCORD_BOT_TOKEN": "tok-original"})
    p = _prompter(name="scribe", discord_token="")  # blank → keep
    assert _run(p, tmp_path, env_path=env_path, home=tmp_path) == 0
    assert read_env(env_path)["DISCORD_BOT_TOKEN"] == "tok-original"


def test_live_finish_passes_configured_mcp_servers(tmp_path: Path, monkeypatch) -> None:
    """The live finish enumerates mcp.json (the same no-secrets seam
    ``disco start`` uses) so the workspace declares the mcp-<server>
    slots from the first boot."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "mcp.json").write_text('{"mcpServers": {"github": {"command": "x"}}}')
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    finish = _FinishStub(reply=True)
    rc = _run(_prompter(name="scribe"), tmp_path, home=tmp_path, finish=finish)
    assert rc == 0
    assert finish.start_calls[0]["mcp_servers"] == ["github"]


def test_live_finish_tolerates_broken_mcp_config(tmp_path: Path, monkeypatch, capsys) -> None:
    """Onboarding must reach a live org even when mcp.json is broken: warn and
    start with no MCP slots (the strict readers — `disco start`, `mcp
    start` — surface the config error for fixing)."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "mcp.json").write_text("{not json")
    monkeypatch.setenv("CALFCORD_HOME", str(tmp_path))
    monkeypatch.delenv("CALFCORD_MCP_CONFIG", raising=False)
    finish = _FinishStub(reply=True)
    rc = _run(_prompter(name="scribe"), tmp_path, home=tmp_path, finish=finish)
    assert rc == 0
    assert finish.start_calls[0]["mcp_servers"] == []
    assert "mcp.json" in capsys.readouterr().out


# --- _wait_for_agent_online body (the mesh presence-poll; previously monkeypatched away) ---


class _WaitFakeClient:
    """Scriptable Client for ``_wait_for_agent_online``: a sequence of get_agents()
    results (each a dict to return or an Exception to raise), or a fixed
    ``get_agents_error`` that always raises, plus aclose() tracking so cleanup is
    asserted on every path."""

    def __init__(self, *, get_agents_seq=None, get_agents_error=None) -> None:
        self._seq = list(get_agents_seq or [])
        self._get_agents_error = get_agents_error
        self.mesh = self  # client.mesh.get_agents() resolves back here
        self.aclosed = False

    async def get_agents(self):
        if self._get_agents_error is not None:
            raise self._get_agents_error
        item = self._seq.pop(0) if self._seq else {}
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.aclosed = True


def _patch_wait_client(monkeypatch, fake: _WaitFakeClient) -> None:
    # _wait_for_agent_online imports Client from calfkit.client locally and patches
    # the poll interval to 0 so the retry loop doesn't actually sleep.
    import calfkit.client

    monkeypatch.setattr(calfkit.client.Client, "connect", lambda *a, **k: fake)
    monkeypatch.setattr(init, "_ONLINE_POLL_INTERVAL_S", 0)


async def test_wait_returns_true_when_present_and_sets_ready(monkeypatch):
    fake = _WaitFakeClient(get_agents_seq=[{"assistant": SimpleNamespace(name="assistant")}])
    _patch_wait_client(monkeypatch, fake)
    ready = asyncio.Event()
    ok = await init._wait_for_agent_online("localhost", agent_id="assistant", timeout_s=5.0, ready=ready)
    assert ok is True
    assert ready.is_set()  # the watcher-joined signal fired
    assert fake.aclosed is True


async def test_wait_retries_until_agent_appears(monkeypatch):
    # topic-absent, then empty, then present — the poll loop must keep going.
    fake = _WaitFakeClient(
        get_agents_seq=[
            MeshUnavailableError("no topic", reason="open_failed"),
            {},
            {"assistant": SimpleNamespace(name="assistant")},
        ]
    )
    _patch_wait_client(monkeypatch, fake)
    ok = await init._wait_for_agent_online("localhost", agent_id="assistant", timeout_s=5.0)
    assert ok is True
    assert fake.aclosed is True


async def test_wait_times_out_returns_false(monkeypatch):
    # agent never appears; a zero window returns False after the first poll.
    fake = _WaitFakeClient(get_agents_seq=[])
    _patch_wait_client(monkeypatch, fake)
    ok = await init._wait_for_agent_online("localhost", agent_id="ghost", timeout_s=0.0)
    assert ok is False
    assert fake.aclosed is True


async def test_wait_broker_down_times_out_to_false(monkeypatch):
    # No broker pre-flight: a down broker surfaces as get_agents() raising
    # MeshUnavailableError, which the poll loop swallows and retries until the window
    # elapses -> False. The client is still closed (the finally).
    fake = _WaitFakeClient(get_agents_error=MeshUnavailableError("broker down", reason="open_failed"))
    _patch_wait_client(monkeypatch, fake)
    ok = await init._wait_for_agent_online("localhost", agent_id="assistant", timeout_s=0.0)
    assert ok is False
    assert fake.aclosed is True
