"""Atomic mutation of frontmatter fields in an ``agents/<name>.md`` file.

The Discord ``/thinking-effort`` slash command and the ``calfcord agent
tools`` editor both persist their values into the agent's declarative
``.md`` file rather than a parallel state file, so ``agents/<name>.md`` is
the single source of truth for an agent's declared defaults. This module
owns that write — load the file with ``python-frontmatter``, mutate the
metadata, validate the mutated state **in memory**, dump to disk
atomically.

Every public mutator routes through the one :func:`_update_fields` path so
there is a single validate-before-write implementation: a per-field mutator
would be a place for the atomicity/validation invariant below to drift.
:func:`update_thinking_effort` and :func:`update_tools` differ only in the
``updates`` dict they pass (and the token pre-validation :func:`update_tools`
layers on top before delegating).

Validate-before-write
---------------------
Validation runs on a synthetic :class:`AgentDefinition` built from the
mutated metadata before any disk write. If validation fails the existing
file is untouched. This is what keeps the on-disk file and the
:class:`AgentRegistry`'s in-memory entry from diverging when an operator
has hand-edited the ``.md`` between boot and the slash invocation: either
both succeed or neither does. The function returns the validated
in-memory definition rather than re-parsing from disk, so a post-write
read error (transient OS, concurrent edit) can't break the invariant
either.

Atomicity on disk uses the same tmp-file + fsync + ``os.replace`` +
parent-dir fsync sequence as
:class:`calfcord.agents.state.AgentStateStore`. Mirrored here
rather than abstracted because a one-call-site abstraction would add
indirection without saving meaningful lines — extract a shared helper if
a third atomic-write call site appears.

Frontmatter round-trip caveat: ``python-frontmatter`` ultimately dumps
through PyYAML's ``safe_dump``, which alphabetizes keys (PyYAML defaults
to ``sort_keys=True`` and the library does not override it) and does not
preserve comments. Operators should avoid putting load-bearing comments
in agent frontmatter.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import yaml

from calfcord.agents.definition import AgentDefinition

if TYPE_CHECKING:
    from calfcord.agents.definition import ThinkingEffort

logger = logging.getLogger(__name__)


def _update_fields(md_path: Path, updates: dict[str, object]) -> AgentDefinition:
    """Apply ``updates`` to ``md_path``'s frontmatter, validating before write.

    The single validate-before-write path every mutator delegates to: load
    the file, overlay ``updates`` onto its metadata, build and validate a
    synthetic :class:`AgentDefinition` from the mutated metadata **in
    memory**, and only then atomically rewrite the file. A bad value raises
    before any disk write, so the on-disk file (and any registry copy a
    caller holds) is left untouched — the desync-prevention invariant the
    module docstring describes. The returned definition is the validated
    in-memory object, not a re-parse, so a transient post-write read error
    can't break that invariant either.

    ``updates`` carries already-coerced field values (e.g. an explicit
    ``list`` for ``tools``); semantic pre-validation that needs richer error
    messages than pydantic gives (the bad-token report in
    :func:`update_tools`) belongs in the caller, before delegating here.

    Raises:
        FileNotFoundError: ``md_path`` does not exist.
        ValueError: the existing ``.md`` is unparseable YAML or the mutated
            metadata fails :class:`AgentDefinition` validation. The on-disk
            file is unchanged.
        OSError: a filesystem error during the atomic write (e.g.
            permission denied, no space). The on-disk file is unchanged.
    """
    try:
        post = frontmatter.load(md_path)
    except yaml.YAMLError as e:
        raise ValueError(f"{md_path}: existing frontmatter is malformed YAML: {e}") from e

    post.metadata.update(updates)

    # Validate the mutated state in memory FIRST. parse_agent_md does an
    # equivalent construction; mirror it here so the disk write is gated
    # on a successful AgentDefinition build.
    candidate_metadata = dict(post.metadata)
    candidate_metadata["system_prompt"] = post.content.strip()
    candidate_metadata["source_path"] = md_path
    validated = AgentDefinition(**candidate_metadata)

    payload = frontmatter.dumps(post)
    # Ensure a trailing newline — frontmatter.dumps may omit it depending
    # on the body's own trailing whitespace, and POSIX text files
    # conventionally end with one.
    if not payload.endswith("\n"):
        payload += "\n"

    _atomic_write_text(md_path, payload)
    logger.info("rewrote %s fields=%s in %s", "/".join(updates), list(updates), md_path)

    # Return the validated in-memory definition rather than re-parsing
    # from disk: the disk content is byte-for-byte what produced
    # ``validated`` above, and a re-parse exception here would leave the
    # caller's registry copy stale relative to disk — the very desync
    # the validate-before-write design is meant to prevent.
    return validated


def update_thinking_effort(md_path: Path, value: ThinkingEffort) -> AgentDefinition:
    """Rewrite the ``thinking_effort`` frontmatter field in ``md_path``.

    Validates the post-mutation state in memory before touching disk so a
    validation failure leaves the on-disk file untouched and the caller's
    in-memory view is consistent with what's persisted.

    Returns the validated :class:`AgentDefinition` so callers can swap
    their cached copy without a second filesystem round-trip.

    Raises:
        FileNotFoundError: ``md_path`` does not exist.
        ValueError: the existing ``.md`` is unparseable YAML, fails
            :class:`AgentDefinition` validation, or the new value would
            produce an invalid definition. The on-disk file is unchanged.
        OSError: a filesystem error during the atomic write (e.g.
            permission denied, no space). The on-disk file is unchanged.
    """
    return _update_fields(md_path, {"thinking_effort": value})


def update_tools(md_path: Path, tools: Sequence[str]) -> AgentDefinition:
    """Rewrite the ``tools`` frontmatter list in ``md_path`` to ``tools``.

    Every token is validated *before* the shared write path runs, so an
    unknown builtin or a malformed ``mcp/`` selector raises a precise
    :class:`ValueError` (naming the offending token) with the on-disk file
    untouched, rather than surfacing as a generic pydantic error. The two
    token classes are checked against the same authorities the rest of the
    system uses, so the editor can never write a list the loader would later
    reject:

    * a *builtin* token must be a key of
      :data:`calfcord.tools.TOOL_REGISTRY`;
    * an *MCP selector* (``mcp/...`` —
      :func:`calfcord.mcp.selector.is_mcp_selector`) must be *syntactically*
      well-formed (:func:`~calfcord.mcp.selector.parse_mcp_selector`).
      Whether the referenced server/tool actually exists in the catalog is a
      deployment concern resolved later (and not knowable from a host with
      no MCP schemas), so it is deliberately not checked here — mirroring the
      syntax-only stance of the frontmatter validator.

    The writer always persists an *explicit* list (``tools: []`` when
    ``tools`` is empty), which is what lets the editor convert the implicit
    "tools omitted → all builtins" default into an unambiguous on-disk state
    on first save.

    The ``TOOL_REGISTRY`` / selector imports are deferred to here (rather
    than module scope) so :func:`update_thinking_effort`'s path stays light:
    importing ``TOOL_REGISTRY`` eagerly walks every builtin tool module,
    which the thinking-effort slash command has no reason to pay for.

    Raises:
        FileNotFoundError: ``md_path`` does not exist.
        ValueError: an unknown builtin token, a malformed ``mcp/`` selector,
            or a post-mutation :class:`AgentDefinition` validation failure.
            The on-disk file is unchanged.
        OSError: a filesystem error during the atomic write. The on-disk
            file is unchanged.
    """
    from calfcord.mcp.selector import is_mcp_selector, parse_mcp_selector
    from calfcord.tools import TOOL_REGISTRY

    for token in tools:
        if is_mcp_selector(token):
            # Syntactic check only — catalog existence is a deployment
            # concern, deferred exactly as the frontmatter validator does.
            parse_mcp_selector(token)
            continue
        if token not in TOOL_REGISTRY:
            valid = ", ".join(sorted(TOOL_REGISTRY)) or "(none registered)"
            raise ValueError(
                f"unknown tool {token!r}; expected a builtin ({valid}) or an MCP "
                f"selector (mcp/<server> or mcp/<server>/<tool>)"
            )

    return _update_fields(md_path, {"tools": list(tools)})


def _atomic_write_text(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via tmp-file + fsync + atomic rename.

    Caller is expected to have verified ``path`` exists (and therefore
    its parent does) — no defensive ``mkdir`` here.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    if os.name == "posix":
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # The rename is already durable on most filesystems even if
            # we can't fsync the parent. Don't fail the caller's commit;
            # we just lose the strong-durability guarantee on power loss.
            logger.warning(
                "parent-dir fsync failed for %s; rename is committed but durability "
                "may be weaker on power loss",
                path,
                exc_info=True,
            )
