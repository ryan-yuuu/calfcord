"""Tests for the Dockerfile templater.

The templater is pure-string output, so tests assert on structural
properties of the rendered Dockerfile rather than golden-comparing the
whole thing. Golden comparisons would fail on every cosmetic update
to the canonical Dockerfile, which is high-friction and low-signal —
the structural checks below cover the contracts that actually matter
to operators (right OS packages, right ENV vars, right CMD).
"""

from __future__ import annotations

import pytest

from calfcord.packaging.dockerfile import (
    os_deps_for_tools,
    render_agents_dockerfile,
    render_tools_dockerfile,
)


class TestOsDepsForTools:
    def test_always_on_deps_included(self) -> None:
        # ca-certificates + git are always on regardless of tool list.
        deps = os_deps_for_tools([])
        assert "ca-certificates" in deps
        assert "git" in deps

    def test_search_files_brings_ripgrep(self) -> None:
        assert "ripgrep" in os_deps_for_tools(["search_files"])
        assert "ripgrep" not in os_deps_for_tools(["read_file"])

    def test_terminal_brings_no_tmux(self) -> None:
        # The hermes terminal uses bash + a PTY, not tmux.
        assert "tmux" not in os_deps_for_tools(["terminal"])
        assert "tmux" not in os_deps_for_tools(["process"])

    def test_unknown_tool_ignored(self) -> None:
        # The templater is loose-coupled — validation is the CLI's
        # job. An unknown name here just contributes no extra deps.
        assert os_deps_for_tools(["definitely_not_a_tool"]) == os_deps_for_tools([])

    def test_deduplication(self) -> None:
        # The union must contain no duplicates regardless of overlap
        # between always-on deps and per-tool deps.
        deps = os_deps_for_tools(["search_files", "terminal", "web_fetch"])
        assert len(deps) == len(set(deps))

    def test_sorted_output(self) -> None:
        deps = os_deps_for_tools(["terminal", "search_files", "web_fetch"])
        assert deps == sorted(deps)


class TestRenderToolsDockerfile:
    def _render(self, names: list[str]) -> str:
        return render_tools_dockerfile(include_tools=names)

    def test_bakes_include_filter_env_var(self) -> None:
        dockerfile = self._render(["terminal", "search_files"])
        # The auto-discovery loader reads this env var; the image
        # baking it is the whole point of per-tool images.
        assert "CALFCORD_TOOLS_INCLUDE=search_files,terminal" in dockerfile

    def test_only_lists_needed_os_packages(self) -> None:
        # search_files image includes ripgrep; a read_file-only image does not.
        dockerfile = self._render(["search_files"])
        assert "ripgrep" in dockerfile
        df_readonly = self._render(["read_file"])
        assert "ripgrep" not in df_readonly
        # Always-on deps still present.
        assert "ca-certificates" in df_readonly
        assert "git" in df_readonly

    def test_search_files_brings_ripgrep_not_tmux(self) -> None:
        dockerfile = self._render(["search_files"])
        assert "ripgrep" in dockerfile
        assert "tmux" not in dockerfile

    def test_default_cmd_is_calfkit_tools(self) -> None:
        dockerfile = self._render(["terminal"])
        # Per-tool images should boot the tools runner by default —
        # bridge / agent / router wouldn't make sense here.
        assert 'CMD ["calfkit-tools"]' in dockerfile

    def test_header_names_inputs(self) -> None:
        dockerfile = self._render(["terminal", "search_files"])
        # Header is operator-forensic; an inspected image should
        # tell you which CLI invocation produced it.
        assert "calfcord-package-tools" in dockerfile
        # Header banner now lives below the # syntax= directive on
        # line 1; check the banner block (lines 2+).
        banner_line = dockerfile.split("\n", 2)[1]
        assert "search_files" in banner_line
        assert "terminal" in banner_line

    def test_deterministic_ordering(self) -> None:
        # Same inputs in different order produce identical output —
        # build caches hit reliably.
        a = self._render(["terminal", "search_files"])
        b = self._render(["search_files", "terminal"])
        assert a == b

    def test_includes_static_uv_copy(self) -> None:
        # The hermetic-build property (uv binary from upstream image
        # rather than curl|sh) must survive templating.
        dockerfile = self._render(["terminal"])
        assert "ghcr.io/astral-sh/uv:latest" in dockerfile

    def test_runs_as_non_root(self) -> None:
        dockerfile = self._render(["terminal"])
        assert "USER calfcord" in dockerfile

    def test_runtime_copy_chowns_to_calfcord(self) -> None:
        """The runtime stage's ``COPY --from=builder`` MUST set
        ``--chown=calfcord:calfcord`` so /app is owned by the non-root
        user. A templater bug that drops this would silently produce an
        image where calfcord (uid 1000) can't write to /app or its
        venv — bind-mount semantics break, .pyc compilation fails (if
        ever re-enabled), etc. Load-bearing for the non-root model."""
        dockerfile = self._render(["terminal"])
        assert "COPY --from=builder --chown=calfcord:calfcord /app /app" in dockerfile

    def test_omits_banner_suppression(self) -> None:
        """Per-tool images host only vendored calfkit-tools nodes and never
        import the openhands SDK, so there's no banner to silence — the env
        var must NOT be baked (only the agents image, with its codex
        provider, needs it)."""
        dockerfile = self._render(["terminal"])
        assert "OPENHANDS_SUPPRESS_BANNER" not in dockerfile

    def test_syntax_directive_on_line_one(self) -> None:
        """Docker only honors the ``# syntax=`` frontend-selector
        directive when it's the first non-blank line of the
        Dockerfile. A regression that puts the generated banner above
        it would silently disable the 1.x BuildKit frontend that the
        ``--mount=type=cache`` lines depend on."""
        dockerfile = self._render(["terminal"])
        first_line = dockerfile.split("\n", 1)[0]
        assert first_line.startswith("# syntax=docker/dockerfile:")


class TestRenderToolsDockerfileAliases:
    """Tests for the ``aliases`` kwarg on ``render_tools_dockerfile``.

    The alias map bakes into the runtime ``ENV`` block as
    ``CALFCORD_TOOLS_ALIAS=src1=dst1,src2=dst2``; at boot the
    discovery loader clones each source tool's ``ToolNodeDef`` under
    the target name. The tests below pin the env-var shape, the
    sort-for-determinism property, the no-aliases default, and the
    header surfacing so an operator inspecting the image can see the
    rename without decoding the ENV block.
    """

    def test_aliases_baked_into_env_block(self) -> None:
        dockerfile = render_tools_dockerfile(
            include_tools=["patch_eu"],
            aliases={"patch": "patch_eu"},
        )
        # The alias env var lives in the ENV block. A future refactor
        # that puts it in a separate ``ENV`` statement would also
        # satisfy this assertion, which is fine — the contract is
        # "this name=value pair appears somewhere in the file."
        assert "CALFCORD_TOOLS_ALIAS=patch=patch_eu" in dockerfile

    def test_aliases_sorted_for_determinism(self) -> None:
        """Same input dict in different insertion orders must produce
        byte-identical output so the build cache hits reliably across
        invocations."""
        a = render_tools_dockerfile(
            include_tools=["patch_eu", "terminal_eu"],
            aliases={"patch": "patch_eu", "terminal": "terminal_eu"},
        )
        b = render_tools_dockerfile(
            include_tools=["patch_eu", "terminal_eu"],
            aliases={"terminal": "terminal_eu", "patch": "patch_eu"},
        )
        assert a == b

    def test_no_aliases_omits_env_var(self) -> None:
        """Default behavior (no ``aliases`` kwarg or empty dict)
        produces no ``CALFCORD_TOOLS_ALIAS`` line. A stray line with
        an empty value would parse as an empty alias map at boot
        (harmless but operator-noisy), so it's worth pinning."""
        dockerfile = render_tools_dockerfile(include_tools=["terminal"])
        assert "CALFCORD_TOOLS_ALIAS" not in dockerfile
        # Same for explicit None / empty dict.
        assert (
            "CALFCORD_TOOLS_ALIAS"
            not in render_tools_dockerfile(include_tools=["terminal"], aliases=None)
        )
        assert (
            "CALFCORD_TOOLS_ALIAS"
            not in render_tools_dockerfile(include_tools=["terminal"], aliases={})
        )

    def test_header_mentions_aliases_when_present(self) -> None:
        """The generated banner surfaces the rename map so a copy of
        the Dockerfile pulled out of the image is self-describing
        without re-parsing the ENV block."""
        dockerfile = render_tools_dockerfile(
            include_tools=["patch_eu"],
            aliases={"patch": "patch_eu"},
        )
        # The header lives in the first ~6 lines of the file. Pin the
        # rename appearance there so it's discoverable at a glance.
        head = "\n".join(dockerfile.split("\n", 8)[:8])
        assert "patch" in head and "patch_eu" in head
        assert "Renames" in head or "→" in head

    def test_aliases_do_not_break_syntax_directive_position(self) -> None:
        """The ``# syntax=`` directive must still be on line 1 even
        with the optional header alias line inserted below."""
        dockerfile = render_tools_dockerfile(
            include_tools=["patch_eu"],
            aliases={"patch": "patch_eu"},
        )
        first_line = dockerfile.split("\n", 1)[0]
        assert first_line.startswith("# syntax=docker/dockerfile:")


class TestRenderAgentsDockerfile:
    def test_copies_only_selected_agents(self) -> None:
        dockerfile = render_agents_dockerfile(include_agents=["scribe", "conan"])
        # Selected agents get individual COPY lines.
        assert "COPY agents/scribe.md ./agents/scribe.md" in dockerfile
        assert "COPY agents/conan.md ./agents/conan.md" in dockerfile
        # The catch-all directory COPY (used by the canonical
        # Dockerfile) MUST NOT appear — that would defeat the
        # filesystem-level filtering.
        assert "COPY agents ./agents" not in dockerfile

    def test_no_tools_os_deps(self) -> None:
        # Agent images don't host tool bodies, so tmux / ripgrep / git
        # are unnecessary weight. Slice out just the apt-install block
        # before asserting absence — the explanatory comment above
        # the block mentions the excluded packages by name to explain
        # WHY they're not installed, which would otherwise
        # false-positive a substring check on the whole Dockerfile.
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        apt_block_start = dockerfile.index("RUN apt-get update")
        apt_block_end = dockerfile.index("rm -rf /var/lib/apt/lists/*")
        apt_block = dockerfile[apt_block_start:apt_block_end]
        assert "tmux" not in apt_block
        assert "ripgrep" not in apt_block
        # git intentionally excluded from agent images — ~30MB savings
        # vs. zero use case (no tool body to run git commands).
        assert "git" not in apt_block
        # The trust store is still needed for LLM-provider HTTP calls.
        assert "ca-certificates" in apt_block

    def test_no_tools_include_env_var(self) -> None:
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        # CALFCORD_TOOLS_INCLUDE is irrelevant in agent images
        # (they don't run discover_tools).
        assert "CALFCORD_TOOLS_INCLUDE" not in dockerfile

    def test_default_cmd_is_calfkit_agent(self) -> None:
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        assert 'CMD ["calfkit-agent"]' in dockerfile

    def test_deterministic_ordering(self) -> None:
        a = render_agents_dockerfile(include_agents=["scribe", "conan"])
        b = render_agents_dockerfile(include_agents=["conan", "scribe"])
        # COPY lines are sorted alphabetically internally, so the two
        # invocations produce identical output. Build cache stays warm
        # regardless of caller arg order.
        assert a == b

    def test_header_names_inputs(self) -> None:
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        assert "calfcord-package-agents" in dockerfile
        # Header banner now lives BELOW the # syntax= directive on
        # line 1; look at line 2 onward for the names.
        assert "scribe" in dockerfile.split("\n", 2)[1]

    def test_runtime_copy_chowns_to_calfcord(self) -> None:
        """Same chown contract as tools images. Bind-mount semantics
        on the agent service depend on /app being calfcord-owned."""
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        assert "COPY --from=builder --chown=calfcord:calfcord /app /app" in dockerfile

    def test_bakes_banner_suppression(self) -> None:
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        assert "OPENHANDS_SUPPRESS_BANNER=1" in dockerfile

    def test_syntax_directive_on_line_one(self) -> None:
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        first_line = dockerfile.split("\n", 1)[0]
        assert first_line.startswith("# syntax=docker/dockerfile:")

    def test_no_redundant_mkdir(self) -> None:
        """Docker auto-creates COPY destination directories, so the
        previously-emitted ``RUN mkdir -p ./agents`` was dead weight.
        Pin its absence so a future "be defensive" refactor doesn't
        silently re-add it."""
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        assert "mkdir -p ./agents" not in dockerfile


@pytest.mark.parametrize(
    "tool_name,expected_dep",
    [
        ("search_files", "ripgrep"),
    ],
)
def test_per_tool_os_dep_mapping(tool_name: str, expected_dep: str) -> None:
    """Parametrized sanity check on the per-tool OS-dep mapping.

    Adding a new tool that needs an OS binary requires updating both
    the canonical Dockerfile AND the templater's mapping table — this
    test catches the second half of that pair.
    """
    deps = os_deps_for_tools([tool_name])
    assert expected_dep in deps
