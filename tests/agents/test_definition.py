"""Unit tests for AgentDefinition field validators and the parse_agent_md loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from calfcord.agents.definition import AgentDefinition, parse_agent_md


def _make_definition(**overrides) -> AgentDefinition:
    defaults = dict(
        agent_id="scheduler",
        description="Calendar mechanics.",
        system_prompt="Test scheduler.",
    )
    return AgentDefinition(**(defaults | overrides))


class TestAgentDefinitionValidators:
    def test_valid_construction(self) -> None:
        d = _make_definition()
        assert d.agent_id == "scheduler"

    def test_construct_via_name_alias(self) -> None:
        """YAML uses ``name:``; Pydantic alias should accept that key as well."""
        d = AgentDefinition(
            name="echo",
            description="Echoes.",
            system_prompt="Echo body.",
        )
        assert d.agent_id == "echo"

    @pytest.mark.parametrize("bad_id", ["Scheduler", "sched uler", "x" * 33, "", "sched.uler"])
    def test_invalid_agent_id_rejected(self, bad_id: str) -> None:
        with pytest.raises(ValidationError, match="name"):
            _make_definition(agent_id=bad_id)

    def test_stale_slash_frontmatter_rejected(self) -> None:
        """The ``slash`` field was removed; stale ``slash:`` frontmatter must fail loudly.

        ``extra="forbid"`` is the load-time guard that catches operators
        who haven't yet stripped ``slash: /foo`` from their ``.md`` files
        after the field was dropped — silent acceptance would let the
        stale value rot in source without warning.
        """
        with pytest.raises(ValidationError):
            _make_definition(slash="/scheduler")

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

    @pytest.mark.parametrize(
        "selector",
        ["mcp/gmail", "mcp/gmail/search", "mcp/demo/get-x", "mcp/srv_2"],
    )
    def test_valid_mcp_selectors_accepted(self, selector: str) -> None:
        """Well-formed ``mcp/...`` selectors pass the syntactic field check —
        whether the server is configured/running is a runtime concern (the
        capability view), not a parse-time one."""
        d = _make_definition(tools=[selector])
        assert d.tools == (selector,)

    def test_mixed_builtins_and_mcp_selectors_accepted(self) -> None:
        """One flat ``tools:`` list carries both bare builtin names and
        ``mcp/...`` selectors — both kinds coexist on the same agent."""
        d = _make_definition(tools=["shell", "mcp/gmail", "mcp/calendar/list-events"])
        assert d.tools == ("shell", "mcp/gmail", "mcp/calendar/list-events")

    @pytest.mark.parametrize(
        "bad_selector",
        ["mcp/", "mcp/a/b/c", "mcp//x", "mcp/Gmail", "mcp/gmail/"],
    )
    def test_malformed_mcp_selectors_rejected(self, bad_selector: str) -> None:
        with pytest.raises(ValidationError, match="malformed MCP tool selector"):
            _make_definition(tools=[bad_selector])

    def test_multiple_malformed_selectors_aggregated(self) -> None:
        """All malformed selectors surface in ONE error so an operator fixes
        the whole ``tools:`` line in a single pass."""
        with pytest.raises(ValidationError) as exc_info:
            _make_definition(tools=["mcp/", "mcp/a/b/c"])
        msg = str(exc_info.value)
        assert "'mcp/'" in msg
        assert "'mcp/a/b/c'" in msg

    def test_arbitrary_bare_names_still_accepted(self) -> None:
        """Bare (non-``mcp/``) names pass through untouched — existence is
        validated later by the factory, not here. This guards the
        ``["calendar", "email"]`` case that
        :meth:`test_tools_coerced_to_tuple` relies on."""
        d = _make_definition(tools=["calendar", "totally_made_up_tool"])
        assert d.tools == ("calendar", "totally_made_up_tool")

    def test_publish_topic_rejected(self) -> None:
        """``publish_topic`` (the router that used it is gone) is no longer a
        declared field, so ``extra="forbid"`` rejects a stale value as an unknown
        field — a stale setting fails loudly instead of silently doing nothing."""
        with pytest.raises(ValidationError, match="publish_topic"):
            _make_definition(publish_topic="routing.decisions")

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

    def test_memory_defaults_to_false(self) -> None:
        assert _make_definition().memory is False

    def test_memory_accepts_true(self) -> None:
        assert _make_definition(memory=True).memory is True

    def test_thinking_effort_defaults_to_none(self) -> None:
        assert _make_definition().thinking_effort is None

    @pytest.mark.parametrize(
        "effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_thinking_effort_accepts_known_tiers(self, effort: str) -> None:
        d = _make_definition(thinking_effort=effort)
        assert d.thinking_effort == effort

    def test_a2a_defaults_to_true(self) -> None:
        """Native A2A (message_agent) is on by default — every agent can reach
        any peer unless it opts out (`a2a: false`) or restricts (`a2a: [names]`)."""
        assert _make_definition().a2a is True

    def test_a2a_accepts_false(self) -> None:
        assert _make_definition(a2a=False).a2a is False

    def test_a2a_accepts_peer_list_coerced_to_tuple(self) -> None:
        assert _make_definition(a2a=["scribe", "conan"]).a2a == ("scribe", "conan")

    def test_handoff_defaults_to_true(self) -> None:
        """Native handoff is on by default (replaces the in-channel @<agent>
        convention); opt out with `handoff: false` or restrict with a list."""
        assert _make_definition().handoff is True

    def test_handoff_accepts_false(self) -> None:
        assert _make_definition(handoff=False).handoff is False

    def test_handoff_accepts_peer_list_coerced_to_tuple(self) -> None:
        assert _make_definition(handoff=["scribe"]).handoff == ("scribe",)

    def test_a2a_empty_list_normalizes_to_false(self) -> None:
        """`a2a: []` ("no peers") is unambiguously "capability off": normalize the
        empty tuple to False so it can't reach the factory and build a bare
        Messaging() (which calfkit rejects, crashing agent boot)."""
        assert _make_definition(a2a=[]).a2a is False

    def test_handoff_empty_list_normalizes_to_false(self) -> None:
        assert _make_definition(handoff=[]).handoff is False

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
        "typo",
        [
            "provder",
            "thiking_effort",
            "extra_garbage",
            # The four fields dropped in the 0.12 migration are now unknown keys,
            # so ``extra="forbid"`` rejects any ``.md`` that still carries them
            # (the on-disk migration signal).
            "display_name",
            "avatar_url",
            "history_turns",
            "role",
        ],
    )
    def test_unknown_frontmatter_keys_rejected(self, typo: str) -> None:
        """``extra="forbid"`` surfaces frontmatter typos at parse time.

        Before this guard, ``provder: openai`` would silently fall back
        to the project default ``anthropic`` provider — a bewildering
        debugging experience for operators. It also rejects the fields
        dropped in the 0.12 migration (``display_name``/``avatar_url``/
        ``history_turns``/``role``) so a stale ``.md`` fails loudly.
        """
        with pytest.raises(ValidationError):
            _make_definition(**{typo: "value"})


class TestParseAgentMd:
    def _write_md(self, path: Path, body: str = "You are a scheduler.", **frontmatter_extra) -> None:
        fields = {
            "name": path.stem,
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

    def test_parses_memory_frontmatter_field(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        self._write_md(path, memory="true")
        d = parse_agent_md(path)
        assert d.memory is True

    def test_filename_must_match_name(self, tmp_path: Path) -> None:
        path = tmp_path / "scheduler.md"
        # frontmatter declares name=finance but filename is scheduler.md
        self._write_md(path, name="finance")
        with pytest.raises(ValueError, match="does not match filename stem"):
            parse_agent_md(path)

    def test_mcp_tool_in_frontmatter_loads(self, tmp_path: Path) -> None:
        """An on-disk ``.md`` whose ``tools:`` names an ``mcp/...`` entry loads
        on the real read path (the same one the agent process and the bridge
        registry use) — selectors are first-class tool declarations again."""
        path = tmp_path / "scheduler.md"
        self._write_md(path, tools="[shell, mcp/gmail]")
        definition = parse_agent_md(path)
        assert definition.tools == ("shell", "mcp/gmail")

    def test_malformed_mcp_tool_in_frontmatter_rejected(self, tmp_path: Path) -> None:
        """A malformed selector fails the load with the entry named, so a
        frontmatter typo never reaches a running worker."""
        path = tmp_path / "scheduler.md"
        self._write_md(path, tools="[shell, mcp/a/b/c]")
        with pytest.raises(ValueError, match="malformed MCP tool selector"):
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
            # missing description
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
            "description: Test.\n"
            "---\n"
        )
        with pytest.raises(ValidationError, match="system_prompt"):
            parse_agent_md(path)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_agent_md(tmp_path / "nope.md")
