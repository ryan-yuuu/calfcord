"""``calfcord agent create [<name>]`` — the reusable agent-creation flow.

This is the one place the "name → describe → provider/model → tools → write"
sequence lives, so the two surfaces that need it — the standalone ``agent
create`` command *and* ``init``'s first-run setup — can never drift on
*how* an agent is brought into being. :func:`create_agent` is the extracted
flow; :func:`run` is the thin ``agent create`` wrapper around it (no seed prune,
offers the optional ``$EDITOR`` prompt step, prints the restart guidance).

Two design rules keep the two callers honest:

* **``create_agent`` never touches ``CALFKIT_AGENT_DEFAULT_PROVIDER``.** The
  agent it writes carries an *explicit* ``provider``/``model`` in its
  frontmatter, so the install-wide default-provider env var is irrelevant to it
  — that env default is purely ``init``'s concern (first-run wants a sensible
  default for *future* agents). Writing it here would let ``agent create`` of a
  one-off OpenAI agent silently flip the install default, surprising the next
  ``init`` re-run.

* **Provider/model/tools all flow through the validated seams.**
  :func:`~calfcord.cli._providers.configure_provider` owns provider-select,
  credential capture, and the live model pick (so an operator can never type a
  slug the provider rejects); :func:`~calfcord.cli._agents.pick_tools` owns the
  pre-checked tool checkbox; :func:`~calfcord.cli._agents.write_agent` owns the
  validate-before-write disk path. This module only sequences them.

``configure_provider`` is imported at module scope (not lazily) so tests can
monkeypatch ``agent_create.configure_provider`` to a fixed ``(provider, model)``
and drive the whole flow without a provider SDK, network, key, or OAuth — the
same pattern the ``init`` tests use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from calfcord.cli._agents import (
    DEFAULT_DESCRIPTION,
    STARTER_AGENT_NAME,
    detect_agents,
    existing_agent,
    pick_tools,
    slug_stem,
    write_agent,
)
from calfcord.cli._envfile import read_env
from calfcord.cli._providers import configure_provider

if TYPE_CHECKING:
    from pathlib import Path

    from calfcord.cli._prompts import Prompter

# The install-wide default-provider env var ``init`` reads to pre-select the
# provider menu. ``create_agent`` only *reads* it (as the menu default); it
# never writes it — see the module docstring's "never touches" rule.
_DEFAULT_PROVIDER_VAR = "CALFKIT_AGENT_DEFAULT_PROVIDER"


class CreatedAgent(NamedTuple):
    """What :func:`create_agent` produced: the agent's resolved ``name`` and ``provider``.

    A named pair (not a bare ``tuple[str, str]``) so the two same-typed strings
    can't be unpacked in the wrong order — callers read ``.name`` / ``.provider``.
    ``init`` uses ``.provider`` to persist the install default; ``agent create``
    uses ``.name`` for its restart hint.
    """

    name: str
    provider: str


def create_agent(
    prompter: Prompter,
    *,
    agents_dir: Path,
    env_path: Path,
    name_default: str | None = None,
    prune_seed: bool = False,
    offer_prompt: bool = True,
) -> CreatedAgent:
    """Run the shared create flow and return the created agent's ``name`` + ``provider``.

    The single create sequence both ``agent create`` (``prune_seed=False``,
    ``offer_prompt=True``) and ``init``'s first-run setup (``prune_seed=True``,
    ``offer_prompt=False``) build on, so the two can't drift on how an agent is
    created. Steps:

    1. **Name.** Defaults to ``name_default`` when given, else the lone existing
       agent's stem (a re-run editing it in place), else the starter name. The
       typed value is slugified so it can't yield an invalid filename; a blank
       answer keeps the default.
    2. **Description.** Pre-filled from the target agent's current description on
       a re-run (so editing in place shows the existing value), else the seed
       default; a blank answer falls back to the seed default.
    3. **Provider + credentials + model.** Delegated wholesale to
       :func:`~calfcord.cli._providers.configure_provider`, which owns
       provider-select, key/Codex auth, and the live model pick. The provider
       menu is pre-selected from the install default-provider env var (read,
       never written here) so a fresh agent biases toward the operator's usual
       choice; ``current_model`` pre-selects an existing agent's model on a
       re-run.
    4. **Tools.** The pre-checked builtin/MCP checkbox via
       :func:`~calfcord.cli._agents.pick_tools`.
    5. **Write.** :func:`~calfcord.cli._agents.write_agent` (validate before
       write; ``prune_seed`` only when the caller opted in).
    6. **Optional prompt edit.** When ``offer_prompt`` and the operator
       confirms, open the new agent's system prompt in ``$EDITOR`` via
       :func:`calfcord.cli.agent_edit.edit_system_prompt` (imported lazily to
       keep this module free of the subprocess/editor concern unless used).

    Returns the created agent's ``name`` and ``provider`` (as a
    :class:`CreatedAgent`) so the caller can word its own success/next-steps
    guidance (``init`` persists the provider as the install default; ``agent
    create`` just names the agent in its restart hint). Lets :class:`ValueError` /
    :class:`OSError` from :func:`~calfcord.cli._agents.write_agent` propagate — the
    caller decides how to report a write failure (and must not print a success
    banner on one).
    """
    current = read_env(env_path)

    # 1. Name. ``name_default`` (explicit) wins; otherwise default to the lone
    # existing agent (re-run editing it) or the starter on a fresh install.
    if name_default is None:
        existing = detect_agents(agents_dir)
        name_default = existing[0] if len(existing) == 1 else STARTER_AGENT_NAME
    typed_name = prompter.text("Agent name:", default=name_default)
    name = slug_stem(typed_name) if typed_name.strip() else name_default

    # 2. Description. Pre-fill from the target (if it already exists) so a re-run
    # shows the current value; a blank answer falls back to the seed default.
    prior = existing_agent(agents_dir, name)
    desc_default = (prior.description if prior else None) or DEFAULT_DESCRIPTION
    typed_desc = prompter.text("Agent description:", default=desc_default)
    description = typed_desc.strip() or DEFAULT_DESCRIPTION

    # 3. Provider + credentials + model. ``configure_provider`` writes only the
    # credential side effect; we read (never write) the install default-provider
    # env var purely to pre-select the menu.
    provider, model = configure_provider(
        prompter,
        env_path=env_path,
        current=current,
        default_provider=current.get(_DEFAULT_PROVIDER_VAR) or "anthropic",
        cheap=False,
        current_model=prior.model if prior else None,
    )

    # 4. Tools.
    tools = pick_tools(prompter, name)

    # 5. Write (validate-before-write; prune only when the caller opted in).
    md_path = write_agent(
        agents_dir,
        name=name,
        description=description,
        provider=provider,
        model=model,
        tools=tools,
        prune_seed=prune_seed,
    )

    # 6. Optional system-prompt edit. Imported lazily so merely importing this
    # module (which ``init`` does at startup) never pulls in the editor/
    # subprocess machinery unless an operator actually opts to edit the prompt.
    if offer_prompt and prompter.confirm(
        "Edit this agent's system prompt now? (opens $EDITOR)", default=False
    ):
        from calfcord.cli.agent_edit import edit_system_prompt

        edit_system_prompt(md_path)

    return CreatedAgent(name=name, provider=provider)


def run(prompter: Prompter, *, agents_dir: Path, env_path: Path, name: str | None = None) -> int:
    """``calfcord agent create [<name>]``: create one agent and return an exit code.

    The standalone create command: it runs :func:`create_agent` with
    ``prune_seed=False`` (adding an agent must never delete the operator's
    starter — only ``init``'s first-run prunes a *pristine* seed) and
    ``offer_prompt=True`` (the operator can jump straight into editing the new
    agent's system prompt). A given ``name`` pre-fills the name prompt; the
    operator can still rename at the prompt.

    On success it names the created agent then prints the terse next-step block
    (behavior #3): a sentence ending in a colon, a blank line, the indented
    command. A brand-new agent comes online via the roster verb (the new
    substrate/roster model), so the steer is ``calfcord agent start <name>`` — not
    the old runner-restart banner. Per the CLI error-handling convention, a write
    failure (``ValueError``/``OSError`` from the validate-before-write path) is
    reported as a single ``error:`` line and returns 1 with no success banner —
    printing "Created agent ..." on a failed write would send the operator off to
    boot processes against an agent that isn't there.
    """
    try:
        created = create_agent(
            prompter,
            agents_dir=agents_dir,
            env_path=env_path,
            name_default=name,
            prune_seed=False,
            offer_prompt=True,
        )
    except (ValueError, OSError) as e:
        # The create path validates before writing, so this is either an invalid
        # value the validator rejected or a filesystem failure during the atomic
        # write — both leave no usable agent on disk. Report and stop without a
        # success banner.
        print(f"error: could not create agent {(name or '?')!r}: {e}")
        return 1

    print(f"Created agent {created.name!r}.")
    print(f"Bring {created.name} online:\n\n  calfcord agent start {created.name}")
    return 0
