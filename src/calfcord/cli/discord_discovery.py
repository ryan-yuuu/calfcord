"""Discord auto-discovery for the ``disco init`` wizard (design §4.5).

This module is a *library* — it has no CLI subcommand of its own. The init wizard composes it to
replace the old "paste a numeric ID" prompts with discovery: validate the bot token the instant it
is pasted, hand the user a ready-made invite link, wait until the bot actually appears in a server,
then offer pick-lists of the user's guilds and *postable* channels.

Why "postable", not "visible": a bot can *see* a channel (View Channel) yet be unable to say
anything in it (no Send Messages, or no Manage Webhooks — which the persona webhooks require). A
pick-list filtered by visibility would happily seat the default agent in a channel where it can
never reply, producing the worst onboarding outcome: a green light that lies. So the channel filter
computes the bot's *effective* permission for each channel using Discord's documented overwrite
algorithm and keeps only channels where it can both send and manage webhooks. Channels the bot can
see but not post in are surfaced separately so the wizard can explain the gap instead of hiding it.

Secrets: the bot token is handled exactly as ``doctor`` handles it — it rides only in the
``Authorization`` header and is NEVER placed in a return value, a message, or an exception. The
underlying httpx exception text (which could echo a request) is never propagated; failures are
re-raised as this module's own typed errors with fixed, token-free messages so the wizard can branch
(rejected vs. rate-limited vs. unreachable vs. join-timeout) without risk of leaking the token.

The httpx client is injected via ``client_factory`` (as in ``doctor._discord_username``) so tests
drive it with ``httpx.MockTransport`` and no real Discord is contacted; ``poll_until_joined`` injects
``clock``/``sleep`` (as in ``supervisor/lifecycle.py``) so its backoff/timeout runs instantly.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

# --- Discord API ------------------------------------------------------------------------------

_API_BASE = "https://discord.com/api/v10"
_HTTP_TIMEOUT = 5.0

# Permission bits (https://discord.com/developers/docs/topics/permissions). Only the handful we
# reason about are named; the rest of the 64-bit mask is irrelevant to "can this bot post here".
_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11
_ADMINISTRATOR = 1 << 3
_MANAGE_WEBHOOKS = 1 << 29

# What the persona-reply path actually requires in a channel: View Channel (without it the bot can't
# see the channel at all — and Send/Manage are dead bits), Send Messages (the bot user speaks), AND
# Manage Webhooks (the bridge posts agent replies as persona webhooks). All three, not any subset:
# a channel granting Send|Manage while denying View is one the bot can never reply in, so classifying
# it postable would be a green light that lies.
_POST_REQUIRED = _VIEW_CHANNEL | _SEND_MESSAGES | _MANAGE_WEBHOOKS

# Permission overwrite target types in the channel object (`permission_overwrites[].type`).
_OVERWRITE_ROLE = 0
_OVERWRITE_MEMBER = 1

# Channel types we treat as message targets. Only standard guild text (0) is offered; voice,
# categories, threads, forums, etc. are not where a default agent should be seated.
_TEXT_CHANNEL = 0

# The invite bitmask granted by the canonical invite link (kept in lock-step with
# docs/discord-setup.md's `permissions=...`). It is a superset of `_POST_REQUIRED`; a guard test
# asserts the relationship so the two can never silently drift.
INVITE_PERMISSIONS = 292594732032

# Shown verbatim at the invite step. The two privileged intents are the single most-missed setup
# step (see docs/discord-setup.md); naming them inline turns a silent "bot online but never replies"
# into a checklist the user can act on before the bridge ever fails with PrivilegedIntentsRequired.
INTENTS_REMINDER = (
    "Before inviting, enable both Privileged Gateway Intents on the Bot tab: "
    "Message Content Intent and Server Members Intent — then click Save Changes."
)


# --- Errors -----------------------------------------------------------------------------------


class DiscordDiscoveryError(Exception):
    """Base for every discovery failure. Messages are fixed and token-free by construction."""


class DiscordAuthError(DiscordDiscoveryError):
    """The token was rejected (401/403). Fatal — re-prompting with the same token is pointless."""


class DiscordRateLimitedError(DiscordDiscoveryError):
    """Discord returned 429 on a one-shot call. The poller tolerates 429 instead of raising this."""


class DiscordUnavailableError(DiscordDiscoveryError):
    """Could not reach Discord, or it returned something unusable (5xx / transport / bad body)."""


class DiscordJoinTimeoutError(DiscordDiscoveryError):
    """The bot did not appear in any guild within the poll budget (the user never authorized)."""


# --- Result types -----------------------------------------------------------------------------


@dataclass(frozen=True)
class BotIdentity:
    """The bot user behind a token — enough to echo "Connected as <username> (id …)"."""

    id: str
    username: str


@dataclass(frozen=True)
class Guild:
    """A guild the bot is in, with the bot's *base* (guild-level) permissions already resolved.

    ``base_permissions`` is Discord's computed guild-level permission integer for the bot in this
    guild (the ``permissions`` field of the partial guild object), the starting point for the
    per-channel overwrite computation. ``owner`` short-circuits to all permissions.
    """

    id: str
    name: str
    owner: bool
    base_permissions: int


@dataclass(frozen=True)
class PostableChannel:
    """A pick-list-ready text channel (id + display name). The id is what the wizard persists."""

    id: str
    name: str


@dataclass(frozen=True)
class ChannelListing:
    """Text channels split by whether the bot can actually post (vs. merely see) them.

    ``unpostable`` exists so the wizard can distinguish "this server has no channels you can post
    in" from "this server has no text channels at all" — and explain the permission gap rather than
    silently dropping channels the user expected to see.
    """

    postable: list[PostableChannel]
    unpostable: list[PostableChannel]


# --- HTTP plumbing ----------------------------------------------------------------------------


def _get(
    path: str,
    token: str,
    *,
    client_factory: Callable[[], httpx.Client] | None,
) -> Any:
    """GET ``{_API_BASE}{path}`` with the token in the header and return the parsed JSON body.

    Every Discord failure is normalized to this module's typed errors with token-free messages;
    the raw httpx error is never propagated (it could echo the request). httpx is imported lazily so
    importing this module stays cheap and import-light (no network deps pulled at import time).
    """
    import httpx

    factory = client_factory or (lambda: httpx.Client(timeout=_HTTP_TIMEOUT))
    try:
        with factory() as client:
            resp = client.get(f"{_API_BASE}{path}", headers={"Authorization": f"Bot {token}"})
    except httpx.HTTPError:  # transport-level: DNS, connect, read timeout, …
        # ``from None`` suppresses the chained httpx error, whose text can echo the request (and
        # thus the Authorization header) in some versions — our own message stays token-free.
        raise DiscordUnavailableError(f"could not reach Discord ({path})") from None

    _raise_for_discord_status(resp, path)
    try:
        return resp.json()
    except ValueError:  # a 2xx with a non-JSON body (edge proxy / interstitial)
        raise DiscordUnavailableError(f"unreadable response from Discord ({path})") from None


def _raise_for_discord_status(resp: httpx.Response, path: str) -> None:
    """Map an HTTP status to a typed, token-free error. 2xx returns; everything else raises."""
    code = resp.status_code
    if 200 <= code < 300:
        return
    if code in (401, 403):  # token not accepted -> fatal; re-prompting won't help
        raise DiscordAuthError(f"token rejected by Discord ({code})")
    if code == 429:  # rate limited -> the poller backs off; one-shot callers see this type
        raise DiscordRateLimitedError(_retry_after(resp))
    raise DiscordUnavailableError(f"unexpected response from Discord ({code}) ({path})")


def _retry_after(resp: httpx.Response) -> float:
    """Seconds to wait after a 429, preferring the JSON ``retry_after`` then the header; defaults sane.

    Returns a float (the poller sleeps it; the one-shot path attaches it as the exception message).
    """
    with contextlib.suppress(Exception):
        body = resp.json()
        if isinstance(body, dict) and "retry_after" in body:
            return float(body["retry_after"])
    with contextlib.suppress(Exception):
        return float(resp.headers.get("Retry-After", ""))
    return _DEFAULT_RETRY_AFTER_SECONDS


# --- (a) token validation + identity echo -----------------------------------------------------


def verify_bot_identity(
    token: str,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> BotIdentity:
    """Validate the token by calling ``GET /users/@me`` and return the bot's id + username.

    Raises :class:`DiscordAuthError` if the token is rejected, :class:`DiscordRateLimitedError` on 429,
    :class:`DiscordUnavailableError` if Discord can't be reached or the body is unusable. The token is
    never echoed in any of these (the wizard prints "Connected as <username>" using the return).
    """
    body = _get("/users/@me", token, client_factory=client_factory)
    if not isinstance(body, dict):
        raise DiscordUnavailableError("unreadable identity from Discord")
    return BotIdentity(id=str(body.get("id", "")), username=str(body.get("username", "?")))


# --- (b) invite URL + intents reminder --------------------------------------------------------


def invite_url(application_id: str | int) -> str:
    """Build the OAuth2 invite URL granting exactly the permissions calfcord needs.

    The scope (``bot applications.commands``) and the :data:`INVITE_PERMISSIONS` bitmask mirror
    docs/discord-setup.md so the wizard and the docs hand out an identical link. Pair it with
    :data:`INTENTS_REMINDER` at the call site — the bitmask grants channel permissions, but the two
    privileged *intents* are a separate Developer-Portal toggle the URL cannot set.
    """
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}"
        "&scope=bot+applications.commands"
        f"&permissions={INVITE_PERMISSIONS}"
    )


# --- (c) poll until the bot joins a guild -----------------------------------------------------

_DEFAULT_POLL_TIMEOUT_SECONDS = 300.0  # ~5 min (the §12.6 soft-timeout budget for the detour)
_DEFAULT_POLL_INTERVAL_SECONDS = 3.0
_DEFAULT_RETRY_AFTER_SECONDS = 2.0


def poll_until_joined(
    token: str,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    timeout_s: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
    interval_s: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> list[Guild]:
    """Poll ``GET /users/@me/guilds`` until the bot is in ≥1 guild, returning them.

    The Discord detour is the one place the wizard must wait on the *user* (open browser, pick a
    server, click Authorize), so this blocks-and-polls and auto-advances the instant the bot
    appears. Resilience matches that intent: a 429 backs off by Retry-After (inheriting doctor's
    rate-limit tolerance) and a transient transport blip is retried — neither aborts the wait. A
    *rejected token*, by contrast, is fatal and fails fast (spinning would never recover). If the
    bot never joins within ``timeout_s``, raises :class:`DiscordJoinTimeoutError` (the user likely never
    authorized) so the wizard can surface the common causes rather than hanging forever.

    ``clock``/``sleep`` are injected (default ``time.monotonic``/``time.sleep``) so tests run the
    backoff/timeout in virtual time.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + timeout_s

    while True:
        try:
            guilds = _parse_guilds(_get("/users/@me/guilds", token, client_factory=client_factory))
        except DiscordRateLimitedError as exc:
            wait = _seconds_from(exc.args, _DEFAULT_RETRY_AFTER_SECONDS)
        except DiscordUnavailableError:
            # A transient blip (connect reset, brief 5xx) during a multi-minute wait is expected;
            # keep polling at the normal cadence rather than aborting the whole detour.
            wait = interval_s
        else:
            if guilds:
                return guilds
            wait = interval_s  # joined zero guilds yet -> keep waiting at the normal cadence

        # Stop *before* sleeping past the deadline, so the budget is honored to the second and we
        # never sleep through a timeout we've already crossed.
        if clock() + wait > deadline:
            raise DiscordJoinTimeoutError(
                f"bot did not join a server within {timeout_s:g}s — "
                "open the invite link, pick a server, and click Authorize"
            )
        sleep(wait)


# --- (d) list guilds --------------------------------------------------------------------------


def list_guilds(
    token: str,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> list[Guild]:
    """Return every guild the bot is in (pick-list-ready), with base permissions resolved.

    An empty list is a legitimate (and surfaced) outcome — the bot was invited nowhere yet.
    """
    return _parse_guilds(_get("/users/@me/guilds", token, client_factory=client_factory))


def _parse_guilds(body: Any) -> list[Guild]:
    """Coerce the ``/users/@me/guilds`` payload into :class:`Guild` values, tolerantly.

    Discord serializes the per-guild ``permissions`` as a *string*; it is coerced to int here so
    callers reason about a plain bitmask. A malformed item is skipped rather than crashing the list.
    """
    if not isinstance(body, list):
        raise DiscordUnavailableError("unreadable guild list from Discord")
    out: list[Guild] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        out.append(
            Guild(
                id=str(item.get("id", "")),
                name=str(item.get("name", "?")),
                owner=bool(item.get("owner", False)),
                base_permissions=_as_int(item.get("permissions", 0)),
            )
        )
    return out


# --- (e) list postable channels ---------------------------------------------------------------


def list_postable_channels(
    token: str,
    guild_id: str | int,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> ChannelListing:
    """List a guild's text channels, split by whether the bot can actually *post* in each.

    "Postable" means the bot's *effective* permission for the channel includes both Send Messages
    and Manage Webhooks (or it is the guild owner / has Administrator, which bypass everything).
    Effective permission is computed from the bot's guild-level base permission plus the channel's
    overwrites, following Discord's documented overwrite order. Non-text channels are excluded
    entirely (they are not message targets); text channels the bot can see but not post in land in
    ``unpostable`` so the gap is explainable.

    Raises :class:`DiscordUnavailableError` if the bot is not in / cannot see the given guild (it would
    otherwise compute permissions against an absent base).
    """
    guild_id = str(guild_id)

    # The bot's user id — needed to apply the member-specific channel overwrite (the highest-
    # precedence overwrite), and it's the cheapest call that also re-validates the token.
    bot_user_id = verify_bot_identity(token, client_factory=client_factory).id

    # Base (guild-level) permissions + owner flag come from the partial guild object; this is
    # Discord's already-computed base permission, so we don't re-fetch+fold roles ourselves.
    guild = _find_guild(list_guilds(token, client_factory=client_factory), guild_id)

    # The bot's role ids in this guild — needed to apply role-level channel overwrites.
    # Use the bot's explicit user id: GET /guilds/{id}/members/@me is a USER-OAuth route
    # (needs the guilds.members.read scope) and returns HTTP 400 for a BOT token — only
    # GET /guilds/{id}/members/{user.id} works with a bot token (verified against live Discord).
    member = _get(f"/guilds/{guild_id}/members/{bot_user_id}", token, client_factory=client_factory)
    role_ids = _member_role_ids(member)

    channels = _get(f"/guilds/{guild_id}/channels", token, client_factory=client_factory)
    postable: list[PostableChannel] = []
    unpostable: list[PostableChannel] = []
    for raw in _iter_dicts(channels):
        if raw.get("type") != _TEXT_CHANNEL:
            continue  # only standard text channels are message targets
        effective = _effective_channel_permissions(
            base=guild.base_permissions,
            owner=guild.owner,
            guild_id=guild_id,
            bot_user_id=bot_user_id,
            role_ids=role_ids,
            overwrites=raw.get("permission_overwrites", []),
        )
        channel = PostableChannel(id=str(raw.get("id", "")), name=str(raw.get("name", "?")))
        (postable if _can_post(effective) else unpostable).append(channel)
    return ChannelListing(postable=postable, unpostable=unpostable)


def _can_post(permissions: int) -> bool:
    """True if a permission integer permits posting agent replies (Administrator, or both required bits)."""
    if permissions & _ADMINISTRATOR:
        return True
    return permissions & _POST_REQUIRED == _POST_REQUIRED


def _effective_channel_permissions(
    *,
    base: int,
    owner: bool,
    guild_id: str,
    bot_user_id: str,
    role_ids: frozenset[str],
    overwrites: Any,
) -> int:
    """Apply Discord's channel-overwrite algorithm to a base permission integer.

    Order (per the Discord permissions reference): owner/Administrator → ALL; otherwise apply the
    @everyone overwrite (keyed by guild id), then the union of the bot's role overwrites, then the
    bot's member-specific overwrite — each as "clear deny bits, then set allow bits", with later
    stages winning. Implemented here (rather than pulled from discord.py) to keep the wizard path
    import-light and the rule unit-testable in isolation.
    """
    if owner or base & _ADMINISTRATOR:
        return _ALL_PERMISSIONS

    by_target = _index_overwrites(overwrites)
    permissions = base

    everyone = by_target.get((_OVERWRITE_ROLE, guild_id))
    if everyone:
        permissions = (permissions & ~everyone[1]) | everyone[0]

    role_allow = 0
    role_deny = 0
    for role_id in role_ids:
        ov = by_target.get((_OVERWRITE_ROLE, role_id))
        if ov:
            role_allow |= ov[0]
            role_deny |= ov[1]
    permissions = (permissions & ~role_deny) | role_allow

    member = by_target.get((_OVERWRITE_MEMBER, bot_user_id))
    if member:
        permissions = (permissions & ~member[1]) | member[0]

    return permissions


_ALL_PERMISSIONS = (1 << 64) - 1  # owner/Administrator sentinel; only its named bits are ever read


def _index_overwrites(overwrites: Any) -> dict[tuple[int, str], tuple[int, int]]:
    """Map ``(type, target_id) -> (allow, deny)`` from a channel's ``permission_overwrites``.

    ``allow``/``deny`` arrive as strings on the wire and are coerced to ints; malformed entries are
    skipped so one bad overwrite can't poison the whole channel's permission computation.
    """
    indexed: dict[tuple[int, str], tuple[int, int]] = {}
    for ov in _iter_dicts(overwrites):
        try:
            otype = int(ov.get("type"))
        except (TypeError, ValueError):
            continue
        indexed[(otype, str(ov.get("id", "")))] = (_as_int(ov.get("allow", 0)), _as_int(ov.get("deny", 0)))
    return indexed


# --- small shared helpers ---------------------------------------------------------------------


def _find_guild(guilds: list[Guild], guild_id: str) -> Guild:
    for guild in guilds:
        if guild.id == guild_id:
            return guild
    # Not in /users/@me/guilds: the bot isn't a member, or lost access. Computing channel
    # permissions without a base would silently mislabel everything, so fail loudly instead.
    raise DiscordUnavailableError(f"bot is not a member of guild {guild_id}")


def _member_role_ids(member: Any) -> frozenset[str]:
    if not isinstance(member, dict):
        raise DiscordUnavailableError("unreadable guild membership from Discord")
    return frozenset(str(r) for r in member.get("roles", []) if r is not None)


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    """Return only the dict items of a list (tolerant of a non-list or stray scalars)."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_int(value: Any) -> int:
    """Coerce a wire value (str or int) to int; unparseable -> 0 (treated as no permission bits)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _seconds_from(args: tuple[Any, ...], default: float) -> float:
    """Pull a float retry-after out of an exception's args (the poller's 429 wait), else the default."""
    if args:
        try:
            return float(args[0])
        except (TypeError, ValueError):
            return default
    return default
