"""``disco init`` — one continuous, resumable guided setup that ends LIVE.

This is the onboarding alternative to hand-editing ``.env`` *and* hand-writing an
``agents/<name>.md``. It walks the operator through one agent end to end, then —
on a native install — *opens the workspace, brings the agent online, and waits
until the agent registers on the live mesh*. The flow is the §4.6 / §11
"ends-live" experience: time-to-online over everything.

Composition, not reinvention
----------------------------
``init`` is a **composer**. Each cohesive unit lives in its own module and ``init``
only sequences them, so the wizard stays unit-testable and the pieces stay
reusable:

* **Agent + provider + model** — delegated wholesale to
  :func:`calfcord.cli.agent_create.create_agent` (the ONE shared create flow,
  which ``agent create`` also uses, so the two can't drift). ``init`` opts into
  pruning the pristine starter and persists the chosen provider as the install
  default.
* **Discord** — :func:`_run_discord` composes :mod:`calfcord.cli.discord_discovery`
  (verify-token-on-paste, the invite link + intents reminder, block-and-poll
  until the bot joins, then guild / *postable*-channel pick-lists) in place of
  the old "paste a numeric ID" prompts (§4.5).
* **Live finish** — :func:`_run_finish` composes
  :func:`calfcord.supervisor.lifecycle.start` (substrate, health-gated) →
  :func:`calfcord.supervisor.roster.agent_start` (the agent clocks in) → an
  in-flow ``@<agent> hello`` prompt → online-presence detection on the mesh
  (:func:`_wait_for_agent_online`, §4.6 / §12.6). On a dev run (no install) or a
  missing supervisor binary it DEGRADES to honest manual next-steps rather than
  orchestrating something it cannot.
* **Resumability** — :mod:`calfcord.cli.setup_state` records *which steps are
  done* so a crash / Ctrl-C / the unavoidable browser detour resumes ("Welcome
  back …") instead of restarting. The checkpoint is **advisory** (§12.7): every
  resumed step RE-VERIFIES the real artifact (agent ``.md`` parses? token still
  valid?) before skipping — the world is ground truth, the checkpoint only
  chooses *where* to resume.

Injected seams
--------------
All prompting goes through an injected :class:`Prompter`; every world-touching
dependency — the Discord HTTP calls, the substrate/roster coroutines, the
online-presence watcher, the process-compose binary probe, and the clock — is a
keyword-only injectable defaulting to the real thing. So the whole wizard runs in
a unit test with no TTY, no Discord, no broker, and no supervisor.

Two invariants the design pins:

* **Idempotent and non-destructive to secrets.** Re-running treats an empty
  answer as "keep what's there" for every ``.env`` secret, and defaults a re-run
  to the saved (working) guild/channel binding rather than clobbering it (§12.7).
* **No green light that lies.** The finish only celebrates once the agent is
  *seen online* on the mesh; a clean timeout downgrades to an honest "try it
  yourself / run doctor" hint, and a substrate that never reaches ready stops the
  flow instead of clocking an agent into a workspace that isn't up (§12.6).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from calfcord.cli import _envfile, discord_discovery, setup_state
from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli.agent_create import create_agent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from calfcord.cli.discord_discovery import BotIdentity, ChannelListing, Guild

_DEFAULT_PROVIDER_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"
_BROKER_VAR = "CALF_HOST_URL"
_LOCAL_BROKER_URL = "localhost:9092"

# How long the live finish waits for the agent to come online before downgrading
# to the honest "try it yourself" hint. Bounded so init never hangs on an agent
# that never registers — the §12.6 fallback is the safety net, not a failure.
_FIRST_REPLY_TIMEOUT_S = 60.0

# How often the online-presence watcher re-reads the mesh, and the per-read bound
# on the mesh view's open-time catch-up. Small so a brand-new org — whose
# ``calf.agents`` topic is not created until the first agent registers — fails a
# read fast and retries within the window rather than blocking on a missing topic.
_ONLINE_POLL_INTERVAL_S = 0.5
_ONLINE_CATCHUP_TIMEOUT_S = 5.0


async def _wait_for_agent_online(
    server_urls: str,
    *,
    agent_id: str,
    timeout_s: float,
    ready: asyncio.Event | None = None,
) -> bool:
    """Poll calfkit's mesh until ``agent_id`` is online, or ``timeout_s`` elapses.

    The live-finish confirmation, replacing the deleted control-plane first-reply
    watcher. It proves the agent's PRESENCE — it registered on the ``calf.agents``
    mesh at startup — not an end-to-end message reply; the org is already live once
    the agent is online, so presence is the honest "it worked" signal.

    Opens a short-lived observer :class:`~calfkit.client.Client`, signals ``ready``
    once it is watching, then reads ``client.mesh.get_agents()`` on a small interval
    until the name appears (-> ``True``) or the window elapses (-> ``False``). No
    broker pre-flight — the read raises at call time if the mesh can't be reached; a
    :class:`~calfkit.exceptions.MeshUnavailableError` (most often the ``calf.agents``
    topic not existing until the first agent registers, or the broker being down) is
    treated as "not online yet" and retried until ``timeout_s`` elapses, at which
    point the caller downgrades a ``False`` to the honest fallback. The mesh view is
    a compacted-topic (ktable) reader, so it catches up to the agent's registration
    even if it opens slightly after the agent came online — ``ready`` can fire before
    the first read without a lost-registration race.
    """
    from calfkit import MeshViewConfig
    from calfkit.client import Client
    from calfkit.exceptions import MeshUnavailableError

    client = Client.connect(server_urls, mesh_config=MeshViewConfig(catchup_timeout=_ONLINE_CATCHUP_TIMEOUT_S))
    try:
        if ready is not None:
            ready.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            try:
                agents = await client.mesh.get_agents()
                if agent_id in agents:
                    return True
            except MeshUnavailableError:
                pass  # topic absent / still establishing — the agent is still coming up
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_ONLINE_POLL_INTERVAL_S)
    finally:
        await client.aclose()


# The reboot-non-survival fact, stated honestly (§12.6: the daemon is
# session-scoped, not init-managed). Kept as one constant so the live-finish and
# manual-degrade paths can't drift on the core claim while wording their own
# follow-up.
_REBOOT_NOTE = "The workspace runs for this session only — it does not survive a reboot."


def resolve_paths(home: Path | None) -> tuple[Path, Path]:
    """Resolve ``(env_path, agents_dir)`` for the current run.

    Native installs pass ``home`` (``$CALFCORD_HOME``): config lives at
    ``home/config/.env`` and agents at ``home/agents`` — unless the operator
    pinned a different agents dir via ``CALFKIT_AGENTS_DIR``, which the shim and
    runners already honour, so ``init``'s detection must agree with them.

    Dev / ``uv run calfcord-cli init`` passes ``home=None``: config is the
    project-local ``./.env`` and agents the project-local ``./agents`` (again
    overridable by ``CALFKIT_AGENTS_DIR``), matching the non-shim defaults.
    """
    agents_override = os.environ.get("CALFKIT_AGENTS_DIR")
    if home is not None:
        env_path = home / "config" / ".env"
        agents_dir = Path(agents_override) if agents_override else home / "agents"
    else:
        env_path = Path(".env")
        agents_dir = Path(agents_override) if agents_override else Path("agents")
    return env_path, agents_dir


def _set_label(value: str) -> str:
    """Render a secret's presence without leaking it: '(currently set)' / '(not set)'."""
    return "(currently set)" if value else "(not set)"


def _agent_md_parses(agents_dir: Path, name: str) -> bool:
    """True iff ``agents_dir/<name>.md`` exists *and* parses (the re-verify gate).

    The §12.7 advisory contract: a checkpoint saying "agent done" is only trusted
    when the real artifact is actually there and loadable — a deleted or corrupted
    ``.md`` means the wizard re-walks the create step rather than skipping it on a
    stale flag. ``parse_agent_md`` is imported lazily so a dev run that bails
    before this never pays the agents-definition import cost.
    """
    md = agents_dir / f"{name}.md"
    if not md.is_file():
        return False
    from calfcord.agents.definition import parse_agent_md

    try:
        parse_agent_md(md)
    except (ValueError, OSError):
        return False
    return True


def run(
    prompter: Prompter,
    *,
    env_path: Path,
    agents_dir: Path,
    home: Path | None = None,
    server_urls: str = "localhost",
    # --- injected world-touching seams (default to the real thing) ----------
    verify_identity_fn: Callable[..., BotIdentity] | None = None,
    poll_joined_fn: Callable[..., list[Guild]] | None = None,
    list_guilds_fn: Callable[..., list[Guild]] | None = None,
    list_channels_fn: Callable[..., ChannelListing] | None = None,
    start_fn: Callable[..., Awaitable[int]] | None = None,
    agent_start_fn: Callable[..., Awaitable[int]] | None = None,
    first_reply_fn: Callable[..., Awaitable[bool]] | None = None,
    pc_binary_fn: Callable[[], str] | None = None,
    now: Callable[[], datetime] | None = None,
    open_url_fn: Callable[[str], None] | None = None,
) -> int:
    """Run the guided, resumable, ends-live setup flow and return an exit code.

    Phases, in order: **(1)** agent identity + provider + model + tools + write
    (the shared :func:`create_agent`), **(2)** Discord (:func:`_run_discord`),
    **(3)** broker, **(4)** the live finish (:func:`_run_finish`). A checkpoint is
    saved after each completed phase so a Ctrl-C resumes; each resumed phase
    re-verifies its real artifact before skipping (advisory, §12.7).

    Every ``.env`` write goes through :func:`_envfile.upsert`; an empty secret
    answer keeps the existing value (re-run safe). ``server_urls`` is the broker
    URL ``main.py`` sampled from ``CALF_HOST_URL`` BEFORE the wizard ran; it is a
    pre-wizard hint only — the broker phase (§3) may write a different
    ``CALF_HOST_URL``, so the live finish re-reads the EFFECTIVE value from the
    just-written ``.env`` (same ``value or "localhost"`` default the runners use)
    rather than trusting this. The injected seams are the test surface —
    production defaults wire the real :mod:`discord_discovery`, :mod:`supervisor`,
    and first-reply modules.
    """
    verify_identity_fn = verify_identity_fn or discord_discovery.verify_bot_identity
    poll_joined_fn = poll_joined_fn or discord_discovery.poll_until_joined
    list_guilds_fn = list_guilds_fn or discord_discovery.list_guilds
    list_channels_fn = list_channels_fn or discord_discovery.list_postable_channels

    checkpoint_file = setup_state.checkpoint_path(home)
    checkpoint = setup_state.load(checkpoint_file) or setup_state.SetupCheckpoint()

    print("disco init — configuring", env_path)
    # Advisory resume greeting: only when the checkpoint claims the agent step is
    # done AND the real .md still parses (re-verify, never trust the flag alone).
    resuming = (
        checkpoint.provider_done
        and checkpoint.agent_name is not None
        and _agent_md_parses(agents_dir, checkpoint.agent_name)
    )
    if resuming:
        # Honest wording (nit #18a): the resume RE-WALKS the create flow rather
        # than skipping it, so don't claim the agent step is settled. We pre-fill
        # the create defaults from the saved agent (a blank answer keeps it) — the
        # re-walk confirms/edits in place, it doesn't restart from scratch.
        print(
            f"Welcome back — picking up where you left off (agent {checkpoint.agent_name}). "
            "Press enter to keep each saved answer."
        )
    print()

    # --- Phase 1: agent identity + provider + model + tools + write --------
    # Delegated wholesale to the shared create flow so ``agent create`` and
    # ``init`` can't drift on how an agent is made. A write failure means no
    # usable agent landed, so abort before Discord / broker / the live finish.
    try:
        created = create_agent(
            prompter,
            agents_dir=agents_dir,
            env_path=env_path,
            # On resume, default the name prompt to the agent the operator was
            # mid-creating (nit #18b): in a 2+-agent install ``create_agent``'s own
            # default falls back to the starter (its lone-existing rule needs
            # exactly one agent), so without this the re-walk would re-create under
            # the wrong name. A fresh run passes None and lets that default logic own it.
            name_default=checkpoint.agent_name if resuming else None,
            prune_seed=True,
            offer_prompt=False,
        )
    except (ValueError, OSError) as e:
        print(f"error: could not create agent: {e}")
        return 1
    name = created.name
    _envfile.upsert(env_path, {_DEFAULT_PROVIDER_VAR: created.provider})
    checkpoint = checkpoint.model_copy(update={"provider_done": True, "agent_name": name})
    setup_state.save(checkpoint_file, checkpoint, now=now)
    print()

    # --- Phase 2: Discord (verify → invite → poll → pick guild/channel) ----
    checkpoint = _run_discord(
        prompter,
        env_path=env_path,
        checkpoint=checkpoint,
        verify_identity_fn=verify_identity_fn,
        poll_joined_fn=poll_joined_fn,
        list_guilds_fn=list_guilds_fn,
        list_channels_fn=list_channels_fn,
        open_url_fn=open_url_fn or _try_open_browser,
    )
    setup_state.save(checkpoint_file, checkpoint, now=now)
    print()

    # --- Phase 3: broker ---------------------------------------------------
    _run_broker(prompter, env_path=env_path)
    checkpoint = checkpoint.model_copy(update={"broker_done": True})
    setup_state.save(checkpoint_file, checkpoint, now=now)
    print()

    # Re-read the EFFECTIVE broker URL the broker phase just wrote, rather than
    # the pre-wizard ``server_urls`` (sampled by main.py BEFORE the wizard ran).
    # The operator can configure a different broker inside the wizard, so the
    # live finish's lifecycle.start broker probe AND the first-reply watcher must
    # connect to what is now on disk — using the SAME ``value or "localhost"``
    # default the runners (main.py ``_run_lifecycle``) resolve from the env, so
    # all three agree on the broker the install actually talks to.
    effective_server_urls = _envfile.read_env(env_path).get(_BROKER_VAR) or "localhost"

    # --- Phase 4: live finish (or honest degrade) --------------------------
    return _run_finish(
        prompter,
        name=name,
        home=home,
        agents_dir=agents_dir,
        env_path=env_path,
        server_urls=effective_server_urls,
        start_fn=start_fn,
        agent_start_fn=agent_start_fn,
        first_reply_fn=first_reply_fn,
        pc_binary_fn=pc_binary_fn,
    )


def _try_open_browser(url: str) -> None:
    """Best-effort browser pop for the invite link; never raises.

    The URL is ALWAYS printed before this runs, so a wrong guess in either
    direction only costs the convenience, never the link (§12.6). Skipped when
    stdout isn't a terminal (piped/captured runs — including pytest — must
    never pop a tab), over SSH, and on display-less Linux, where
    ``webbrowser`` may fall back to a terminal browser and hijack the wizard.
    """
    if not sys.stdout.isatty():
        return
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return
    with contextlib.suppress(Exception):
        webbrowser.open(url)


def _run_discord(
    prompter: Prompter,
    *,
    env_path: Path,
    checkpoint: setup_state.SetupCheckpoint,
    verify_identity_fn: Callable[..., BotIdentity],
    poll_joined_fn: Callable[..., list[Guild]],
    list_guilds_fn: Callable[..., list[Guild]],
    list_channels_fn: Callable[..., ChannelListing],
    open_url_fn: Callable[[str], None] = _try_open_browser,
) -> setup_state.SetupCheckpoint:
    """The Discord sub-flow: validate-on-paste → invite → poll → pick (§4.5/§12.6).

    Composes :mod:`discord_discovery` to replace the old numeric-ID prompts. The
    bot token is captured (keep-existing-on-empty), verified the instant it is
    pasted (echoing the bot's identity), then the operator invites the bot, the
    wizard block-and-polls until it joins, and guild / *postable*-channel
    pick-lists persist ``DISCORD_GUILD_ID`` / ``DISCORD_DEFAULT_CHANNEL_ID``.

    Returns the checkpoint advanced with ``discord_done`` and the chosen
    non-secret guild/channel IDs. The whole sub-flow is non-fatal: a join timeout
    or zero postable channels surfaces the §12.6 actionable hint and returns what
    progress it could make rather than aborting the wizard.
    """
    current = _envfile.read_env(env_path)
    print("Discord setup (the wizard discovers your server and channel — no IDs to paste).")

    token = _capture_token(prompter, env_path, current, verify_identity_fn)
    if not token:
        # No token at all (fresh run, skipped): nothing to discover. The bridge
        # will fail-fast later; we surface it but keep going so the rest of the
        # config still lands and a re-run can finish Discord.
        print("  no Discord token set — skipping discovery; re-run init to finish Discord.")
        return checkpoint

    # Invite step: print the ready-made link + the privileged-intents reminder +
    # the resumability banner BEFORE the wait (§12.6 — Ctrl-C is safe here).
    app_id = _capture_app_id(prompter, env_path, current)
    invite = discord_discovery.invite_url(app_id)
    print()
    print("Invite the bot to your server:")
    print(f"    {invite}")
    print(f"  {discord_discovery.INTENTS_REMINDER}")
    print("  (Ctrl-C is safe & resumable — re-run `disco init` to pick up where you left off.)")
    # Pop the link in a browser AFTER printing it — best-effort only, and a
    # broken opener must never derail the wizard.
    with contextlib.suppress(Exception):
        open_url_fn(invite)
    print()
    print("Waiting for the bot to join a server…")

    try:
        guilds = poll_joined_fn(token)
    except discord_discovery.DiscordJoinTimeoutError:
        # The user never authorized within the budget. Surface the common causes
        # + the "I don't have a server" branch (§12.6) and degrade — the binding
        # is unset, but the rest of init still completes and a re-run finishes it.
        print("  the bot did not join a server in time. Common causes:")
        print("    - did you click Authorize on the invite link?")
        print("    - is the Message Content intent enabled (required; Server Members is recommended)?")
        print("    - do you have Manage Server on the server you tried to add it to?")
        print("  No server yet? Create one in Discord (the + button), then re-run `disco init`.")
        return checkpoint
    except discord_discovery.DiscordDiscoveryError as e:
        # Rate-limited / unreachable on the one-shot poll: surface and degrade.
        print(f"  could not confirm the bot joined ({e}); re-run init to finish Discord.")
        return checkpoint

    guild_id = _pick_guild(prompter, guilds, default=checkpoint.guild_id)
    if guild_id is None:
        return checkpoint
    _envfile.upsert(env_path, {"DISCORD_GUILD_ID": guild_id})

    channel_id = _pick_channel(prompter, list_channels_fn, token, guild_id, default=checkpoint.channel_id)
    if channel_id is None:
        # Guild bound but no postable channel chosen (zero postable / surfaced
        # gap). Record the guild progress; the channel can be picked on a re-run.
        return checkpoint.model_copy(update={"guild_id": guild_id})
    _envfile.upsert(env_path, {"DISCORD_DEFAULT_CHANNEL_ID": channel_id})

    return checkpoint.model_copy(update={"discord_done": True, "guild_id": guild_id, "channel_id": channel_id})


def _capture_token(
    prompter: Prompter,
    env_path: Path,
    current: dict[str, str],
    verify_identity_fn: Callable[..., BotIdentity],
) -> str:
    """Prompt for the bot token (keep-existing-on-empty) and verify it on paste.

    Returns the token in effect (the freshly pasted one, or the kept existing
    one). A rejected token is fatal for *that* value, so we re-prompt for a fresh
    one (re-prompting the same token would be pointless, §12.6); a transient
    Discord error is surfaced but the token is still accepted so the rest of init
    can proceed (the bridge re-validates at boot anyway).
    """
    existing = current.get("DISCORD_BOT_TOKEN", "")
    while True:
        pasted = prompter.secret(f"DISCORD_BOT_TOKEN {_set_label(existing)} — paste to set, enter to keep:")
        token = pasted or existing
        if not token:
            return ""
        try:
            identity = verify_identity_fn(token)
        except discord_discovery.DiscordAuthError:
            # The token Discord rejected is unusable; clear the kept value so an
            # empty answer can't "keep" the bad one, and ask for a fresh paste.
            print("  token rejected by Discord — paste a fresh bot token.")
            existing = ""
            continue
        except discord_discovery.DiscordDiscoveryError as e:
            # Couldn't reach Discord / rate-limited: don't block setup on a blip.
            print(f"  could not verify token right now ({e}); continuing — the bridge will re-check.")
            if pasted:
                _envfile.upsert(env_path, {"DISCORD_BOT_TOKEN": pasted})
            return token
        print(f"  Connected as {identity.username} (id {identity.id}).")
        if pasted:
            _envfile.upsert(env_path, {"DISCORD_BOT_TOKEN": pasted})
        return token


def _capture_app_id(prompter: Prompter, env_path: Path, current: dict[str, str]) -> str:
    """Prompt for the application id (needed for the invite URL); keep-on-empty.

    The id is not a secret and the invite link cannot be built without it, so we
    show + default to the current value and flag a non-numeric typo without
    blocking (matching the bridge's later validation).
    """
    app_id = prompter.text(
        "DISCORD_APPLICATION_ID (numeric — from the Developer Portal):",
        default=current.get("DISCORD_APPLICATION_ID", ""),
    )
    if app_id:
        if not app_id.isdigit():
            print(f"  warning: DISCORD_APPLICATION_ID should be numeric, got {app_id!r}")
        _envfile.upsert(env_path, {"DISCORD_APPLICATION_ID": app_id})
        return app_id
    return current.get("DISCORD_APPLICATION_ID", "")


def _pick_guild(prompter: Prompter, guilds: list[Guild], *, default: str | None) -> str | None:
    """Present a guild pick-list; return the chosen id, or ``None`` if none exist.

    Zero guilds is a legitimate surfaced outcome (the bot joined nowhere the API
    reports yet); we explain it rather than offering an empty menu. ``default``
    pre-selects a previously-saved binding so a re-run keeps the working guild
    (don't clobber it, §12.7).
    """
    if not guilds:
        print("  the bot isn't in any server the API reports yet — re-run init once it has joined one.")
        return None
    choices = [Choice(g.id, f"{g.name}{' (owner)' if g.owner else ''}") for g in guilds]
    return prompter.select(
        "Which server should the agent live in?",
        choices,
        default=default if any(g.id == default for g in guilds) else guilds[0].id,
    )


def _pick_channel(
    prompter: Prompter,
    list_channels_fn: Callable[..., ChannelListing],
    token: str,
    guild_id: str,
    *,
    default: str | None,
) -> str | None:
    """Present a *postable*-channel pick-list; return the chosen id or ``None``.

    Postability (Send Messages + Manage Webhooks), not mere visibility, is the
    filter — a green light that lies (a channel the agent can never reply in) is
    the worst onboarding outcome (§12.6). Channels the bot can see but not post in
    are surfaced separately so the gap is explained, and zero-postable is called
    out explicitly. ``default`` keeps a previously-saved channel on a re-run.
    """
    try:
        listing = list_channels_fn(token, guild_id)
    except discord_discovery.DiscordDiscoveryError as e:
        print(f"  could not list channels ({e}); re-run init to pick a channel.")
        return None

    if not listing.postable:
        if listing.unpostable:
            names = ", ".join(f"#{c.name}" for c in listing.unpostable)
            print(f"  the bot can see {names} but can't post there (needs Send Messages + Manage Webhooks).")
            print("  Grant those permissions on a channel (or the bot's role), then re-run `disco init`.")
        else:
            print("  this server has no text channels the bot can post in — re-run init after adding one.")
        return None

    if listing.unpostable:
        names = ", ".join(f"#{c.name}" for c in listing.unpostable)
        print(f"  (note: the bot can see but can't post in: {names})")
    choices = [Choice(c.id, f"#{c.name}") for c in listing.postable]
    return prompter.select(
        "Which channel should the agent post in by default?",
        choices,
        default=default if any(c.id == default for c in listing.postable) else listing.postable[0].id,
    )


def _run_broker(prompter: Prompter, *, env_path: Path) -> None:
    """The broker step: a local Tansu (``CALF_HOST_URL=localhost:9092``) or a URL.

    Native is the default — the live finish starts the substrate (broker + bridge)
    detached, so unlike the old flow there is no command to print here. The URL
    branch keeps-existing-on-empty and warns only when a fresh install ends with
    no broker (the processes can't start without one).
    """
    current = _envfile.read_env(env_path)
    choice = prompter.select(
        "Kafka broker?",
        [
            Choice("native", "Start a local Tansu broker (recommended — init starts it for you)"),
            Choice("url", "I have a broker URL"),
        ],
        default="native",
    )
    if choice == "native":
        _envfile.upsert(env_path, {_BROKER_VAR: _LOCAL_BROKER_URL})
        return
    url = prompter.text(f"{_BROKER_VAR} (e.g. broker.example.com:9092):", default=current.get(_BROKER_VAR, ""))
    if url:
        _envfile.upsert(env_path, {_BROKER_VAR: url})
    elif not current.get(_BROKER_VAR):
        print(
            f"  warning: no {_BROKER_VAR} is set — the processes won't start until one "
            f"is (re-run 'disco init' or run 'disco self set-broker <url>')."
        )


def _run_finish(
    prompter: Prompter,
    *,
    name: str,
    home: Path | None,
    agents_dir: Path,
    env_path: Path,
    server_urls: str,
    start_fn: Callable[..., Awaitable[int]] | None,
    agent_start_fn: Callable[..., Awaitable[int]] | None,
    first_reply_fn: Callable[..., Awaitable[bool]] | None,
    pc_binary_fn: Callable[[], str] | None,
) -> int:
    """The ends-live finish (§4.6 / §12.6): start substrate → agent → watch reply.

    Only possible on a **native install** (the supervisor is install-scoped — its
    lock, derived REST port, generated YAML, logs, and shim launcher all live
    under ``$CALFCORD_HOME``). On a dev run (``home is None``) or when the
    process-compose binary is missing, this DEGRADES to honest manual next-steps
    instead of orchestrating something it cannot (no green light that lies).

    On the native happy path it composes :func:`lifecycle.start` →
    :func:`roster.agent_start` → :func:`_wait_for_agent_online` (started FIRST so
    it is already watching the mesh) → an in-flow ``@<name> hello`` prompt once the
    watcher is listening, mapping each failure to its specific hint:

    * substrate not ready → tear-down already happened in ``start``; point at
      ``disco logs`` / ``disco doctor`` (don't misattribute the cause) and stop
      (don't clock the agent into a workspace that isn't up);
    * agent start failed → stop before the presence watch;
    * agent seen online → 🎉; timed out → the bounded "org is live — try it
      yourself / run ``disco doctor``" downgrade.
    """
    pc_binary_fn = pc_binary_fn or _default_pc_binary

    if home is None or not _supervisor_available(pc_binary_fn):
        _print_manual_finish(name)
        return 0

    # Resolve the real orchestration coroutines lazily (import-light): the agent
    # deployment path must not pull supervisor modules at import. The presence
    # watcher (:func:`_wait_for_agent_online`) is a local module function whose own
    # calfkit imports are deferred to its body, so referencing it here adds nothing
    # to init's import graph.
    if start_fn is None or agent_start_fn is None or first_reply_fn is None:
        from calfcord.supervisor import lifecycle, roster

        start_fn = start_fn or lifecycle.start
        agent_start_fn = agent_start_fn or roster.agent_start
        first_reply_fn = first_reply_fn or _wait_for_agent_online

    return asyncio.run(
        _finish_live(
            prompter,
            name=name,
            home=home,
            agents_dir=agents_dir,
            server_urls=server_urls,
            start_fn=start_fn,
            agent_start_fn=agent_start_fn,
            first_reply_fn=first_reply_fn,
        )
    )


async def _finish_live(
    prompter: Prompter,
    *,
    name: str,
    home: Path,
    agents_dir: Path,
    server_urls: str,
    start_fn: Callable[..., Awaitable[int]],
    agent_start_fn: Callable[..., Awaitable[int]],
    first_reply_fn: Callable[..., Awaitable[bool]],
) -> int:
    """Run the native live finish; returns 0 on a live org, non-zero on a failure
    *before* the org could be reached (substrate / agent start)."""
    from calfcord.cli._agents import detect_agents

    print("Opening your workspace (broker + bridge)…")
    # Mirror main.py's _run_lifecycle wiring (DRY): the shim launcher every
    # supervised process execs under, the broker URL, the defined roster, and
    # the mcp.json servers. Unlike `disco start`, a broken mcp.json is a
    # WARNING here: onboarding's job is reaching a live org, and MCP slots are
    # optional — the strict readers surface the error for fixing afterwards.
    from calfcord.mcp.config import McpConfigError, list_server_names, resolve_config_path

    try:
        mcp_servers = list_server_names(resolve_config_path())
    except McpConfigError as exc:
        print(f"  warning: skipping MCP servers ({exc})")
        mcp_servers = []
    launcher = str(home / "shims" / "disco")
    rc = await start_fn(
        home,
        server_urls=server_urls,
        launcher=launcher,
        agent_ids=detect_agents(agents_dir),
        mcp_servers=mcp_servers,
    )
    if rc != 0:
        # start() already tore the substrate down and printed the specific cause;
        # point at the diagnostics rather than guessing one (§12.6 — never
        # misattribute: a cold-start broker failure, a Discord-intents gap, and a
        # bad config all land here, so name the tools that show which it was).
        print(
            "  the workspace didn't come up. Check `disco logs` for the details or "
            "run `disco doctor` to diagnose, then re-run `disco init`."
        )
        return rc

    print(f"Bringing {name} online…")
    rc = await agent_start_fn(home, name=name, server_urls=server_urls)
    if rc != 0:
        return rc

    # In-flow "try it" prompt (§12.6: prompt the @mention INSIDE init). Presence
    # detection is independent of the human's post — the agent registers on the
    # mesh at startup regardless — so the prompt is a genuine "go say hi" nudge
    # while the watcher confirms in the background that the agent came online.
    #
    # Start the watcher FIRST and wait until it is watching (its ``ready`` event,
    # set once the broker is up) before prompting, so the confirmation can fire as
    # soon as the agent registers. The watcher runs as a task that makes progress
    # while we await its readiness; ``_FIRST_REPLY_TIMEOUT_S`` still bounds the
    # whole wait.
    watcher_ready = asyncio.Event()
    watch_task = asyncio.ensure_future(
        first_reply_fn(server_urls, agent_id=name, timeout_s=_FIRST_REPLY_TIMEOUT_S, ready=watcher_ready)
    )
    # Proceed to the prompt the instant EITHER the watcher reports joined OR the
    # task finishes first. Racing against task completion means a watcher that
    # returns/raises before ever signalling ready (broker unreachable, an early
    # error) does not hang the wizard waiting for a readiness that will never
    # come — we fall through to the prompt and let ``await watch_task`` surface
    # the (already-resolved) result or exception.
    ready_wait = asyncio.ensure_future(watcher_ready.wait())
    try:
        await asyncio.wait({ready_wait, watch_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Cancel and drain the readiness waiter (it may still be pending if the
        # task completed first) so it is never left as an un-retrieved pending
        # task — its CancelledError is expected and swallowed.
        ready_wait.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ready_wait
    prompter.confirm(f"In Discord, say:  @{name} hello   — press enter once you've sent it.", default=True)
    print(f"Waiting for {name} to come online…")
    # The org is ALREADY live (substrate + agent both started). Presence detection
    # is advisory, and the watcher opens its OWN Client.connect — a broker drop or
    # transient connect blip mid-watch could raise out of it. Don't let that crash
    # the wizard after the org came up: degrade any failure into the same bounded
    # "org is live — try it yourself / run doctor" fallback below.
    # ``except Exception`` (not bare) deliberately lets asyncio.CancelledError
    # propagate — swallowing the rest is correct here: the failure is in detection,
    # not in the (already-live) org.
    try:
        detected = await watch_task
    except Exception:
        detected = False
    if detected:
        print(f"🎉 {name} is online — your organization is live!")
    else:
        # Bounded fallback (§12.6): never promise more than we detected.
        print(
            f"  your organization is live — try `@{name} hello` in Discord. If nothing replies, run `disco doctor`."
        )
    print()
    print(f"({_REBOOT_NOTE} `disco start` reopens it; `disco status` shows who's online.)")
    return 0


def _print_manual_finish(name: str) -> None:
    """Honest degrade (§12.6): everything is configured; name the manual next steps.

    Used on a dev run or a missing supervisor binary, where init cannot
    orchestrate the install-scoped supervisor. The next step is always named so
    the operator is never stranded at "configured, now what?".
    """
    print(f"Set up agent '{name}'. To bring it online:")
    print("    disco start")
    print(f"    disco agent start {name}")
    print(f"Then in Discord, say: @{name} hello")
    print(f"({_REBOOT_NOTE} Re-run `disco start` after a reboot.)")


def _supervisor_available(pc_binary_fn: Callable[[], str]) -> bool:
    """Whether the process-compose binary the live finish needs is resolvable.

    A missing binary is a degrade branch (§12.6), not a crash: ``resolve_pc_binary``
    raises an actionable :class:`RuntimeError`, which we catch here to fall back to
    the manual next-steps.
    """
    try:
        pc_binary_fn()
    except RuntimeError:
        return False
    return True


def _default_pc_binary() -> str:
    """Resolve the process-compose binary via the supervisor's own resolver.

    Imported lazily so the dev-mode path (which degrades before this is called)
    never pulls the supervisor package at import time (import-light invariant).
    """
    from calfcord.supervisor.lifecycle import resolve_pc_binary

    return resolve_pc_binary()
