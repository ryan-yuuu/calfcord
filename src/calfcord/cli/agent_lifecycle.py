"""``calfcord agent set`` / ``rename`` / ``delete`` — non-interactive agent mutation.

The write-side commands. ``set`` applies one or more ``--field value``
edits to an agent's ``.md`` through the *validated* write paths the rest of the
system already owns; ``rename`` and ``delete`` are file operations on the
agent's ``agents/<name>.md``.

Two invariants shape this module:

* **Every metadata write goes through a validate-before-write seam.** ``set``
  never serializes frontmatter itself: simple fields ride
  :func:`calfcord.cli._fields.write_simple_field`, ``tools`` rides
  :func:`calfcord.agents.md_writer.update_tools`, provider/model and the rename's
  ``name`` change ride :func:`calfcord.agents.md_writer._update_fields`, and the
  prompt body rides :func:`calfcord.agents.md_writer.update_system_prompt`. All
  build and validate a synthetic :class:`~calfcord.agents.definition.AgentDefinition`
  in memory first, so a bad value leaves the on-disk file untouched.

* **Rename never loses the agent.** :func:`rename_agent` orders its steps so a
  failure can never destroy the agent: the new ``.md`` is written (and validated)
  *before* the old one is removed, so a crash mid-rename leaves at worst a
  recoverable both-files state, never a no-agent state.

The ``run_*`` wrappers map every operator-recoverable failure to an ``error:``
line + exit code 1 (per the CLI error convention — no traceback escapes); the
reusable ``rename_agent`` / ``delete_agent`` file-ops raise so other callers can
compose them. Mutations take effect on the next ``calfkit-agent`` (and, for
identity changes, ``calfkit-bridge``) boot — the node bakes its config at
construction — so each success line tells the operator to restart.

This module imports only the lightweight ``calfcord.agents`` / ``calfcord.cli``
seams (no provider SDK), so it stays importable from the argparse entry point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import yaml

from calfcord.agents import md_writer
from calfcord.agents.definition import AgentDefinition, parse_agent_md
from calfcord.cli._agents import atomic_write, slug_stem
from calfcord.cli._fields import FIELDS_BY_KEY, write_simple_field

if TYPE_CHECKING:
    from pathlib import Path

    from calfcord.cli._prompts import Prompter

# Frontmatter keys ``set`` accepts as standalone update keys even though the
# editable-field registry models them as the single ``provider_model`` row: the
# CLI exposes ``--model`` and ``--provider`` as two flags (you can set a model
# without restating the provider), so the two raw update keys must dispatch to
# the shared ``_update_fields`` seam directly.
_PROVIDER_MODEL_KEYS = ("provider", "model")


def run_set(agents_dir: Path, name: str, updates: dict[str, str]) -> int:
    """``calfcord agent set <name> --field value …``: apply validated edits.

    ``updates`` maps a field key (``description``, ``tools``, ``provider`` /
    ``model``, ``system_prompt``, …) to its raw string value; ``main.py`` parses
    the ``--flag``s from :data:`FIELDS` into this dict. Each update dispatches to
    the matching *validated* write path by its field kind (see the module
    docstring); ``provider``/``model`` and ``system_prompt`` are accepted as
    standalone keys even though the registry models provider+model as one row and
    the prompt as the Markdown body.

    The agent must exist (else an ``error:`` line + return 1) and at least one
    update is required. Each field is its own validated-atomic write, so a later
    field's failure can't corrupt an earlier success: any ``ValueError``/``OSError``
    (a bad choice, an invalid boolean, an unknown tool, an unwritable file) prints
    ``error: <field>: <e>``, names any fields already applied this call (they stay
    written), and returns 1. On full success it names the fields it wrote and tells
    the operator to restart. Returns 0.
    """
    md_path = agents_dir / f"{name}.md"
    if not md_path.is_file():
        print(f"error: no agent {name!r} in {agents_dir} (expected {md_path})")
        return 1
    if not updates:
        print("error: no updates given; pass at least one --field value")
        return 1

    # A provider switch carries its model on every interactive surface (the wizard
    # and the edit menu both write the pair together). The standalone ``--provider``
    # flag can't, so flag the case where the existing model may not be valid for
    # the new provider — the mismatch would otherwise only surface at the first
    # model call.
    if "provider" in updates and "model" not in updates:
        print(
            "warning: --provider was set without --model; the agent keeps its "
            "current model, which may not be valid for the new provider — pass "
            "--model too if it isn't."
        )

    written: list[str] = []
    for key, raw in updates.items():
        try:
            _apply_one(md_path, key, raw)
        except (ValueError, OSError) as e:
            # Validated writes fail in memory before touching disk, so any field
            # already written this call stays written and the failing field's file
            # is unchanged. Name what landed (so a partial apply isn't a surprise),
            # then report the offending field and stop.
            if written:
                print(f"note: already applied {', '.join(written)} before this error.")
            print(f"error: {key}: {e}")
            return 1
        written.append(key)

    print(f"Updated {name} ({', '.join(written)}).")
    # The terse next-step block (behavior #3): a sentence ending in a colon, a
    # blank line, the two-space-indented command. A config edit takes effect on a
    # running agent via the roster `restart` verb (the node bakes its config at
    # construction); the parenthetical flags that a provider/key change can affect
    # every agent sharing that provider, so a same-provider fleet may need
    # restarting too. We re-read the resolved provider off disk so the caveat names
    # the agent's CURRENT provider (post-`set`), whether or not it was just changed.
    #
    # The re-read is GUARDED: the success line above already printed, so a re-read
    # that raises (a now-unparsable `.md` — e.g. an external edit racing this write)
    # must not escape as a traceback on an otherwise-successful command. On failure,
    # drop the provider parenthetical (we can't name the provider) but still steer
    # the operator to restart, which is the load-bearing half of the hint.
    try:
        provider = parse_agent_md(md_path).provider
    except (ValueError, OSError):
        print(f"Restart {name} to apply:\n\n  calfcord agent restart {name}")
    else:
        print(
            f"Restart {name} to apply (and any other agents on {provider} if the "
            f"provider/key changed):\n\n  calfcord agent restart {name}"
        )
    return 0


def _apply_one(md_path: Path, key: str, raw: str) -> None:
    """Dispatch one ``set`` update to its validated write path by field kind.

    Centralizes the key→seam routing so ``run_set`` stays a thin loop and the
    "which write path owns this field" decision lives in one place. ``provider``
    and ``model`` are handled before the registry lookup because they are not
    :data:`FIELDS_BY_KEY` keys (the registry models them as one ``provider_model``
    row); ``system_prompt`` and ``tools`` have dedicated md_writer seams; every
    remaining simple field rides :func:`write_simple_field`.

    Raises:
        ValueError: an unknown field key, or any validation failure from the
            underlying write path. The on-disk file is unchanged.
        OSError: a filesystem error during the atomic write. The on-disk file is
            unchanged.
    """
    if key in _PROVIDER_MODEL_KEYS:
        md_writer._update_fields(md_path, {key: raw})
        return

    field = FIELDS_BY_KEY.get(key)
    if field is None:
        raise ValueError(f"unknown field {key!r}")

    if field.kind == "tools":
        # Comma-separated on the flag; update_tools validates each token and
        # always persists an explicit list.
        md_writer.update_tools(md_path, [t.strip() for t in raw.split(",") if t.strip()])
        return
    if field.kind == "prompt":
        # ``raw`` is the prompt body text (main.py expands an ``@file`` argument
        # before handing it here); update_system_prompt rejects an empty body.
        md_writer.update_system_prompt(md_path, raw)
        return

    # Simple text/select/bool field — the one shared validated-atomic seam.
    write_simple_field(md_path, field, raw)


def rename_agent(agents_dir: Path, old: str, new: str) -> None:
    """Rename agent ``old`` to ``new`` by moving its ``agents/<name>.md``.

    Reusable file-op (no prompts). Validates ``new`` to a legal agent stem,
    rewrites the old ``.md``'s frontmatter ``name`` and writes it to
    ``agents_dir/<new>.md``, then deletes the old ``.md``.

    Order of operations is chosen so a failure can never lose the agent: the new
    ``.md`` is validated in memory and written *first*, and the old ``.md`` is
    removed *only after* the new one is durably in place. A crash mid-rename
    therefore leaves at worst a recoverable both-files state, never a no-agent
    state.

    Raises:
        ValueError: ``new`` is not a legal agent stem, equals ``old``, the source
            ``.md`` is missing/unparseable, or the target ``agents_dir/<new>.md``
            already exists — renaming onto it would clobber a live agent.
        OSError: a filesystem error writing the new ``.md`` or deleting the old.
    """
    new_stem = slug_stem(new)
    if new_stem == old:
        raise ValueError(f"new name {new!r} resolves to the same agent id {old!r}")

    old_md = agents_dir / f"{old}.md"
    new_md = agents_dir / f"{new_stem}.md"
    if not old_md.is_file():
        raise ValueError(f"no agent {old!r} in {agents_dir} (expected {old_md})")
    if new_md.exists():
        raise ValueError(f"target agent {new_stem!r} already exists ({new_md}); pick a different name")

    payload = _rewritten_md(old_md, new_stem, new_md)

    # Write the new file FIRST so the old one is only removed once the rename
    # target is durably on disk — a failure here leaves the original intact.
    atomic_write(new_md, payload)
    try:
        old_md.unlink()
    except OSError:
        # The new ``.md`` is already written; if we can't remove the old one we
        # would leave TWO live agents on disk (old + new). Roll the new file back
        # so the rename fails cleanly to the original single-agent state.
        new_md.unlink(missing_ok=True)
        raise


def _rewritten_md(old_md: Path, new_stem: str, new_md: Path) -> str:
    """Serialize ``old_md``'s content with its frontmatter ``name`` set to ``new_stem``.

    Validates the result in memory before returning the payload — mirroring
    md_writer's validate-before-write seam — so :func:`rename_agent` never writes
    a ``.md`` that the loader would later reject (the synthetic
    :class:`AgentDefinition` is built with ``source_path`` and the stripped body
    exactly as :func:`parse_agent_md` does). The ``name`` rewrite is the whole
    point of a rename: the loader enforces ``stem == name``, so the new file's
    frontmatter must carry the new id, not the old.

    Raises:
        ValueError: the source ``.md`` is unparseable YAML or the rewritten
            metadata fails :class:`AgentDefinition` validation.
        OSError: the source ``.md`` cannot be read.
    """
    try:
        post = frontmatter.load(old_md)
    except yaml.YAMLError as e:
        raise ValueError(f"{old_md}: existing frontmatter is malformed YAML: {e}") from e

    post.metadata["name"] = new_stem

    # Validate the rewritten definition in memory FIRST (mirrors
    # md_writer._update_fields): build it against the NEW path so source_path is
    # correct, with the stripped body as system_prompt, before any bytes are
    # written.
    candidate = dict(post.metadata)
    candidate["system_prompt"] = post.content.strip()
    candidate["source_path"] = new_md
    AgentDefinition(**candidate)

    payload = frontmatter.dumps(post)
    if not payload.endswith("\n"):
        payload += "\n"
    return payload


def run_rename(agents_dir: Path, old: str, new: str) -> int:
    """``calfcord agent rename <old> <new>``: rename an agent.

    Thin wrapper over :func:`rename_agent` that maps any ``ValueError``/``OSError``
    to an ``error:`` line + return 1 (per the CLI convention — no traceback
    escapes). On success it reports the rename and tells the operator to restart
    both the agent runner and the bridge, since the agent id (and thus its
    ``/<name>`` slash command and Kafka identity) changed. Returns 0.
    """
    try:
        rename_agent(agents_dir, old, new)
    except (ValueError, OSError) as e:
        print(f"error: {e}")
        return 1
    print(
        f"Renamed {old!r} -> {slug_stem(new)!r}. Restart `calfcord calfkit-agent` and "
        f"`calfcord calfkit-bridge` (the /<name> slash command and agent id changed)."
    )
    return 0


def delete_agent(agents_dir: Path, name: str) -> None:
    """Delete agent ``name``'s ``agents/<name>.md``.

    Reusable file-op (no prompts). The ``.md`` must exist.

    Raises:
        ValueError: ``agents_dir/<name>.md`` does not exist (nothing to delete).
        OSError: a filesystem error removing the file.
    """
    md_path = agents_dir / f"{name}.md"
    if not md_path.is_file():
        raise ValueError(f"no agent {name!r} in {agents_dir} (expected {md_path})")

    md_path.unlink()


def run_delete(
    prompter: Prompter,
    agents_dir: Path,
    name: str,
    *,
    yes: bool = False,
) -> int:
    """``calfcord agent delete <name>``: confirm, then delete the agent.

    A missing agent prints an ``error:`` line and returns 1. Unless ``yes`` is
    set, the operator must confirm via the injected :class:`Prompter` (default
    ``no``, so a stray enter cancels a destructive op); declining prints
    ``cancelled`` and returns 0 (a deliberate no-op, not an error). On
    confirmation it calls :func:`delete_agent`, reports the deletion, and tells
    the operator to restart. Returns 0 on success or cancel, 1 only when the
    agent doesn't exist.
    """
    md_path = agents_dir / f"{name}.md"
    if not md_path.is_file():
        print(f"error: no agent {name!r} in {agents_dir} (expected {md_path})")
        return 1

    if not yes and not prompter.confirm(
        f"Delete agent {name!r}? This removes {agents_dir}/{name}.md", default=False
    ):
        print("cancelled")
        return 0

    delete_agent(agents_dir, name)
    print(f"Deleted {name!r}. Restart `calfcord calfkit-agent` (and `calfcord calfkit-bridge`).")
    return 0
