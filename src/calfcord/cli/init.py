"""``calfcord init`` — guided first-run setup: one agent plus the install's ``.env``.

This is the onboarding alternative to hand-editing ``.env`` *and* hand-writing an
``agents/<name>.md``: it walks the operator through naming and describing an
agent, picking its provider + credentials + model, choosing its tools, then
supplying the Discord bot credentials and a Kafka broker. The provider key,
Discord secrets, and broker URL are written to the install's ``config/.env``
(dev: ``./.env``) via the position-preserving, atomic, ``chmod 0600`` upsert in
:mod:`calfcord.cli._envfile`; the agent identity is written as an
``agents/<name>.md`` file via :func:`_write_agent`.

Three design constraints shape the flow:

* **It sets up exactly one agent, end to end.** The provider/model/tools steps
  feed a single ``agents/<name>.md`` so a first run yields a usable agent rather
  than just a configured broker. Naming a new agent on an install that still
  carries the *pristine* seeded ``assistant.md`` prunes that starter (see
  :func:`_write_agent`) so the operator ends with one clean agent, not two.
* **It is idempotent and non-destructive to secrets.** Re-running shows the
  current value where sensible and treats an empty answer as "keep what's there"
  for every ``.env`` secret/text field, so an operator can safely re-run to
  change one field without retyping a token. Re-running with an existing agent
  name *updates* that agent's frontmatter in place, preserving its body and
  display name.
* **The provider/model picks always go through validated seams.** Provider
  selection, credential capture (key prompt or inline Codex OAuth), and the
  live model pick are delegated to :func:`calfcord.cli._providers.configure_provider`
  so an operator can never type a model slug the provider would reject. All
  prompting goes through an injected :class:`Prompter`, so the whole flow is
  testable without a TTY.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

import frontmatter

from calfcord.agents import md_writer
from calfcord.agents.definition import AgentDefinition, parse_agent_md
from calfcord.agents.identifier import AGENT_ID_PATTERN
from calfcord.cli import _envfile
from calfcord.cli._agents import detect_agents
from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli._providers import configure_provider
from calfcord.cli.agent_tools import first_line

logger = logging.getLogger(__name__)

# The one-liner that starts a throwaway local Redpanda matching
# ``CALF_HOST_URL=localhost:19092``. Printed (never executed) so the operator
# stays in control of what runs on their box — the README shows it as a
# separate, explicit step.
REDPANDA_DOCKER_CMD = (
    "docker run -d --name calfcord-redpanda -p 19092:19092 \\\n"
    "  docker.redpanda.com/redpandadata/redpanda:latest \\\n"
    "  redpanda start --mode dev-container --smp 1 \\\n"
    "  --kafka-addr internal://0.0.0.0:9092,external://0.0.0.0:19092 \\\n"
    "  --advertise-kafka-addr internal://localhost:9092,external://localhost:19092"
)

_DEFAULT_PROVIDER_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"
_BROKER_VAR = "CALF_HOST_URL"
_LOCAL_BROKER_URL = "localhost:19092"

# The starter agent's name and the *exact* description the installer seeds it
# with. The prune-pristine check in :func:`_write_agent` keys off this string:
# an ``assistant.md`` still carrying it is an untouched seed (safe to remove
# when the operator names a different agent); any other description means the
# operator customized it and it must be preserved.
_STARTER_AGENT_NAME = "assistant"
_DEFAULT_DESCRIPTION = "General-purpose AI teammate — answers questions and helps with tasks."

# Tools that grant shell / filesystem-write reach into the ``calfkit-tools``
# launch directory. Selecting any of them drives the one-line security caution,
# because anyone who can @mention the agent can then drive them.
_DANGEROUS_TOOLS = frozenset({"shell", "write_file", "edit_file"})

# Permitted characters for an agent-name *stem*. The on-disk identifier must
# satisfy ``AgentDefinition.agent_id`` (``[a-z0-9_-]{1,32}``); we slugify toward
# that here so a friendly typed name ("My Helper") becomes a valid stem
# ("my_helper") rather than failing validation at write time.
_STEM_INVALID = re.compile(r"[^a-z0-9_-]+")


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


def _slug_stem(raw: str) -> str:
    """Coerce a typed agent name into a safe ``.md`` filename stem.

    The frontmatter ``name`` (and thus the filename) must match
    ``[a-z0-9_-]{1,32}``; an operator typing "My Helper" should not hit a
    validation error, so we lowercase, turn runs of disallowed characters into
    single underscores, and trim the result. A name that slugifies to nothing
    (e.g. all punctuation) falls back to the starter name rather than producing
    an empty, invalid stem — keeping the wizard moving instead of aborting.
    """
    slug = _STEM_INVALID.sub("_", raw.strip().lower()).strip("_-")
    slug = slug[:32].strip("_-")
    result = slug or _STARTER_AGENT_NAME
    # Postcondition: the stem is a valid agent id, so callers can write it as a
    # filename without a second validation. The fallback guarantees non-empty,
    # and slugification guarantees the charset/length, so this can only fire on
    # a bug in those rules — assert rather than silently emit an invalid stem.
    assert AGENT_ID_PATTERN.fullmatch(result), f"slugified stem {result!r} is not a valid agent id"
    return result


def _existing_agent(agents_dir: Path, name: str) -> AgentDefinition | None:
    """Return the parsed agent at ``agents_dir/<name>.md`` if it parses, else ``None``.

    Used purely to pre-fill the description/model prompts with the operator's
    current values on a re-run that targets an existing agent. A missing or
    malformed file is not an error here — we simply offer the defaults — so the
    parse is guarded; the actual write path validates strictly.
    """
    target = agents_dir / f"{name}.md"
    if not target.is_file():
        return None
    try:
        return parse_agent_md(target)
    except (ValueError, OSError):
        return None


def run(prompter: Prompter, *, env_path: Path, agents_dir: Path) -> int:
    """Run the guided setup flow and return an exit code.

    Steps, in order: agent name → description → provider/credentials/model →
    tools → write the agent file → Discord credentials → broker → next steps.
    All ``.env`` writes go through :func:`_envfile.upsert`; current values are
    read once up front via :func:`_envfile.read_env` so each step can show/keep
    them. An empty answer never overwrites a set secret/text field — that is
    what makes re-runs safe.
    """
    current = _envfile.read_env(env_path)

    def upsert_text(var: str, message: str) -> None:
        """Prompt for an optional text field, defaulting to the current value.

        Writes only when the operator typed something, so an empty answer keeps
        whatever was already on disk — the keep-existing-on-empty contract that
        makes re-runs safe.
        """
        value = prompter.text(message, default=current.get(var, ""))
        if value:
            _envfile.upsert(env_path, {var: value})

    def upsert_secret(var: str, message: str) -> None:
        """Prompt for an optional secret, writing only when a value was entered.

        ``secret`` has no ``default=`` in the Protocol (a masked field can't
        usefully echo the prior value), so an empty answer keeps the existing
        one without ever displaying it.
        """
        value = prompter.secret(message)
        if value:
            _envfile.upsert(env_path, {var: value})

    print("calfcord init — configuring", env_path)
    print()

    # 1. Agent name ---------------------------------------------------------
    # Default to the lone existing agent's name (a re-run editing it in place)
    # or the starter name on a fresh install. A blank answer keeps the default;
    # whatever is typed is slugified so it can't yield an invalid filename.
    existing = detect_agents(agents_dir)
    name_default = existing[0] if len(existing) == 1 else _STARTER_AGENT_NAME
    typed_name = prompter.text("Agent name:", default=name_default)
    name = _slug_stem(typed_name) if typed_name.strip() else _STARTER_AGENT_NAME

    # 2. Agent description --------------------------------------------------
    # Pre-fill from the agent we're about to write to, if it already exists, so
    # a re-run shows the current description; otherwise offer the seed default.
    prior = _existing_agent(agents_dir, name)
    desc_default = (prior.description if prior else None) or _DEFAULT_DESCRIPTION
    typed_desc = prompter.text("Agent description:", default=desc_default)
    description = typed_desc.strip() or _DEFAULT_DESCRIPTION

    print()

    # 3. Provider + credentials + model -------------------------------------
    # ``configure_provider`` owns provider-select, key/Codex-auth, and the live
    # model pick; we only persist the chosen provider as the install default.
    provider, model = configure_provider(
        prompter,
        env_path=env_path,
        current=current,
        default_provider=current.get(_DEFAULT_PROVIDER_VAR) or "anthropic",
        cheap=False,
        current_model=prior.model if prior else None,
    )
    _envfile.upsert(env_path, {_DEFAULT_PROVIDER_VAR: provider})

    print()

    # 4. Tools --------------------------------------------------------------
    selected = _pick_tools(prompter, name)

    print()

    # 5. Write the agent file -----------------------------------------------
    # A validation or filesystem failure here means no usable agent landed on
    # disk, so abort *before* prompting for Discord/broker and before the
    # success banner — reporting success on a half-configured install would
    # send the operator off to boot processes against an agent that won't load.
    try:
        _write_agent(
            agents_dir,
            name=name,
            description=description,
            provider=provider,
            model=model,
            tools=selected,
        )
    except (ValueError, OSError) as e:
        print(f"error: could not create agent {name!r}: {e}")
        return 1

    print()

    # 6. Discord credentials ------------------------------------------------
    print("Discord bot credentials (see docs/discord-setup.md to create the app + token).")
    upsert_secret(
        "DISCORD_BOT_TOKEN",
        f"DISCORD_BOT_TOKEN {_set_label(current.get('DISCORD_BOT_TOKEN', ''))} — paste to set, enter to keep:",
    )
    upsert_text("DISCORD_APPLICATION_ID", "DISCORD_APPLICATION_ID (numeric):")
    upsert_text(
        "DISCORD_GUILD_ID",
        "DISCORD_GUILD_ID (optional — guild-scoped slash sync; enter to skip):",
    )
    upsert_text(
        "DISCORD_DEFAULT_CHANNEL_ID",
        "DISCORD_DEFAULT_CHANNEL_ID (optional — seeds the first agent's channel; enter to skip):",
    )

    print()

    # 7. Broker -------------------------------------------------------------
    broker_choice = prompter.select(
        "Kafka broker?",
        [
            Choice("docker", "Start a local Redpanda in Docker (recommended)"),
            Choice("url", "I have a broker URL"),
        ],
        default="docker",
    )
    if broker_choice == "docker":
        _envfile.upsert(env_path, {_BROKER_VAR: _LOCAL_BROKER_URL})
        print(f"  Set {_BROKER_VAR}={_LOCAL_BROKER_URL}. Start the broker with:")
        print()
        print(REDPANDA_DOCKER_CMD)
    else:
        url = prompter.text(
            f"{_BROKER_VAR} (e.g. broker.example.com:9092):",
            default=current.get(_BROKER_VAR, ""),
        )
        if url:
            _envfile.upsert(env_path, {_BROKER_VAR: url})

    print()

    # 8. Confirm + next steps ----------------------------------------------
    print(f"Set up agent '{name}' in {agents_dir}.")
    print()
    print("Next steps:")
    if broker_choice == "docker":
        print("  1. Start the broker (command above).")
        step = 2
    else:
        step = 1
    print(f"  {step}. Run the four processes (separate terminals):")
    print("       calfcord calfkit-bridge")
    print("       calfcord calfkit-agent")
    print("       calfcord calfkit-router")
    print("       calfcord calfkit-tools")
    print(f"  {step + 1}. In Discord, say: @{name} hello")
    print(
        "  Ambient routing (replies to messages without an @mention) is optional — "
        "run `calfcord router setup` to enable it."
    )

    return 0


def _pick_tools(prompter: Prompter, name: str) -> list[str]:
    """Prompt for the agent's tools and return the selected tokens.

    Every builtin (sorted :data:`calfcord.tools.TOOL_REGISTRY`) is offered
    pre-checked so the default is the same "all builtins" set a frontmatter that
    omits ``tools:`` would expand to; MCP selectors discovered from the committed
    schemas are offered *un*-checked (opt-in). Enumeration uses only the
    schema-only seams — ``TOOL_REGISTRY`` and ``discover_mcp_catalog`` — and
    never touches ``calfcord.mcp.servers`` (transport/secrets), so this works on
    a host that holds no MCP credentials. If a write/shell tool ends up selected
    we print the security caution, because anyone who can @mention the agent can
    then drive it.
    """
    from calfcord.mcp import schemas as schemas_pkg
    from calfcord.mcp.discovery import discover_mcp_catalog
    from calfcord.tools import TOOL_REGISTRY

    choices: list[Choice] = []
    for tool_name in sorted(TOOL_REGISTRY):
        summary = first_line(TOOL_REGISTRY[tool_name].tool_schema.description)
        label = f"{tool_name} — {summary}" if summary else tool_name
        choices.append(Choice(tool_name, label, True))

    try:
        catalog = discover_mcp_catalog(schemas_pkg)
    except Exception as e:
        # A broken generated schema must not brick setup: degrade to builtins
        # only, loudly, so the operator sees the cause. Broad on purpose — a
        # corrupt generated module can raise SyntaxError / AttributeError too,
        # not just ImportError / ValueError, and none of them should abort setup.
        print(f"warning: MCP catalog failed to load, showing builtins only: {e}")
        catalog = {}

    for server in sorted(catalog):
        tools = catalog[server]
        all_selector = f"mcp/{server}"
        choices.append(Choice(all_selector, f"{all_selector} — all {len(tools)} tools", False))
        for tool in tools:
            selector = f"mcp/{server}/{tool.name}"
            summary = first_line(getattr(tool, "description", None))
            label = f"{selector} — {summary}" if summary else selector
            choices.append(Choice(selector, label, False))

    selected = prompter.checkbox(
        f"Tools for {name} (all selected — deselect any you don't want):",
        choices,
        instruction="space toggles, enter confirms",
    )

    if _DANGEROUS_TOOLS.intersection(selected):
        print(
            "note: these tools include shell + file write access in the calfkit-tools "
            "launch dir, drivable by anyone who can @mention this agent — keep the bot "
            "off public Discord (docs/security.md §3.4)."
        )

    return selected


def _display_name_for(name: str) -> str:
    """Derive a sane default ``display_name`` from the agent's slug stem.

    Title-casing the underscore/dash-split stem ("my_helper" → "My Helper")
    gives a human-friendly default the operator can refine later, without
    forcing a separate prompt during first-run setup.
    """
    return name.replace("_", " ").replace("-", " ").title()


def _agent_body(display_name: str) -> str:
    """Render the generic system-prompt body for a brand-new agent.

    A minimal, generic prompt so the agent answers sensibly from the first boot
    without further editing. Kept separate from the frontmatter so the create
    path can serialize identity fields through ``frontmatter.dumps`` (which
    YAML-quotes free-text values safely) rather than string interpolation.
    """
    return (
        f"You are {display_name}, a helpful AI teammate in this Discord workspace. Answer\n"
        "questions and help with tasks clearly and concisely. If you don't know something,\n"
        "say so rather than guessing.\n"
    )


def _atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via a same-dir tmp file + atomic rename.

    A partial agent file would make the next ``calfkit-agent`` boot fail to
    load the directory, so the create path must never leave a half-written file
    behind on error. ``path.parent`` is created first because a fresh install's
    ``agents/`` directory may not exist yet (unlike :mod:`md_writer`'s in-place
    rewrite, which can assume the file — and so the dir — already exists).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _is_pristine_seed(agents_dir: Path) -> bool:
    """True when ``agents_dir/assistant.md`` is the untouched seeded starter.

    "Untouched" is detected by the seed's two stable identity markers: its
    ``agent_id`` is still ``assistant`` and its description is still the exact
    seed default. If the operator customized the description, both halves no
    longer hold and the file must be preserved. A missing or malformed
    ``assistant.md`` is treated as "not a pristine seed" (nothing safe to
    prune), so the parse is guarded — a broken file is never deleted on a guess.
    """
    seed = agents_dir / f"{_STARTER_AGENT_NAME}.md"
    if not seed.is_file():
        return False
    try:
        parsed = parse_agent_md(seed)
    except (ValueError, OSError):
        return False
    return parsed.agent_id == _STARTER_AGENT_NAME and parsed.description == _DEFAULT_DESCRIPTION


def _write_agent(
    agents_dir: Path,
    *,
    name: str,
    description: str,
    provider: str,
    model: str,
    tools: list[str],
) -> Path:
    """Create or update ``agents_dir/<name>.md`` for the wizard's agent.

    Two paths, both validate-before-write so a bad value never lands on disk:

    * **Target exists** — update the agent in place, preserving its body and
      ``display_name``: rewrite ``description``/``provider``/``model`` via
      :func:`md_writer._update_fields`, then the tool list via
      :func:`md_writer.update_tools`. Both are validated-atomic, so a bad value
      leaves the file untouched.
    * **Target missing** — build the frontmatter as a mapping and serialize it
      with :func:`frontmatter.dumps` (NOT string interpolation), which
      YAML-quotes free-text values so a description like ``"Calendar: book
      meetings"`` or one carrying quotes/``#``/leading punctuation can't corrupt
      the file. The synthetic :class:`AgentDefinition` is built *first* (mirroring
      :func:`md_writer._update_fields`), so an invalid value raises before any
      disk write. After the atomic write, when the operator named a *different*
      agent (``name != "assistant"``) on an install still carrying the *pristine*
      seeded ``assistant.md``, that seed is deleted so they end with one clean
      agent. A *customized* ``assistant.md`` (or naming the agent ``assistant``
      itself) is never deleted.

    Raises:
        ValueError: a field value fails :class:`AgentDefinition` validation
            (create path) or the existing ``.md``/new value is invalid (update
            path). No partial file is written.
        OSError: a filesystem error during the atomic write. No partial file is
            written.
    """
    target = agents_dir / f"{name}.md"

    if target.exists():
        md_writer._update_fields(target, {"description": description, "provider": provider, "model": model})
        md_writer.update_tools(target, tools)
        return target

    display_name = _display_name_for(name)
    body = _agent_body(display_name)
    metadata = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "provider": provider,
        "model": model,
        "tools": list(tools),
    }
    # Validate the full definition in memory FIRST (mirrors
    # md_writer._update_fields): a bad free-text value raises here, before any
    # bytes touch disk, so the create path can never leave an unloadable file.
    AgentDefinition(**{**metadata, "system_prompt": body, "source_path": target})

    payload = frontmatter.dumps(frontmatter.Post(body, **metadata))
    if not payload.endswith("\n"):
        payload += "\n"
    _atomic_write(target, payload)

    # Prune the pristine starter only when a *different* agent was created;
    # naming the agent ``assistant`` would have hit the update path above.
    if name != _STARTER_AGENT_NAME and _is_pristine_seed(agents_dir):
        (agents_dir / f"{_STARTER_AGENT_NAME}.md").unlink(missing_ok=True)
        logger.info("pruned pristine seed assistant.md after creating %s", target)

    return target
