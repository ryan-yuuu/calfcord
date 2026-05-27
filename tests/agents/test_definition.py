"""Unit tests for AgentDefinition field validators and the parse_agent_md loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from calfkit_organization.agents.definition import AgentDefinition, parse_agent_md


def _make_definition(**overrides) -> AgentDefinition:
    defaults = dict(
        agent_id="scheduler",
        slash="/scheduler",
        display_name="Aksel (Scheduler)",
        description="Calendar mechanics.",
        system_prompt="Test scheduler.",
    )
    return AgentDefinition(**(defaults | overrides))


class TestAgentDefinitionValidators:
    def test_valid_construction(self) -> None:
        d = _make_definition()
        assert d.agent_id == "scheduler"
        assert d.slash == "/scheduler"

    def test_construct_via_name_alias(self) -> None:
        """YAML uses ``name:``; Pydantic alias should accept that key as well."""
        d = AgentDefinition(
            name="echo",
            slash="/echo",
            display_name="Echo",
            description="Echoes.",
            system_prompt="Echo body.",
        )
        assert d.agent_id == "echo"

    @pytest.mark.parametrize("bad_id", ["Scheduler", "sched uler", "x" * 33, "", "sched.uler"])
    def test_invalid_agent_id_rejected(self, bad_id: str) -> None:
        with pytest.raises(ValidationError, match="name"):
            _make_definition(agent_id=bad_id)

    @pytest.mark.parametrize("bad_slash", ["scheduler", "/Scheduler", "/x" * 20, "/", "/sched.uler"])
    def test_invalid_slash_rejected(self, bad_slash: str) -> None:
        with pytest.raises(ValidationError, match="slash"):
            _make_definition(slash=bad_slash)

    def test_display_name_clyde_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Clyde"):
            _make_definition(display_name="clyde")

    @pytest.mark.parametrize("bad_name", ["", "x" * 81])
    def test_display_name_length_rejected(self, bad_name: str) -> None:
        with pytest.raises(ValidationError, match="display_name"):
            _make_definition(display_name=bad_name)

    @pytest.mark.parametrize("bad_desc", ["", "x" * 101])
    def test_description_length_rejected(self, bad_desc: str) -> None:
        with pytest.raises(ValidationError, match="description"):
            _make_definition(description=bad_desc)

    @pytest.mark.parametrize("bad_body", ["", "   ", "\n\n"])
    def test_empty_system_prompt_rejected(self, bad_body: str) -> None:
        with pytest.raises(ValidationError, match="system_prompt"):
            _make_definition(system_prompt=bad_body)

    def test_tools_default_none_means_all(self) -> None:
        """Omitted ``tools:`` in frontmatter → field is ``None``, which the
        loader (and factory's tool resolver) expand to "every registered
        tool". An explicit empty list ``tools: []`` is the way to opt out
        of all tools — see :attr:`AgentDefinition.tools` for the semantics."""
        d = _make_definition()
        assert d.tools is None

    def test_tools_explicit_empty_stays_empty(self) -> None:
        """Explicit ``tools: []`` is preserved as ``()`` (not normalized to
        ``None``) so the opt-out-of-all-tools semantic survives parsing."""
        d = _make_definition(tools=[])
        assert d.tools == ()

    def test_tools_coerced_to_tuple(self) -> None:
        d = _make_definition(tools=["calendar", "email"])
        assert d.tools == ("calendar", "email")

    def test_provider_defaults_to_none(self) -> None:
        """Unset provider lets the factory's default win at build time."""
        d = _make_definition()
        assert d.provider is None

    @pytest.mark.parametrize("provider", ["anthropic", "openai"])
    def test_provider_accepts_supported_values(self, provider: str) -> None:
        d = _make_definition(provider=provider)
        assert d.provider == provider

    @pytest.mark.parametrize("bad_provider", ["cohere", "Anthropic", "gpt", ""])
    def test_provider_rejects_unsupported_values(self, bad_provider: str) -> None:
        with pytest.raises(ValidationError, match="provider"):
            _make_definition(provider=bad_provider)

    def test_model_dump_by_alias_uses_yaml_key(self) -> None:
        """Pin the YAML-facing contract: ``model_dump(by_alias=True)`` emits ``name``.

        Guards against an accidental future change to the alias setup that
        would silently break any ``calfkit-agent init``-style dump-to-YAML
        path.
        """
        d = _make_definition()
        dumped = d.model_dump(by_alias=True)
        assert dumped["name"] == "scheduler"
        assert "agent_id" not in dumped

    def test_thinking_effort_defaults_to_none(self) -> None:
        assert _make_definition().thinking_effort is None

    @pytest.mark.parametrize(
        "effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_thinking_effort_accepts_known_tiers(self, effort: str) -> None:
        d = _make_definition(thinking_effort=effort)
        assert d.thinking_effort == effort

    @pytest.mark.parametrize("bad_effort", ["ludicrous", "HIGH", "", "veryhigh"])
    def test_thinking_effort_rejects_unknown_values(self, bad_effort: str) -> None:
        with pytest.raises(ValidationError, match="thinking_effort"):
            _make_definition(thinking_effort=bad_effort)

    def test_source_path_excluded_from_model_dump(self) -> None:
        """``source_path`` is an in-memory annotation, not a YAML field."""
        d = _make_definition(source_path=Path("/tmp/scheduler.md"))
        assert d.source_path == Path("/tmp/scheduler.md")
        assert "source_path" not in d.model_dump()
        assert "source_path" not in d.model_dump(by_alias=True)

    @pytest.mark.parametrize(
        "typo", ["provder", "thiking_effort", "displayname", "extra_garbage"]
    )
    def test_unknown_frontmatter_keys_rejected(self, typo: str) -> None:
        """``extra="forbid"`` surfaces frontmatter typos at parse time.

        Before this guard, ``provder: openai`` would silently fall back
        to the project default ``anthropic`` provider — a bewildering
        debugging experience for operators.
        """
        with pytest.raises(ValidationError):
            _make_definition(**{typo: "value"})


class TestParseAgentMd:
    def _write_md(self, path: Path, body: str = "You are a scheduler.", **frontmatter_extra) -> None:
        fields = {
            "name": path.stem,
            "slash": f"/{path.stem}",
            "display_name": path.stem.title(),
            "description": f"Test {path.stem}.",
        }
        fields.update(frontmatter_extra)
        lines = ["---"]
        for k, v in fields.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append(body)
        path.write_text("\n".join(lines))

    def test_happy_path(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        self._write_md(path)
        d = parse_agent_md(path)
        assert d.agent_id == "scheduler"
        assert d.slash == "/scheduler"
        assert d.system_prompt == "You are a scheduler."

    def test_stamps_source_path(self, tmp_path: Path) -> None:
        """source_path lets the bridge rewrite the same file later."""
        path = tmp_path / "scheduler.md"
        self._write_md(path)
        d = parse_agent_md(path)
        assert d.source_path == path

    def test_parses_thinking_effort_frontmatter_field(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        self._write_md(path, thinking_effort="high")
        d = parse_agent_md(path)
        assert d.thinking_effort == "high"

    def test_filename_must_match_name(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        # frontmatter declares name=finance but filename is scheduler.md
        self._write_md(path, name="finance")
        with pytest.raises(ValueError, match="does not match filename stem"):
            parse_agent_md(path)

    def test_missing_frontmatter_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        path.write_text("Just a body, no frontmatter.\n")
        with pytest.raises(ValueError, match="missing YAML frontmatter"):
            parse_agent_md(path)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        path.write_text(
            "---\n"
            "name: scheduler\n"
            "slash: /scheduler\n"
            # missing display_name + description
            "---\n"
            "Body.\n"
        )
        with pytest.raises(ValidationError):
            parse_agent_md(path)

    def test_empty_body_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        path.write_text(
            "---\n"
            "name: scheduler\n"
            "slash: /scheduler\n"
            "display_name: Scheduler\n"
            "description: Test.\n"
            "---\n"
        )
        with pytest.raises(ValidationError, match="system_prompt"):
            parse_agent_md(path)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_agent_md(tmp_path / "nope.md")
