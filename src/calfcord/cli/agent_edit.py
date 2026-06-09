"""``calfcord agent edit [<name>]`` — the interactive field-menu editor.

The workhorse of the editing surface: resolve an agent, then loop a menu
of its editable fields (sourced from :data:`calfcord.cli._fields.FIELDS`, the one
registry every edit/set/show surface shares) where each row shows the field's
current value and selecting it edits exactly that field. This module also owns
:func:`edit_system_prompt`, the ``$EDITOR`` helper the menu's *prompt* row and
``agent create``'s optional prompt step both reuse.

Three design rules shape the loop:

* **Re-read before every edit.** Each iteration re-parses the ``.md`` with
  :func:`~calfcord.agents.definition.parse_agent_md` so the menu always shows the
  freshly-written value (a just-edited field reflects immediately) and so an edit
  validates against current on-disk state, never a stale snapshot.

* **A bad value must not crash the menu.** Every field edit is wrapped so a
  validation/OS error (an out-of-range ``history_turns``, a read-only file)
  prints one ``error:`` line and *continues* the loop. The writes go through the
  validate-before-write seams (:func:`calfcord.cli._fields.write_simple_field`,
  :func:`calfcord.agents.md_writer`), so a rejected value leaves the on-disk file
  untouched — the operator can simply retry.

* **Each field reaches its own validated seam.** Simple scalars ride
  :func:`~calfcord.cli._fields.write_simple_field`; the provider+model pair rides
  the shared provider flow then :func:`calfcord.agents.md_writer._update_fields`;
  ``tools`` delegates to the existing :func:`calfcord.cli.agent_tools.run`
  checkbox editor; ``prompt`` rides :func:`edit_system_prompt`. The menu never
  re-encodes a field's validation — it routes to the seam that owns it.

Rename and delete are deliberately *not* in this menu — they change the agent's
identity/existence (and its filename), so they are separate commands.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from calfcord.agents import md_writer
from calfcord.agents.definition import parse_agent_md
from calfcord.cli import agent_tools
from calfcord.cli._agents import detect_agents
from calfcord.cli._envfile import read_env
from calfcord.cli._fields import FIELDS, FIELDS_BY_KEY, render_value, write_simple_field
from calfcord.cli._prompts import Choice, Prompter
from calfcord.cli._providers import configure_provider

# The menu's sentinel "I'm done" row value. Chosen with surrounding double
# underscores so it can never collide with a real :class:`Field.key` (all of
# which are plain frontmatter attribute names).
_DONE = "__done__"


def edit_system_prompt(md_path: Path) -> None:
    """Open the agent's system-prompt **body** in ``$EDITOR`` and save the result.

    The system prompt is free-form prose, so an inline single-line prompt is the
    wrong tool — we hand the operator their real editor. The current body is
    written to a temp ``.md`` file, ``$EDITOR`` (or ``vi`` when unset) is launched
    on it, and the edited contents are read back and persisted via the
    validate-before-write :func:`calfcord.agents.md_writer.update_system_prompt`.

    Defensive on every failure mode that an interactive editor invites, because
    this runs inside the edit menu and must never let an exception escape and
    tear the menu down:

    * **No change / emptied.** If the operator saves without changes, or empties
      the file (a whitespace-only body the validator would reject anyway), print
      a note and return without writing — leaving the existing prompt intact.
    * **Editor can't be launched.** A missing ``$EDITOR`` binary
      (:class:`FileNotFoundError`) prints a clear hint to set ``$EDITOR`` and
      returns, rather than surfacing a raw stack trace.
    * **Validation / OS error.** A rejected body or a filesystem error during the
      atomic write prints one ``error:`` line and returns; the on-disk file is
      left untouched by the validate-before-write seam.

    Never raises out of the menu — every recoverable failure is reported and
    swallowed here.
    """
    try:
        current_body = parse_agent_md(md_path).system_prompt
    except (ValueError, OSError) as e:
        # An unreadable/malformed .md can't be edited via $EDITOR meaningfully;
        # report and return so the menu (or create flow) keeps going.
        print(f"error: cannot read agent prompt: {e}")
        return

    # A temp .md so an editor with Markdown niceties keys off the extension; in
    # the same dir would be ideal for atomicity but the write goes through the
    # md_writer's own atomic path, so a plain system temp file is fine here.
    fd, tmp_name = tempfile.mkstemp(prefix="calfcord-prompt-", suffix=".md")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(current_body)

        # ``shlex.split`` so an EDITOR carrying args ("code --wait", "emacs -nw")
        # is honoured rather than treated as one impossible binary name.
        editor = os.environ.get("EDITOR") or "vi"
        argv = [*shlex.split(editor), str(tmp_path)]
        try:
            subprocess.run(argv, check=True)
        except FileNotFoundError:
            # The configured editor binary doesn't exist on PATH — the single
            # failure that a generic "error:" line would leave the operator
            # unable to act on, so hint at the concrete fix.
            print(
                f"error: could not launch editor {editor!r}; set $EDITOR to an "
                f"installed editor (e.g. EDITOR=nano) and try again."
            )
            return
        except (subprocess.CalledProcessError, OSError) as e:
            # The editor exited non-zero / couldn't run — treat as "operator
            # aborted", don't write, but say why.
            print(f"error: editor exited without saving ({e}); prompt unchanged.")
            return

        try:
            new_body = tmp_path.read_text(encoding="utf-8")
        except (ValueError, OSError) as e:
            # Reading the edited temp file back can fail — e.g. the operator saved
            # a non-UTF-8 body (``UnicodeDecodeError``, a ``ValueError``). Catch it
            # here so this function honours its "never raises out" contract even
            # when called from ``agent create``'s prompt step, which has no menu
            # loop to absorb a stray exception.
            print(f"error: could not read the edited prompt ({e}); prompt unchanged.")
            return
    finally:
        tmp_path.unlink(missing_ok=True)

    # No-op guard: unchanged or emptied → don't touch the file. An empty body
    # would fail the validator anyway; reporting it as "unchanged" reads better
    # than an "error:" for the common "saved without editing" case.
    if not new_body.strip() or new_body == current_body:
        print("Prompt unchanged.")
        return

    try:
        md_writer.update_system_prompt(md_path, new_body)
    except (ValueError, OSError) as e:
        print(f"error: {e}")
        return
    print("Updated system prompt.")


def _resolve_agent(prompter: Prompter, *, agents_dir: Path, name: str | None) -> Path | None:
    """Resolve the agent ``.md`` to edit, or ``None`` after printing why not.

    Mirrors :func:`calfcord.cli.agent_tools._resolve_agent` so the two editors
    agree on resolution: a given ``name`` must exist (an explicit request for a
    missing agent is an error, not a fallback to the picker); an omitted ``name``
    lists the detected agents and prompts, with an empty dir an error too.
    Returning ``None`` lets :func:`run` map every "can't proceed" case to a
    single already-printed message + exit code 1.
    """
    if name is not None:
        md_path = agents_dir / f"{name}.md"
        if not md_path.is_file():
            print(f"error: no agent {name!r} in {agents_dir} (expected {md_path})")
            return None
        return md_path

    agents = detect_agents(agents_dir)
    if not agents:
        print(f"no agents in {agents_dir}")
        return None
    chosen = prompter.select(
        "Which agent do you want to edit?",
        [Choice(a, a) for a in agents],
    )
    return agents_dir / f"{chosen}.md"


def _edit_field(
    prompter: Prompter,
    *,
    md_path: Path,
    agents_dir: Path,
    env_path: Path,
    name: str,
    field_key: str,
) -> bool:
    """Edit the one field ``field_key`` and report whether anything changed.

    Re-parses the ``.md`` fresh so the prompt's default reflects current on-disk
    state, then dispatches by the field's :attr:`~calfcord.cli._fields.Field.kind`
    to the seam that owns that field's validation:

    * ``text`` / ``select`` / ``int`` / ``bool`` ride the one validated-atomic
      :func:`~calfcord.cli._fields.write_simple_field` (which coerces and lets
      :class:`~calfcord.agents.definition.AgentDefinition` enforce the
      constraint), and only write when the value actually changed;
    * ``provider_model`` runs the shared provider flow (so a model the provider
      rejects can't be typed) then writes provider+model together via the one
      validated-atomic metadata seam;
    * ``tools`` delegates to the existing checkbox editor;
    * ``prompt`` delegates to :func:`edit_system_prompt`.

    Returns ``True`` when an edit was attempted (the menu uses this only to decide
    whether to print the restart hint at the end). The simple-scalar branches
    return ``False`` on a no-op (the value equalled the current one); the compound
    branches (``provider_model`` / ``tools`` / ``prompt``) delegate to sub-editors
    that report their own outcome, so they conservatively return ``True`` even
    when nothing changed — the cost is at most a redundant restart hint. Raises
    :class:`ValueError` / :class:`OSError` on a bad value or a filesystem failure —
    :func:`run` catches it, prints one ``error:`` line, and continues the loop (the
    validated-write seams leave the file untouched on failure).
    """
    defn = parse_agent_md(md_path)
    field = FIELDS_BY_KEY[field_key]

    if field.kind == "text":
        current = getattr(defn, field.key) or ""
        new = prompter.text(f"{field.label}:", default=str(current))
        if new != str(current):
            write_simple_field(md_path, field, new)
            return True
        return False

    if field.kind == "select":
        current = getattr(defn, field.key)
        choices = [Choice(c, c) for c in field.choices or ()]
        new = prompter.select(field.label, choices, default=current if current else None)
        if new != current:
            write_simple_field(md_path, field, new)
            return True
        return False

    if field.kind == "int":
        current = getattr(defn, field.key)
        new = prompter.text(
            f"{field.label} ({field.int_min}-{field.int_max}):",
            default=str(current),
        )
        if new != str(current):
            write_simple_field(md_path, field, new)
            return True
        return False

    if field.kind == "bool":
        current = bool(getattr(defn, field.key))
        new = prompter.confirm(f"{field.label}?", default=current)
        if new != current:
            # ``write_simple_field`` coerces a string; hand it the lowercase
            # spelling ``_coerce_bool`` accepts ("true"/"false").
            write_simple_field(md_path, field, str(new).lower())
            return True
        return False

    if field.kind == "provider_model":
        # The provider+model pair shares the validated provider flow (so a model
        # the provider rejects can't be typed), then writes both together through
        # the one validated-atomic metadata seam.
        provider, model = configure_provider(
            prompter,
            env_path=env_path,
            current=read_env(env_path),
            default_provider=defn.provider,
            current_model=defn.model,
        )
        md_writer._update_fields(md_path, {"provider": provider, "model": model})
        return True

    if field.kind == "tools":
        # Reuse the existing checkbox editor wholesale — it owns the
        # builtin-tool enumeration, pre-checking, and validated write.
        agent_tools.run(prompter, agents_dir=agents_dir, name=name)
        return True

    if field.kind == "prompt":
        edit_system_prompt(md_path)
        return True

    # Unreachable: every FIELDS kind is handled above. A new kind without a
    # branch should fail loudly in development rather than silently no-op.
    raise ValueError(f"no editor for field kind {field.kind!r}")


def run(prompter: Prompter, *, agents_dir: Path, env_path: Path, name: str | None = None) -> int:
    """``calfcord agent edit [<name>]``: loop a field menu over one agent.

    Resolves the agent (given ``name`` must exist; omitted ``name`` prompts a
    picker; an empty agents dir is an error), then loops: show each editable
    field with its current value plus a ``✓ Done`` row, edit the chosen field
    through its owning validated seam, and re-read before the next iteration so
    the menu always reflects disk. Per the CLI error-handling convention, a bad
    value or a filesystem hiccup during one edit prints a single ``error:`` line
    and the loop continues (the validate-before-write seams leave the file
    untouched), so a typo never drops the operator out of the menu.

    Returns 1 (with an already-printed message) when no agent can be resolved;
    otherwise 0, printing the restart hint when at least one field was changed.
    """
    md_path = _resolve_agent(prompter, agents_dir=agents_dir, name=name)
    if md_path is None:
        return 1
    agent_name = md_path.stem

    changed = False
    while True:
        try:
            defn = parse_agent_md(md_path)
        except (ValueError, OSError) as e:
            # A malformed/unreadable .md can't drive the menu — report and exit
            # non-zero rather than loop forever on an unparseable file.
            print(f"error: cannot read agent {agent_name!r}: {e}")
            return 1

        rows = [Choice(f.key, f"{f.label} — {render_value(defn, f)}") for f in FIELDS]
        rows.append(Choice(_DONE, "✓ Done"))
        choice = prompter.select("What do you want to change?", rows)
        if choice == _DONE:
            break

        try:
            if _edit_field(
                prompter,
                md_path=md_path,
                agents_dir=agents_dir,
                env_path=env_path,
                name=agent_name,
                field_key=choice,
            ):
                changed = True
        except (ValueError, OSError) as e:
            # A rejected value (an out-of-range history_turns, a bad
            # thinking_effort choice, an unknown tool) or a filesystem error — the
            # validated-write path left the file untouched, so just report and keep
            # looping. (The memory→fs-tools requirement is NOT enforced here; it is
            # a build-time check in AgentFactory, so enabling memory writes fine and
            # surfaces at the next calfkit-agent boot.)
            print(f"error: {e}")

    if changed:
        # The terse next-step block (behavior #3): a sentence ending in a colon, a
        # blank line, the two-space-indented command. A config edit takes effect on
        # a running agent via the roster `restart` verb (the node bakes its config at
        # construction); the parenthetical flags that a provider/key change can
        # affect every agent sharing that provider, so a same-provider fleet may need
        # restarting too. ``defn`` is the last re-read definition (the menu re-parses
        # each iteration), so it reflects the just-applied provider.
        print(
            f"Restart {agent_name} to apply (and any other agents on {defn.provider} "
            f"if the provider/key changed):\n\n  calfcord agent restart {agent_name}"
        )
    return 0
