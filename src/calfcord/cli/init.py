"""``calfcord init`` — guided first-run configuration of the install's ``.env``.

This is the onboarding alternative to hand-editing ``.env``: it walks the
operator through picking a provider + supplying its key, the Discord bot
credentials, and a Kafka broker, then writes the answers to the install's
``config/.env`` (dev: ``./.env``) via the position-preserving, atomic,
``chmod 0600`` upsert in :mod:`calfcord.cli._envfile`.

Two design constraints shape the flow:

* **It configures, it never seeds.** Dropping in the starter ``assistant.md``
  is the installer's job; ``init`` only *detects and reports* the agent so the
  starter content lives in exactly one place (the plan's "init never seeds").
* **It is idempotent and non-destructive.** Re-running shows the current value
  where sensible and treats an empty answer as "keep what's there", so an
  operator can safely re-run to change one field without retyping secrets. All
  prompting goes through an injected :class:`Prompter`, so the whole flow is
  testable without a TTY.
"""

from __future__ import annotations

import os
from pathlib import Path

from calfcord.cli import _envfile
from calfcord.cli._agents import detect_agents
from calfcord.cli._prompts import Choice, Prompter

# Selectable providers; values match the ``provider:`` frontmatter Literal
# (:data:`calfcord.agents.definition.Provider`) and the
# ``CALFKIT_AGENT_DEFAULT_PROVIDER`` env var that drives the default. A drift
# guard test keeps this list in sync with the Literal.
PROVIDERS: list[Choice] = [
    Choice("anthropic", "Anthropic (Claude)"),
    Choice("openai", "OpenAI (GPT)"),
    Choice("openai-codex", "ChatGPT subscription (Codex)"),
]

# Providers that authenticate via a plain API-key env var. ``openai-codex`` is
# absent on purpose: it uses the OAuth flow behind ``calfcord calfkit-auth``,
# not a key in ``.env``.
PROVIDER_KEY_VAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

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


def run(prompter: Prompter, *, env_path: Path, agents_dir: Path) -> int:
    """Run the guided config flow against ``env_path`` and return an exit code.

    All writes go through :func:`_envfile.upsert`; current values are read once
    up front via :func:`_envfile.read_env` so each step can show/keep them. An
    empty answer never overwrites a set value — that is what makes re-runs safe.
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

    # 1. Provider + its credential ------------------------------------------
    provider = prompter.select(
        "Default model provider for your agents?",
        PROVIDERS,
        default=current.get(_DEFAULT_PROVIDER_VAR) or "anthropic",
    )
    _envfile.upsert(env_path, {_DEFAULT_PROVIDER_VAR: provider})

    if provider in PROVIDER_KEY_VAR:
        key_var = PROVIDER_KEY_VAR[provider]
        upsert_secret(key_var, f"{key_var} {_set_label(current.get(key_var, ''))} — paste to set, enter to keep:")
    elif provider == "openai-codex":
        print("  ChatGPT subscription needs no key here. Authenticate once with:")
        print("    calfcord calfkit-auth login")

    print()

    # 2. Discord credentials ------------------------------------------------
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

    # 3. Broker -------------------------------------------------------------
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

    # 4. Detect agents ------------------------------------------------------
    agents = detect_agents(agents_dir)
    if agents:
        print(f"Found {len(agents)} agent(s) in {agents_dir}: {', '.join(agents)}")
    else:
        print(f"No agents found in {agents_dir}.")
        print("  The starter agent is 'assistant' (agents/assistant.md), seeded by the installer.")
        print(f"  Add an agent by dropping a <name>.md file into {agents_dir}.")

    print()

    # 5. Next steps ---------------------------------------------------------
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
    print(f"  {step + 1}. In Discord, say: @assistant hello")

    return 0
