"""Tests for ``calfcord.cli.discord_discovery`` — the init wizard's Discord auto-discovery library.

The module is a *library* (no CLI subcommand): it validates a bot token, builds the invite URL,
polls until the bot joins a guild, and lists guilds / postable channels. Every network call goes
through an injected ``client_factory`` so these tests use ``httpx.MockTransport`` — no real Discord
is ever contacted (mirrors ``tests/cli/test_doctor.py``). Polling injects ``clock``/``sleep`` so the
backoff/timeout logic runs instantly and deterministically (mirrors ``supervisor/lifecycle.py``).

The bot token is a secret: like ``doctor``, it must ride only in the ``Authorization`` header and
NEVER appear in any returned value, message, or exception text. A sentinel token guards that.
"""
from __future__ import annotations

import httpx
import pytest

from calfcord.cli import discord_discovery as dd

# A recognizable bot token that must NEVER appear in any output or exception.
TOKEN = "SENTINEL_TOKEN_do_not_leak_42"

# Discord permission bits used to build fixtures (kept literal here so a wrong constant in the
# module under test can't make a wrong test pass).
SEND_MESSAGES = 1 << 11  # 2048
MANAGE_WEBHOOKS = 1 << 29  # 536870912
VIEW_CHANNEL = 1 << 10  # 1024
ADMINISTRATOR = 1 << 3  # 8
POSTABLE = SEND_MESSAGES | MANAGE_WEBHOOKS

GUILD_ID = "80351110224678912"
BOT_USER_ID = "1001"

# ----------------------------------------------------------------- transport plumbing


def _factory(handler):
    """A client_factory yielding an httpx.Client backed by a MockTransport handler."""
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def _route(responses: dict[str, httpx.Response | list[httpx.Response]]):
    """Build a handler dispatching on URL path, popping a per-path queue when a list is given.

    A bare ``httpx.Response`` is returned for every hit on that path; a ``list`` is consumed one
    response per call so a path can return different things across poll iterations.
    """
    queues = {path: list(r) if isinstance(r, list) else r for path, r in responses.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        for path, val in queues.items():
            if request.url.path == path:
                if isinstance(val, list):
                    return val.pop(0)
                return val
        raise AssertionError(f"unexpected request to {request.url.path}")

    return handler


def _me(user_id=BOT_USER_ID, username="TestBot"):
    return httpx.Response(200, json={"id": user_id, "username": username})


def _guilds(*guilds):
    return httpx.Response(200, json=list(guilds))


def _guild(gid=GUILD_ID, name="My Server", owner=False, permissions="0"):
    return {"id": gid, "name": name, "owner": owner, "permissions": permissions}


# ============================================================= (a) verify_bot_identity


def test_verify_bot_identity_returns_id_and_username():
    handler = _route({"/api/v10/users/@me": _me(user_id="42", username="Calf")})
    identity = dd.verify_bot_identity(TOKEN, client_factory=_factory(handler))
    assert identity.id == "42"
    assert identity.username == "Calf"


def test_verify_bot_identity_sends_token_only_in_header():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return _me()

    dd.verify_bot_identity(TOKEN, client_factory=_factory(lambda r: handler(r)))
    assert seen[0].headers["Authorization"] == f"Bot {TOKEN}"
    # The token must not appear anywhere in the URL/query.
    assert TOKEN not in str(seen[0].url)


@pytest.mark.parametrize("code", [401, 403])
def test_verify_bot_identity_rejected_raises_auth_error(code):
    handler = _route({"/api/v10/users/@me": httpx.Response(code, json={"message": "no"})})
    with pytest.raises(dd.DiscordAuthError) as exc:
        dd.verify_bot_identity(TOKEN, client_factory=_factory(handler))
    assert TOKEN not in str(exc.value)


def test_verify_bot_identity_rate_limited_raises_rate_limited():
    handler = _route({"/api/v10/users/@me": httpx.Response(429, json={"retry_after": 1.0})})
    with pytest.raises(dd.DiscordRateLimitedError):
        dd.verify_bot_identity(TOKEN, client_factory=_factory(handler))


def test_verify_bot_identity_transport_error_raises_unavailable():
    def boom(request):
        raise httpx.ConnectError("network down")

    with pytest.raises(dd.DiscordUnavailableError):
        dd.verify_bot_identity(TOKEN, client_factory=_factory(boom))


def test_verify_bot_identity_5xx_raises_unavailable():
    handler = _route({"/api/v10/users/@me": httpx.Response(500, text="boom")})
    with pytest.raises(dd.DiscordUnavailableError):
        dd.verify_bot_identity(TOKEN, client_factory=_factory(handler))


# ============================================================= (b) invite_url


def test_invite_url_contains_app_id_scope_and_permission_bitmask():
    url = dd.invite_url("123456789")
    assert "client_id=123456789" in url
    assert "scope=bot" in url and "applications.commands" in url
    # The bitmask must include BOTH Send Messages and Manage Webhooks (what the bridge needs).
    assert f"permissions={dd.INVITE_PERMISSIONS}" in url
    assert dd.INVITE_PERMISSIONS & POSTABLE == POSTABLE


def test_invite_url_accepts_int_app_id():
    assert "client_id=123456789" in dd.invite_url(123456789)


def test_intents_reminder_names_both_privileged_intents():
    reminder = dd.INTENTS_REMINDER.lower()
    assert "message content" in reminder
    assert "server members" in reminder


# ============================================================= (c) poll_until_joined


class _FakeClock:
    """A monotonic clock + sleep pair that advances virtual time on each sleep (no real waiting)."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_poll_until_joined_returns_guilds_once_present():
    clock = _FakeClock()
    handler = _route(
        {
            "/api/v10/users/@me/guilds": [
                _guilds(),  # first poll: not joined yet
                _guilds(_guild(name="Joined")),  # second poll: bot has joined
            ]
        }
    )
    guilds = dd.poll_until_joined(
        TOKEN,
        client_factory=_factory(handler),
        clock=clock.time,
        sleep=clock.sleep,
        timeout_s=60.0,
        interval_s=2.0,
    )
    assert [g.name for g in guilds] == ["Joined"]
    assert clock.slept == [2.0]  # slept once between the two polls


def test_poll_until_joined_times_out_when_never_joined():
    clock = _FakeClock()
    handler = _route({"/api/v10/users/@me/guilds": _guilds()})  # always empty
    with pytest.raises(dd.DiscordJoinTimeoutError):
        dd.poll_until_joined(
            TOKEN,
            client_factory=_factory(handler),
            clock=clock.time,
            sleep=clock.sleep,
            timeout_s=10.0,
            interval_s=2.0,
        )


def test_poll_until_joined_honors_retry_after_on_429():
    clock = _FakeClock()
    handler = _route(
        {
            "/api/v10/users/@me/guilds": [
                httpx.Response(429, json={"retry_after": 7.5}),  # rate limited
                _guilds(_guild()),  # then joined
            ]
        }
    )
    guilds = dd.poll_until_joined(
        TOKEN,
        client_factory=_factory(handler),
        clock=clock.time,
        sleep=clock.sleep,
        timeout_s=60.0,
        interval_s=2.0,
    )
    assert len(guilds) == 1
    # A 429 must back off by Retry-After (7.5s), not the normal 2s interval.
    assert clock.slept == [7.5]


def test_poll_until_joined_tolerates_transient_transport_error():
    clock = _FakeClock()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("blip")
        return _guilds(_guild())

    guilds = dd.poll_until_joined(
        TOKEN,
        client_factory=_factory(handler),
        clock=clock.time,
        sleep=clock.sleep,
        timeout_s=60.0,
        interval_s=2.0,
    )
    assert len(guilds) == 1  # a transient blip is retried, not fatal


def test_poll_until_joined_auth_error_is_fatal_not_retried():
    clock = _FakeClock()
    handler = _route({"/api/v10/users/@me/guilds": httpx.Response(401, json={})})
    with pytest.raises(dd.DiscordAuthError):
        dd.poll_until_joined(
            TOKEN,
            client_factory=_factory(handler),
            clock=clock.time,
            sleep=clock.sleep,
            timeout_s=60.0,
            interval_s=2.0,
        )
    assert clock.slept == []  # a revoked token must fail fast, not spin


# ============================================================= (d) list_guilds


def test_list_guilds_returns_id_name_owner_and_base_permissions():
    handler = _route(
        {
            "/api/v10/users/@me/guilds": _guilds(
                _guild(gid="1", name="Alpha", owner=True, permissions=str(ADMINISTRATOR)),
                _guild(gid="2", name="Beta", owner=False, permissions=str(POSTABLE)),
            )
        }
    )
    guilds = dd.list_guilds(TOKEN, client_factory=_factory(handler))
    assert [(g.id, g.name, g.owner) for g in guilds] == [("1", "Alpha", True), ("2", "Beta", False)]
    assert guilds[0].base_permissions == ADMINISTRATOR
    assert guilds[1].base_permissions == POSTABLE


def test_list_guilds_empty_returns_empty_list():
    handler = _route({"/api/v10/users/@me/guilds": _guilds()})
    assert dd.list_guilds(TOKEN, client_factory=_factory(handler)) == []


# ============================================================= (e) list_postable_channels


def _member(*role_ids):
    return httpx.Response(200, json={"roles": list(role_ids)})


def _channels(*channels):
    return httpx.Response(200, json=list(channels))


def _text_channel(cid, name, overwrites=()):
    return {"id": cid, "name": name, "type": 0, "permission_overwrites": list(overwrites)}


def _overwrite(oid, otype, allow=0, deny=0):
    # Discord serializes allow/deny as strings; the module must coerce them.
    return {"id": oid, "type": otype, "allow": str(allow), "deny": str(deny)}


def _routes_for_channels(*, guild, member, channels):
    return _route(
        {
            "/api/v10/users/@me": _me(),
            "/api/v10/users/@me/guilds": _guilds(guild),
            # Bot tokens must use /members/{bot_user_id}; /members/@me is 400 for bots.
            f"/api/v10/guilds/{GUILD_ID}/members/{BOT_USER_ID}": member,
            f"/api/v10/guilds/{GUILD_ID}/channels": channels,
        }
    )


def test_postable_channels_base_permission_allows_post():
    # Guild base perms already grant Send Messages + Manage Webhooks, no overwrites -> postable.
    handler = _routes_for_channels(
        guild=_guild(permissions=str(POSTABLE | VIEW_CHANNEL)),
        member=_member(),
        channels=_channels(_text_channel("c1", "general")),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert [c.id for c in listing.postable] == ["c1"]
    assert listing.unpostable == []


def test_postable_channels_filters_visible_but_not_postable():
    # Base perms grant only View Channel; the bot can SEE it but cannot post -> unpostable, surfaced.
    handler = _routes_for_channels(
        guild=_guild(permissions=str(VIEW_CHANNEL)),
        member=_member(),
        channels=_channels(_text_channel("c1", "general")),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert listing.postable == []
    assert [c.id for c in listing.unpostable] == ["c1"]


def test_postable_channels_channel_overwrite_denies_post():
    # Base perms allow posting (incl. View), but a channel @everyone overwrite denies Send Messages
    # -> unpostable. View is granted so the denied Send bit is the operative reason, not missing View.
    handler = _routes_for_channels(
        guild=_guild(permissions=str(POSTABLE | VIEW_CHANNEL)),
        member=_member(),
        channels=_channels(
            _text_channel(
                "c1",
                "locked",
                overwrites=[_overwrite(GUILD_ID, 0, deny=SEND_MESSAGES)],
            )
        ),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert listing.postable == []
    assert [c.id for c in listing.unpostable] == ["c1"]


def test_postable_channels_channel_overwrite_denies_view_only():
    # A channel that grants Send + Manage Webhooks but an @everyone overwrite denies ONLY View
    # Channel: without View the bot literally cannot see (and so cannot reply in) the channel, so it
    # must be unpostable. Guards against treating Send|Manage as sufficient while ignoring View — a
    # green light that lies (the wizard would offer a channel the bot can never reply in).
    handler = _routes_for_channels(
        guild=_guild(permissions=str(POSTABLE | VIEW_CHANNEL)),
        member=_member(),
        channels=_channels(
            _text_channel(
                "c1",
                "hidden",
                overwrites=[_overwrite(GUILD_ID, 0, deny=VIEW_CHANNEL)],
            )
        ),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert listing.postable == []
    assert [c.id for c in listing.unpostable] == ["c1"]


def test_postable_channels_role_overwrite_allow_grants_post():
    # Base perms lack posting, but a role overwrite (the bot has that role) grants both bits.
    handler = _routes_for_channels(
        guild=_guild(permissions=str(VIEW_CHANNEL)),
        member=_member("role-A"),
        channels=_channels(
            _text_channel(
                "c1",
                "elevated",
                overwrites=[_overwrite("role-A", 0, allow=POSTABLE)],
            )
        ),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert [c.id for c in listing.postable] == ["c1"]


def test_postable_channels_member_overwrite_wins_over_role_deny():
    # Member-specific overwrite is applied LAST and must win over a role-level deny.
    handler = _routes_for_channels(
        guild=_guild(permissions=str(VIEW_CHANNEL)),
        member=_member("role-A"),
        channels=_channels(
            _text_channel(
                "c1",
                "special",
                overwrites=[
                    _overwrite("role-A", 0, deny=POSTABLE),  # role denies
                    _overwrite(BOT_USER_ID, 1, allow=POSTABLE),  # member re-grants
                ],
            )
        ),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert [c.id for c in listing.postable] == ["c1"]


def test_postable_channels_owner_short_circuits_to_all():
    # A guild owner has ALL permissions regardless of overwrites.
    handler = _routes_for_channels(
        guild=_guild(owner=True, permissions="0"),
        member=_member(),
        channels=_channels(
            _text_channel("c1", "locked", overwrites=[_overwrite(GUILD_ID, 0, deny=POSTABLE)])
        ),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert [c.id for c in listing.postable] == ["c1"]


def test_postable_channels_administrator_short_circuits_to_all():
    # ADMINISTRATOR base perm bypasses every channel overwrite.
    handler = _routes_for_channels(
        guild=_guild(permissions=str(ADMINISTRATOR)),
        member=_member(),
        channels=_channels(
            _text_channel("c1", "locked", overwrites=[_overwrite(GUILD_ID, 0, deny=POSTABLE)])
        ),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert [c.id for c in listing.postable] == ["c1"]


def test_postable_channels_excludes_non_text_channels():
    # Voice (2) / category (4) channels are not message targets; only text (0) is considered.
    handler = _routes_for_channels(
        guild=_guild(permissions=str(POSTABLE | VIEW_CHANNEL)),
        member=_member(),
        channels=httpx.Response(
            200,
            json=[
                _text_channel("c1", "general"),
                {"id": "v1", "name": "Voice", "type": 2, "permission_overwrites": []},
                {"id": "cat", "name": "Category", "type": 4, "permission_overwrites": []},
            ],
        ),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert [c.id for c in listing.postable] == ["c1"]
    assert [c.id for c in listing.unpostable] == []  # non-text are excluded, not "unpostable"


def test_postable_channels_zero_postable_is_representable():
    handler = _routes_for_channels(
        guild=_guild(permissions=str(VIEW_CHANNEL)),
        member=_member(),
        channels=_channels(_text_channel("c1", "general")),
    )
    listing = dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))
    assert not listing.postable
    assert listing.unpostable  # the caller can say "I can see channels but can't post in any"


def test_postable_channels_unknown_guild_raises_unavailable():
    # The bot isn't in / can't see the guild in /users/@me/guilds.
    handler = _routes_for_channels(
        guild=_guild(gid="other"),
        member=_member(),
        channels=_channels(_text_channel("c1", "general")),
    )
    with pytest.raises(dd.DiscordUnavailableError):
        dd.list_postable_channels(TOKEN, GUILD_ID, client_factory=_factory(handler))


# ============================================================= token-leak guard (cross-cutting)


def test_token_never_leaks_through_any_error_path():
    paths = [
        lambda: dd.verify_bot_identity(
            TOKEN, client_factory=_factory(_route({"/api/v10/users/@me": httpx.Response(401, json={})}))
        ),
        lambda: dd.verify_bot_identity(
            TOKEN, client_factory=_factory(_route({"/api/v10/users/@me": httpx.Response(500, text="x")}))
        ),
        lambda: dd.list_postable_channels(
            TOKEN,
            GUILD_ID,
            client_factory=_factory(
                _routes_for_channels(
                    guild=_guild(gid="other"), member=_member(), channels=_channels()
                )
            ),
        ),
    ]
    for call in paths:
        with pytest.raises(dd.DiscordDiscoveryError) as exc:
            call()
        assert TOKEN not in str(exc.value)
        assert TOKEN not in repr(exc.value)
