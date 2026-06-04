"""``calfcord init`` — guided first-run setup: one agent plus the install's ``.env``.

This is the onboarding alternative to hand-editing ``.env`` *and* hand-writing an
``agents/<name>.md``: it walks the operator through naming and describing an
agent, picking its provider + credentials + model, choosing its tools, then
supplying the Discord bot credentials and a Kafka broker. The provider key,
Discord secrets, and broker URL are written to the install's ``config/.env``
(dev: ``./.env``) via the position-preserving, atomic, ``chmod 0600`` upsert in
:mod:`calfcord.cli._envfile`; the agent identity is written as an
``agents/<name>.md`` file via :func:`calfcord.cli._agents.write_agent`.

Three design constraints shape the flow:

* **It sets up exactly one agent, end to end.** The provider/model/tools steps
  feed a single ``agents/<name>.md`` so a first run yields a usable agent rather
  than just a configured broker. Naming a new agent on an install that still
  carries the *pristine* seeded ``assistant.md`` prunes that starter (see
  :func:`calfcord.cli._agents.write_agent`) so the operator ends with one clean
  agent, not two.
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

import os
from pathlib import Path

from calfcord.cli import _envfile
from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli.agent_create import create_agent

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

    # 1-5. Agent identity, provider + model, tools, and the agent file — the
    # shared create flow ``calfcord agent create`` also runs, so the two can't
    # drift on how an agent is made. ``init`` opts into pruning the pristine
    # starter (one clean first agent) and skips the optional $EDITOR prompt step
    # (the quick start stays lean), then persists the chosen provider as the
    # install default. A validation/filesystem failure means no usable agent
    # landed on disk, so abort before the Discord/broker prompts and the success
    # banner rather than send the operator off to boot against an agent that
    # won't load.
    try:
        name, provider = create_agent(
            prompter, agents_dir=agents_dir, env_path=env_path, prune_seed=True, offer_prompt=False
        )
    except (ValueError, OSError) as e:
        print(f"error: could not create agent: {e}")
        return 1
    _envfile.upsert(env_path, {_DEFAULT_PROVIDER_VAR: provider})

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
