"""The editable-field registry: one source of truth for ``calfcord agent`` edits.

Three surfaces onto an agent's ``.md`` — the interactive ``edit``
menu (which rows exist, in what order, showing each field's current value), the
non-interactive ``set`` command (which ``--flag`` writes which field), and
``show`` (how each field renders) — must agree on the *same* set of editable
fields or they drift: a flag with no menu row, a menu row ``show`` can't render,
a field one surface validates differently from another. This module is that
single source: :data:`FIELDS` is the ordered list every surface consumes, and
:func:`render_value` is the one renderer the menu and ``show`` share.

Two kinds of field live here:

* **Simple** (``text`` / ``select`` / ``int`` / ``bool``) — a scalar frontmatter
  value written through the one validated-atomic seam,
  :func:`calfcord.agents.md_writer._update_fields`, via :func:`write_simple_field`.
  Validation is delegated to :class:`~calfcord.agents.definition.AgentDefinition`
  (a bad ``thinking_effort`` or an out-of-range ``history_turns`` raises there),
  so this module never re-encodes a constraint pydantic already owns — keeping
  the two from drifting.
* **Compound** (``provider_model`` / ``tools`` / ``prompt``) — fields with
  dedicated editors (the provider+model pair shares the validated
  provider flow; ``tools`` reuses the checkbox editor; ``prompt`` rewrites the
  body via :func:`calfcord.agents.md_writer.update_system_prompt`). These are
  *not* written through :func:`write_simple_field`; they appear in :data:`FIELDS`
  only so the menu and ``show`` can list and render them.

The module imports only :mod:`calfcord.agents` seams — no provider SDK — so it
stays importable from the lightweight CLI entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from calfcord.agents import md_writer
from calfcord.agents.definition import ThinkingEffort

if TYPE_CHECKING:
    from pathlib import Path

    from calfcord.agents.definition import AgentDefinition

# The closed set of field kinds. A ``Literal`` (not a bare ``str``) so a typo in a
# ``Field(...)`` literal — e.g. ``"boool"`` — is a type error a checker can flag,
# rather than a field with no matching dispatch branch that fails only when an
# operator happens to select it.
FieldKind = Literal["text", "select", "int", "bool", "provider_model", "tools", "prompt"]

THINKING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
"""The :data:`~calfcord.agents.definition.ThinkingEffort` tiers, as a tuple for
the ``thinking_effort`` field's ``choices``. Mirrors the ``Literal`` in
:mod:`calfcord.agents.definition`; kept here as the menu/flag choice list so a
``select`` row and the pydantic ``Literal`` enumerate the same values."""

# This tuple is hand-authored but must enumerate exactly the ``ThinkingEffort``
# Literal it mirrors; assert it at import so a drift (a tier added to one and not
# the other) becomes a hard failure the test suite catches, not a silently wrong
# menu/validator.
assert get_args(ThinkingEffort) == THINKING_EFFORTS, "THINKING_EFFORTS drifted from ThinkingEffort"

# The longest single-line preview shown for the system-prompt body in the edit
# menu / ``show``. Long enough to be recognizable, short enough not to wrap a
# typical terminal row.
_PROMPT_PREVIEW_LEN = 60

# How many tool tokens to spell out before collapsing the rest into a "(+N)"
# count, so an agent with the full builtin set renders as a short, scannable
# label rather than a wrapping comma list.
_TOOLS_PREVIEW_COUNT = 2


@dataclass(frozen=True)
class Field:
    """One editable field, shared by the ``edit`` menu, ``set`` flags, and ``show``.

    ``key`` is the :class:`~calfcord.agents.definition.AgentDefinition` attribute
    (and frontmatter key) the field maps to — except the special value
    ``"system_prompt"``, which is the Markdown *body*, not a metadata key.
    ``kind`` drives both how the value is edited and how :func:`render_value`
    formats it. ``flag`` is the ``agent set`` long option that writes this field
    non-interactively. ``choices`` enumerates the allowed values for a
    ``select`` field; ``int_min`` / ``int_max`` bound an ``int`` field (mirroring
    the pydantic constraint so the menu can show the range without importing the
    model's internals).
    """

    key: str
    label: str
    kind: FieldKind
    flag: str
    choices: tuple[str, ...] | None = None
    int_min: int | None = None
    int_max: int | None = None

    def __post_init__(self) -> None:
        """Assert the per-kind shape so a malformed registry fails loudly at import.

        ``FIELDS`` is hand-authored; a ``select`` row with no ``choices`` would
        otherwise degrade to a silently empty menu (``field.choices or ()`` in the
        editor), and stray ``int_min``/``int_max`` on a non-``int`` field would be
        dead state. Catch both here — at module import — rather than at edit time.
        """
        if self.kind == "select":
            assert self.choices, f"{self.key!r}: a select field needs choices"
        else:
            assert self.choices is None, f"{self.key!r}: only a select field takes choices"
        if self.kind == "int":
            assert self.int_min is not None and self.int_max is not None, (
                f"{self.key!r}: an int field needs int_min and int_max"
            )
        else:
            assert self.int_min is None and self.int_max is None, (
                f"{self.key!r}: only an int field takes int_min/int_max"
            )


# Menu order. ``provider_model`` is ONE row spanning provider+model (the
# provider editor writes both together through the validated provider flow);
# ``tools`` and ``system_prompt`` have dedicated editors. The ordering puts the
# fields an operator most often tweaks (identity, then provider, then tools and
# prompt) before the runtime-tuning knobs (effort, history, memory, avatar).
FIELDS: list[Field] = [
    Field("description", "Description", "text", "--description"),
    Field("display_name", "Display name", "text", "--display-name"),
    Field("provider_model", "Provider / model", "provider_model", "--model"),
    Field("tools", "Tools", "tools", "--tools"),
    Field("system_prompt", "System prompt", "prompt", "--system-prompt"),
    Field(
        "thinking_effort",
        "Thinking effort",
        "select",
        "--thinking-effort",
        choices=THINKING_EFFORTS,
    ),
    Field("history_turns", "History turns", "int", "--history-turns", int_min=0, int_max=100),
    Field("memory", "Memory", "bool", "--memory"),
    Field("avatar_url", "Avatar URL", "text", "--avatar-url"),
]

# Fast lookup by key for the ``set`` command (resolve a ``--flag`` to its field)
# and any caller that needs one field by name. Built from FIELDS so it can't
# drift from the ordered list.
FIELDS_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}


def render_value(defn: AgentDefinition, field: Field) -> str:
    """Human-readable CURRENT value of ``field`` on ``defn``, for the menu and ``show``.

    One renderer so the edit menu's "current value" column and the ``show``
    command can never format the same field differently. Compound fields get a
    compact preview (``tools`` collapses the tail into ``(+N)``;
    ``provider_model`` joins the pair; ``system_prompt`` previews the first line);
    an unset optional field renders as ``(default)`` or ``(none)`` rather than a
    bare ``None`` so the menu reads cleanly.
    """
    if field.kind == "provider_model":
        provider = defn.provider or "(default)"
        model = defn.model or "(default)"
        return f"{provider} · {model}"

    if field.kind == "tools":
        return _render_tools(defn.tools)

    if field.kind == "prompt":
        return _render_prompt(defn.system_prompt)

    value = getattr(defn, field.key)

    if field.kind == "bool":
        return "on" if value else "off"

    if value is None or value == "":
        # ``thinking_effort`` unset means the provider default applies; an unset
        # avatar_url means the webhook default. Both read better as "(default)"
        # than an empty cell.
        return "(default)"

    return str(value)


def _render_tools(tools: tuple[str, ...] | None) -> str:
    """Render a tool tuple as a short label: first few names, then ``(+N)``.

    ``None`` (frontmatter ``tools:`` omitted) means "all builtins" — shown as
    ``(all builtins)`` rather than an empty cell, matching the loader's
    omitted→all semantics. An explicit empty tuple is the deliberate "no tools"
    opt-out, shown as ``(none)``.
    """
    if tools is None:
        return "(all builtins)"
    if not tools:
        return "(none)"
    head = ", ".join(tools[:_TOOLS_PREVIEW_COUNT])
    extra = len(tools) - _TOOLS_PREVIEW_COUNT
    return f"{head} (+{extra})" if extra > 0 else head


def _render_prompt(system_prompt: str) -> str:
    """Render the body as a single-line preview, truncated with an ellipsis."""
    return truncate(system_prompt, _PROMPT_PREVIEW_LEN)


def truncate(text: str, limit: int) -> str:
    """Flatten internal whitespace and clip ``text`` to ``limit`` chars with an ellipsis.

    Collapsing newlines/runs of whitespace to single spaces keeps a multi-line
    value on one row (a menu line, or a ``list`` table cell); the trailing ``…``
    signals the value was clipped so the reader knows to use ``show`` for the rest.
    Shared by the edit-menu/``show`` prompt preview here and the ``list`` table in
    :mod:`calfcord.cli.agent_inspect` so the two can't clip differently.
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def write_simple_field(md_path: Path, field: Field, raw: str) -> AgentDefinition:
    """Validate and write a SIMPLE field's new value through the one shared seam.

    Both the ``edit`` menu (after prompting) and the ``set`` command (from a
    ``--flag`` argument) call this for ``text`` / ``select`` / ``int`` / ``bool``
    fields, so the coercion and the validate-before-write path are defined once.
    The value is coerced to the frontmatter type the field expects and handed to
    :func:`calfcord.agents.md_writer._update_fields`, which builds and validates a
    synthetic :class:`AgentDefinition` before any disk write — so an out-of-range
    ``history_turns`` or a bad ``thinking_effort`` raises there, with the on-disk
    file untouched. Coercion is intentionally thin: only the int parse lives here
    (pydantic can't turn ``"abc"`` into the ``ValueError`` the caller wants);
    every domain constraint (range, allowed choices) stays owned by
    :class:`AgentDefinition` so this helper can't drift from it.

    Compound fields (``provider_model`` / ``tools`` / ``prompt``) have dedicated
    editors and must NOT be routed here.

    Raises:
        ValueError: ``field`` is not a simple field, an ``int`` value isn't
            numeric, or :func:`~calfcord.agents.md_writer._update_fields` rejects
            the coerced value (bad choice, out-of-range int, …). The on-disk file
            is unchanged.
        OSError: a filesystem error during the atomic write. The on-disk file is
            unchanged.
    """
    value = _coerce_simple(field, raw)
    return md_writer._update_fields(md_path, {field.key: value})


def _coerce_simple(field: Field, raw: str) -> object:
    """Coerce a ``set``-flag / prompt string to the field's frontmatter type.

    ``bool`` accepts the usual truthy/falsey spellings so ``--memory on`` and
    ``--memory false`` both work; ``int`` parses to ``int`` (a non-numeric value
    raises a precise :class:`ValueError` here rather than a confusing pydantic
    one); ``text`` / ``select`` pass through as the raw string and let
    :class:`~calfcord.agents.definition.AgentDefinition` enforce length / choice.
    """
    if field.kind == "bool":
        return _coerce_bool(field, raw)
    if field.kind == "int":
        try:
            return int(raw)
        except ValueError as e:
            raise ValueError(f"{field.flag} expects an integer, got {raw!r}") from e
    if field.kind in ("text", "select"):
        return raw
    raise ValueError(f"{field.key!r} (kind={field.kind!r}) is not a simple field; it has a dedicated editor")


def _coerce_bool(field: Field, raw: str) -> bool:
    """Parse a boolean spelling, raising a clear error on anything unrecognized."""
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{field.flag} expects a boolean (on/off, true/false, yes/no), got {raw!r}")
