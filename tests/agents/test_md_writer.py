"""Unit tests for the .md frontmatter atomic writer."""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from calfcord.agents.md_writer import (
    _update_fields,
    update_system_prompt,
    update_thinking_effort,
    update_tools,
)


def _seed_md(
    path: Path,
    *,
    agent_id: str = "scribe",
    provider: str = "openai",
    thinking_effort: str | None = None,
    body: str = "Hello body.",
) -> Path:
    meta: dict[str, str] = {
        "name": agent_id,
        "display_name": agent_id.capitalize(),
        "description": f"Test {agent_id}.",
        "provider": provider,
    }
    if thinking_effort is not None:
        meta["thinking_effort"] = thinking_effort
    post = frontmatter.Post(body, **meta)
    md_path = path / f"{agent_id}.md"
    md_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return md_path


def test_inserts_thinking_effort_when_absent(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = update_thinking_effort(md_path, "high")
    assert updated.thinking_effort == "high"

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["thinking_effort"] == "high"


def test_overwrites_existing_thinking_effort(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    updated = update_thinking_effort(md_path, "max")
    assert updated.thinking_effort == "max"

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["thinking_effort"] == "max"


def test_preserves_other_frontmatter_fields(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, agent_id="scheduler", provider="anthropic")
    update_thinking_effort(md_path, "medium")

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["name"] == "scheduler"
    assert reloaded.metadata["provider"] == "anthropic"


def test_preserves_body_content(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, body="You are a helpful agent.\n\nBe concise.")
    update_thinking_effort(md_path, "high")

    reloaded = frontmatter.load(md_path)
    assert reloaded.content.strip() == "You are a helpful agent.\n\nBe concise."


def test_returns_parsed_definition_with_source_path(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = update_thinking_effort(md_path, "xhigh")
    assert updated.agent_id == "scribe"
    assert updated.thinking_effort == "xhigh"
    assert updated.source_path == md_path


def test_atomic_no_tmp_files_left_behind(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    update_thinking_effort(md_path, "high")
    tmp_files = list(tmp_path.glob(".*.tmp"))
    assert tmp_files == []


def test_missing_md_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        update_thinking_effort(tmp_path / "ghost.md", "high")


def test_round_trip_through_parse(tmp_path: Path) -> None:
    """A round-trip must produce a re-parsable .md (no corruption)."""
    from calfcord.agents.definition import parse_agent_md

    md_path = _seed_md(tmp_path)
    update_thinking_effort(md_path, "high")
    re_parsed = parse_agent_md(md_path)
    assert re_parsed.thinking_effort == "high"
    assert re_parsed.agent_id == "scribe"


def test_atomic_write_failure_leaves_original_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash during the atomic rename must leave the original file unchanged
    and clean up the .tmp sibling — exercises the ``except: tmp_path.unlink``
    branch in ``_atomic_write_text``.
    """
    import os

    md_path = _seed_md(tmp_path, thinking_effort="low")
    original_payload = md_path.read_text(encoding="utf-8")

    def _raise_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", _raise_replace)

    with pytest.raises(OSError):
        update_thinking_effort(md_path, "high")

    # Original file content unchanged.
    assert md_path.read_text(encoding="utf-8") == original_payload
    # No leftover .tmp files.
    assert list(tmp_path.glob(".*.tmp")) == []


def test_malformed_existing_frontmatter_raises_valueerror(
    tmp_path: Path,
) -> None:
    """An unparseable existing .md surfaces as ValueError with the path —
    not a raw yaml.YAMLError."""
    md_path = tmp_path / "scribe.md"
    md_path.write_text(
        "---\nname: scribe\n  invalid: : : yaml\n---\nbody\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="malformed YAML"):
        update_thinking_effort(md_path, "high")


def test_validation_failure_does_not_touch_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the in-memory validation fails, the on-disk file must be unchanged.

    Forces a failure by monkeypatching ``AgentDefinition`` construction in the
    md_writer module to raise.
    """
    from calfcord.agents import md_writer

    md_path = _seed_md(tmp_path, thinking_effort="low")
    original = md_path.read_text(encoding="utf-8")

    class _Boom:
        def __init__(self, **_kwargs: object) -> None:
            raise ValueError("simulated validation failure")

    monkeypatch.setattr(md_writer, "AgentDefinition", _Boom)

    with pytest.raises(ValueError, match="simulated validation"):
        update_thinking_effort(md_path, "high")

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


# --------------------------------------------------------------- _update_fields ---


def test_update_fields_is_the_shared_write_path(tmp_path: Path) -> None:
    """``_update_fields`` is the generic mutator both wrappers delegate to.

    Driving it directly (with the same dict ``update_thinking_effort`` builds)
    must produce an identical result, proving the wrapper adds nothing beyond
    the dict it passes.
    """
    md_path = _seed_md(tmp_path)
    via_generic = _update_fields(md_path, {"thinking_effort": "high"})
    assert via_generic.thinking_effort == "high"
    assert frontmatter.load(md_path).metadata["thinking_effort"] == "high"


def test_update_thinking_effort_still_works_via_shared_path(tmp_path: Path) -> None:
    """The thinking-effort wrapper is behaviour-preserved post-generalization."""
    md_path = _seed_md(tmp_path, thinking_effort="low")
    updated = update_thinking_effort(md_path, "max")
    assert updated.thinking_effort == "max"
    assert frontmatter.load(md_path).metadata["thinking_effort"] == "max"


# ------------------------------------------------------------ update_system_prompt ---


def test_update_system_prompt_rewrites_body(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, body="Old body.")
    updated = update_system_prompt(md_path, "You are Scribe. Be concise.")
    assert updated.system_prompt == "You are Scribe. Be concise."

    reloaded = frontmatter.load(md_path)
    assert reloaded.content.strip() == "You are Scribe. Be concise."


def test_update_system_prompt_preserves_frontmatter(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, agent_id="scheduler", provider="anthropic", thinking_effort="high")
    update_system_prompt(md_path, "Fresh prompt body.")

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["name"] == "scheduler"
    assert reloaded.metadata["provider"] == "anthropic"
    assert reloaded.metadata["thinking_effort"] == "high"


def test_update_system_prompt_is_reloadable_via_parse_agent_md(tmp_path: Path) -> None:
    from calfcord.agents.definition import parse_agent_md

    md_path = _seed_md(tmp_path)
    update_system_prompt(md_path, "Multi-line body.\n\nSecond paragraph.")
    re_parsed = parse_agent_md(md_path)
    assert re_parsed.agent_id == "scribe"
    assert re_parsed.system_prompt == "Multi-line body.\n\nSecond paragraph."


def test_update_system_prompt_empty_body_raises_and_leaves_file(tmp_path: Path) -> None:
    """An empty/whitespace-only body fails the pydantic system_prompt validator
    before any disk write, leaving the original file untouched."""
    md_path = _seed_md(tmp_path, body="Original body.")
    original = md_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="system_prompt"):
        update_system_prompt(md_path, "   \n  ")

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_update_system_prompt_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        update_system_prompt(tmp_path / "ghost.md", "body")


def test_update_system_prompt_write_failure_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash during the atomic rename leaves the original unchanged and cleans
    up the .tmp sibling — same atomic guarantee the field mutators give."""
    import os

    md_path = _seed_md(tmp_path, body="Original body.")
    original = md_path.read_text(encoding="utf-8")

    def _raise_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", _raise_replace)

    with pytest.raises(OSError):
        update_system_prompt(md_path, "New body.")

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


# ------------------------------------------------------------------ update_tools ---


def test_update_tools_writes_explicit_list(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = update_tools(md_path, ["read_file", "write_file"])
    assert updated.tools == ("read_file", "write_file")

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["tools"] == ["read_file", "write_file"]


def test_update_tools_preserves_other_frontmatter_fields(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, agent_id="scheduler", provider="anthropic")
    update_tools(md_path, ["read_file"])

    reloaded = frontmatter.load(md_path)
    assert reloaded.metadata["name"] == "scheduler"
    assert reloaded.metadata["display_name"] == "Scheduler"
    assert reloaded.metadata["provider"] == "anthropic"


def test_update_tools_is_reloadable_via_parse_agent_md(tmp_path: Path) -> None:
    from calfcord.agents.definition import parse_agent_md

    md_path = _seed_md(tmp_path)
    update_tools(md_path, ["shell"])
    re_parsed = parse_agent_md(md_path)
    assert re_parsed.tools == ("shell",)


def test_update_tools_empty_writes_empty_list(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path)
    updated = update_tools(md_path, [])
    assert updated.tools == ()
    assert frontmatter.load(md_path).metadata["tools"] == []


def test_update_tools_accepts_well_formed_mcp_selector(tmp_path: Path) -> None:
    """A syntactically valid ``mcp/...`` selector passes even with no catalog.

    Catalog existence is a deployment concern the writer deliberately does not
    check — mirroring the frontmatter validator's syntax-only stance.
    """
    md_path = _seed_md(tmp_path)
    updated = update_tools(md_path, ["read_file", "mcp/gmail", "mcp/gmail/search"])
    assert updated.tools == ("read_file", "mcp/gmail", "mcp/gmail/search")


def test_update_tools_unknown_builtin_raises_and_leaves_file(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    original = md_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="unknown tool 'not_a_real_tool'"):
        update_tools(md_path, ["read_file", "not_a_real_tool"])

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_update_tools_malformed_mcp_selector_raises_and_leaves_file(tmp_path: Path) -> None:
    md_path = _seed_md(tmp_path, thinking_effort="low")
    original = md_path.read_text(encoding="utf-8")

    # ``mcp/a/b/c`` has too many segments — rejected by parse_mcp_selector.
    with pytest.raises(ValueError, match="mcp/a/b/c"):
        update_tools(md_path, ["mcp/a/b/c"])

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


def test_update_tools_non_string_token_raises_valueerror_and_leaves_file(tmp_path: Path) -> None:
    """A non-string token surfaces as ``ValueError`` (the documented seam), not
    an ``AttributeError`` from the selector check reaching for ``.startswith``.
    The on-disk file is untouched."""
    md_path = _seed_md(tmp_path, thinking_effort="low")
    original = md_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="invalid tool"):
        update_tools(md_path, [123])  # type: ignore[list-item]

    assert md_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.tmp")) == []


# --------------------------------------------------------------- mode preservation ---


def test_update_fields_preserves_existing_file_mode(tmp_path: Path) -> None:
    """A rewrite must keep the file's existing 0644 mode, not force 0600.

    ``mkstemp`` creates the temp file 0600 and ``os.replace`` adopts the temp
    file's mode, so without the capture-and-restore an operator's 0644 agent
    ``.md`` would silently become 0600 on every edit. The writer restores the
    original mode after the rename — assert that for both write paths.
    """
    import stat

    md_path = _seed_md(tmp_path)
    md_path.chmod(0o644)

    _update_fields(md_path, {"thinking_effort": "high"})
    assert stat.S_IMODE(md_path.stat().st_mode) == 0o644

    # The body-rewrite path shares ``_atomic_write_text`` — preserve mode there too.
    update_system_prompt(md_path, "Fresh body for the mode check.")
    assert stat.S_IMODE(md_path.stat().st_mode) == 0o644
