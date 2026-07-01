"""Unit tests for the editable-field registry shared by Round 2's edit/set/show.

The registry is the single source of truth for which agent ``.md`` fields are
editable, so these tests pin both halves of that contract: :func:`render_value`
(the one renderer the menu and ``show`` share) for every field kind, and
:func:`write_simple_field` (the one validated-atomic write the menu and ``set``
share) including the failure mode the design must surface cleanly — a bad
``thinking_effort`` — which must leave the on-disk file untouched.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from calfcord.agents.definition import AgentDefinition, parse_agent_md
from calfcord.cli._fields import (
    FIELDS,
    FIELDS_BY_KEY,
    THINKING_EFFORTS,
    Field,
    render_value,
    truncate,
    write_simple_field,
)


def _make_definition(**overrides: object) -> AgentDefinition:
    defaults = dict(
        agent_id="scribe",
        description="Takes notes.",
        system_prompt="You are Scribe.",
    )
    return AgentDefinition(**(defaults | overrides))


def _seed_md(tmp_path: Path, **meta_overrides: object) -> Path:
    meta: dict[str, object] = {
        "name": "scribe",
        "description": "Takes notes.",
        "provider": "anthropic",
    }
    meta.update(meta_overrides)
    post = frontmatter.Post("You are Scribe.", **meta)
    md_path = tmp_path / "scribe.md"
    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return md_path


# --- registry shape ---------------------------------------------------------


def test_fields_by_key_matches_fields() -> None:
    """The lookup map is derived from FIELDS, so it can't drift from the list."""
    assert {f.key: f for f in FIELDS} == FIELDS_BY_KEY
    assert len(FIELDS_BY_KEY) == len(FIELDS)  # keys are unique


def test_every_field_has_a_unique_flag() -> None:
    """``set`` resolves a ``--flag`` to a field, so flags must be distinct."""
    flags = [f.flag for f in FIELDS]
    assert len(set(flags)) == len(flags)


def test_thinking_effort_choices_track_the_literal() -> None:
    """The select choices must enumerate exactly the ThinkingEffort Literal."""
    from calfcord.agents.definition import ThinkingEffort

    assert set(THINKING_EFFORTS) == set(ThinkingEffort.__args__)
    assert FIELDS_BY_KEY["thinking_effort"].choices == THINKING_EFFORTS


def test_simple_kinds_carry_no_dedicated_editor_marker() -> None:
    """provider_model/tools/prompt are the compound kinds; the rest are simple."""
    compound = {f.kind for f in FIELDS if f.kind in ("provider_model", "tools", "prompt")}
    assert compound == {"provider_model", "tools", "prompt"}


# --- Field.__post_init__ shape guards ---------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        # A select field MUST carry choices.
        {"key": "k", "label": "L", "kind": "select", "flag": "--k", "choices": None},
        # A non-select field must NOT carry choices.
        {"key": "k", "label": "L", "kind": "text", "flag": "--k", "choices": ("a",)},
    ],
)
def test_field_post_init_rejects_malformed_entry(kwargs: dict[str, object]) -> None:
    """A registry row whose shape doesn't match its kind fails loudly at construction."""
    with pytest.raises(AssertionError):
        Field(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        # Well-formed select (choices present).
        {"key": "k", "label": "L", "kind": "select", "flag": "--k", "choices": ("a", "b")},
        # Well-formed simple text (no choices).
        {"key": "k", "label": "L", "kind": "text", "flag": "--k"},
        # Well-formed compound kind (no choices).
        {"key": "k", "label": "L", "kind": "bool", "flag": "--k"},
    ],
)
def test_field_post_init_accepts_well_formed_entry(kwargs: dict[str, object]) -> None:
    """A correctly-shaped row for each kind constructs without error."""
    assert Field(**kwargs).kind == kwargs["kind"]


# --- truncate ---------------------------------------------------------------


def test_truncate_short_string_flattens_and_keeps_unclipped() -> None:
    """A sub-limit value collapses internal whitespace/newlines to single spaces
    and is returned whole (no ellipsis)."""
    result = truncate("hi\n\n  there   world", 60)
    assert result == "hi there world"
    assert not result.endswith("…")


def test_truncate_long_string_is_clipped_to_limit_with_ellipsis() -> None:
    """An over-limit value (no boundary whitespace) clips to exactly ``limit`` chars,
    the last being the ellipsis."""
    result = truncate("abcdefghij" * 5, 10)  # 50 chars, no spaces
    assert len(result) == 10
    assert result.endswith("…")


def test_truncate_exactly_at_limit_is_unclipped() -> None:
    """A value whose flattened length equals ``limit`` is returned verbatim."""
    result = truncate("a" * 10, 10)
    assert result == "a" * 10
    assert not result.endswith("…")


# --- render_value -----------------------------------------------------------


def test_render_text_field() -> None:
    defn = _make_definition(description="Calendar mechanics.")
    assert render_value(defn, FIELDS_BY_KEY["description"]) == "Calendar mechanics."


def test_render_provider_model() -> None:
    defn = _make_definition(provider="anthropic", model="claude-sonnet-4-5")
    assert render_value(defn, FIELDS_BY_KEY["provider_model"]) == "anthropic · claude-sonnet-4-5"


def test_render_provider_model_unset_shows_default() -> None:
    defn = _make_definition()  # provider/model both None
    assert render_value(defn, FIELDS_BY_KEY["provider_model"]) == "(default) · (default)"


def test_render_tools_collapses_tail() -> None:
    defn = _make_definition(tools=("read_file", "web_search", "shell", "glob"))
    # First two spelled out, the remaining two collapsed into a count.
    assert render_value(defn, FIELDS_BY_KEY["tools"]) == "read_file, web_search (+2)"


def test_render_tools_short_list_no_count() -> None:
    defn = _make_definition(tools=("read_file", "web_search"))
    assert render_value(defn, FIELDS_BY_KEY["tools"]) == "read_file, web_search"


def test_render_tools_omitted_is_all_builtins() -> None:
    defn = _make_definition(tools=None)
    assert render_value(defn, FIELDS_BY_KEY["tools"]) == "(all builtins)"


def test_render_tools_explicit_empty_is_none() -> None:
    defn = _make_definition(tools=())
    assert render_value(defn, FIELDS_BY_KEY["tools"]) == "(none)"


def test_render_prompt_single_line_preview() -> None:
    defn = _make_definition(system_prompt="You are Scribe.\n\nBe concise and accurate.")
    rendered = render_value(defn, FIELDS_BY_KEY["system_prompt"])
    # Newlines collapsed to a single-line preview.
    assert "\n" not in rendered
    assert rendered.startswith("You are Scribe. Be concise")


def test_render_prompt_truncates_long_body() -> None:
    body = "word " * 100
    defn = _make_definition(system_prompt=body)
    rendered = render_value(defn, FIELDS_BY_KEY["system_prompt"])
    assert rendered.endswith("…")
    assert len(rendered) <= 60


def test_render_bool_on_off() -> None:
    # ``render_value`` keys off ``memory`` alone; no tools coupling is implied.
    on = _make_definition(memory=True)
    off = _make_definition(memory=False)
    assert render_value(on, FIELDS_BY_KEY["memory"]) == "on"
    assert render_value(off, FIELDS_BY_KEY["memory"]) == "off"


def test_render_thinking_effort_unset_is_default() -> None:
    defn = _make_definition()  # thinking_effort None
    assert render_value(defn, FIELDS_BY_KEY["thinking_effort"]) == "(default)"


def test_render_thinking_effort_set() -> None:
    defn = _make_definition(thinking_effort="high")
    assert render_value(defn, FIELDS_BY_KEY["thinking_effort"]) == "high"


# --- write_simple_field -----------------------------------------------------


def test_write_text_field(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = write_simple_field(md_path, FIELDS_BY_KEY["description"], "New notes desc.")
    assert updated.description == "New notes desc."
    assert parse_agent_md(md_path).description == "New notes desc."


def test_write_select_field(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = write_simple_field(md_path, FIELDS_BY_KEY["thinking_effort"], "xhigh")
    assert updated.thinking_effort == "xhigh"
    assert parse_agent_md(md_path).thinking_effort == "xhigh"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("on", True), ("true", True), ("yes", True), ("1", True), ("off", False), ("false", False), ("no", False)],
)
def test_write_bool_field(tmp_path: Path, raw: str, expected: bool) -> None:
    # memory=True requires read_file + write_file at factory time, but the
    # md_writer validation is the AgentDefinition only (no factory), so a bare
    # memory flip validates fine on its own.
    md_path = _seed_md(tmp_path)
    updated = write_simple_field(md_path, FIELDS_BY_KEY["memory"], raw)
    assert updated.memory is expected


def test_write_bad_thinking_effort_raises_and_leaves_file(tmp_path: Path) -> None:
    """A value outside the ThinkingEffort Literal fails validation cleanly."""
    md_path = _seed_md(tmp_path)
    original = md_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        write_simple_field(md_path, FIELDS_BY_KEY["thinking_effort"], "ludicrous")

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_bad_bool_raises_cleanly(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    with pytest.raises(ValueError, match="expects a boolean"):
        write_simple_field(md_path, FIELDS_BY_KEY["memory"], "maybe")


def test_write_compound_field_is_rejected(tmp_path: Path) -> None:
    """Compound fields have dedicated editors; routing one here is a programming
    error and must raise rather than silently mis-write."""
    md_path = _seed_md(tmp_path)
    tools_field = Field("tools", "Tools", "tools", "--tools")
    with pytest.raises(ValueError, match="dedicated editor"):
        write_simple_field(md_path, tools_field, "read_file")
