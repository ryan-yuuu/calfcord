"""Unit tests for the editable-field registry shared by Round 2's edit/set/show.

The registry is the single source of truth for which agent ``.md`` fields are
editable, so these tests pin both halves of that contract: :func:`render_value`
(the one renderer the menu and ``show`` share) for every field kind, and
:func:`write_simple_field` (the one validated-atomic write the menu and ``set``
share) including the two failure modes the design must surface cleanly — an
out-of-range ``history_turns`` and a bad ``thinking_effort`` — both of which
must leave the on-disk file untouched.
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
    write_simple_field,
)


def _make_definition(**overrides: object) -> AgentDefinition:
    defaults = dict(
        agent_id="scribe",
        display_name="Scribe",
        description="Takes notes.",
        system_prompt="You are Scribe.",
    )
    return AgentDefinition(**(defaults | overrides))


def _seed_md(tmp_path: Path, **meta_overrides: object) -> Path:
    meta: dict[str, object] = {
        "name": "scribe",
        "display_name": "Scribe",
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
    on = _make_definition(memory=True, tools=("read_file", "write_file"))
    off = _make_definition(memory=False)
    assert render_value(on, FIELDS_BY_KEY["memory"]) == "on"
    assert render_value(off, FIELDS_BY_KEY["memory"]) == "off"


def test_render_int_history_turns() -> None:
    defn = _make_definition(history_turns=12)
    assert render_value(defn, FIELDS_BY_KEY["history_turns"]) == "12"


def test_render_thinking_effort_unset_is_default() -> None:
    defn = _make_definition()  # thinking_effort None
    assert render_value(defn, FIELDS_BY_KEY["thinking_effort"]) == "(default)"


def test_render_thinking_effort_set() -> None:
    defn = _make_definition(thinking_effort="high")
    assert render_value(defn, FIELDS_BY_KEY["thinking_effort"]) == "high"


def test_render_avatar_url_unset_is_default() -> None:
    defn = _make_definition()  # avatar_url None
    assert render_value(defn, FIELDS_BY_KEY["avatar_url"]) == "(default)"


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


def test_write_int_field(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = write_simple_field(md_path, FIELDS_BY_KEY["history_turns"], "50")
    assert updated.history_turns == 50
    assert parse_agent_md(md_path).history_turns == 50


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


def test_write_int_out_of_range_raises_and_leaves_file(tmp_path: Path) -> None:
    """An out-of-range history_turns must fail at the AgentDefinition validator,
    with the on-disk file untouched (the validate-before-write guarantee)."""
    md_path = _seed_md(tmp_path)
    original = md_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        write_simple_field(md_path, FIELDS_BY_KEY["history_turns"], "101")

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_int_non_numeric_raises_cleanly(tmp_path: Path) -> None:
    """A non-numeric int value raises a precise ValueError, not a pydantic one."""
    md_path = _seed_md(tmp_path)
    original = md_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="expects an integer"):
        write_simple_field(md_path, FIELDS_BY_KEY["history_turns"], "lots")

    assert md_path.read_text(encoding="utf-8") == original


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
