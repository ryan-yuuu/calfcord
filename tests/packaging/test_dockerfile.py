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

from calfkit_organization.packaging.dockerfile import (
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

    def test_shell_brings_tmux(self) -> None:
        assert "tmux" in os_deps_for_tools(["shell"])
        assert "tmux" not in os_deps_for_tools(["read_file"])

    def test_grep_or_glob_bring_ripgrep(self) -> None:
        assert "ripgrep" in os_deps_for_tools(["grep"])
        assert "ripgrep" in os_deps_for_tools(["glob"])
        assert "ripgrep" not in os_deps_for_tools(["shell"])

    def test_unknown_tool_ignored(self) -> None:
        # The templater is loose-coupled — validation is the CLI's
        # job. An unknown name here just contributes no extra deps.
        assert os_deps_for_tools(["definitely_not_a_tool"]) == os_deps_for_tools([])

    def test_deduplication(self) -> None:
        # grep and glob both bring ripgrep; the union must have exactly
        # one ripgrep entry.
        deps = os_deps_for_tools(["grep", "glob"])
        assert deps.count("ripgrep") == 1

    def test_sorted_output(self) -> None:
        deps = os_deps_for_tools(["shell", "grep", "web_fetch"])
        assert deps == sorted(deps)


class TestRenderToolsDockerfile:
    def _render(self, names: list[str]) -> str:
        return render_tools_dockerfile(
            include_tools=names,
            registry_keys=["shell", "grep", "glob", "read_file"],
        )

    def test_bakes_include_filter_env_var(self) -> None:
        dockerfile = self._render(["shell", "grep"])
        # The auto-discovery loader reads this env var; the image
        # baking it is the whole point of per-tool images.
        assert "CALFCORD_TOOLS_INCLUDE=grep,shell" in dockerfile

    def test_only_lists_needed_os_packages(self) -> None:
        # shell-only image must include tmux but NOT ripgrep.
        dockerfile = self._render(["shell"])
        assert "tmux" in dockerfile
        assert "ripgrep" not in dockerfile
        # Always-on deps still present.
        assert "ca-certificates" in dockerfile
        assert "git" in dockerfile

    def test_grep_only_omits_tmux(self) -> None:
        dockerfile = self._render(["grep"])
        assert "ripgrep" in dockerfile
        assert "tmux" not in dockerfile

    def test_default_cmd_is_calfkit_tools(self) -> None:
        dockerfile = self._render(["shell"])
        # Per-tool images should boot the tools runner by default —
        # bridge / agent / router wouldn't make sense here.
        assert 'CMD ["calfkit-tools"]' in dockerfile

    def test_header_names_inputs(self) -> None:
        dockerfile = self._render(["shell", "grep"])
        # Header is operator-forensic; an inspected image should
        # tell you which CLI invocation produced it.
        assert "calfcord-package-tools" in dockerfile
        assert "grep" in dockerfile.split("\n")[0]
        assert "shell" in dockerfile.split("\n")[0]

    def test_deterministic_ordering(self) -> None:
        # Same inputs in different order produce identical output —
        # build caches hit reliably.
        a = self._render(["shell", "grep"])
        b = self._render(["grep", "shell"])
        assert a == b

    def test_includes_static_uv_copy(self) -> None:
        # The hermetic-build property (uv binary from upstream image
        # rather than curl|sh) must survive templating.
        dockerfile = self._render(["shell"])
        assert "ghcr.io/astral-sh/uv:latest" in dockerfile

    def test_runs_as_non_root(self) -> None:
        dockerfile = self._render(["shell"])
        assert "USER calfcord" in dockerfile


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
        # Agent images don't host tool bodies, so tmux / ripgrep are
        # unnecessary weight. Slice out just the apt-install block
        # before asserting absence — the explanatory comment above
        # the block mentions tmux/ripgrep by name to explain WHY
        # they're not installed, which would otherwise false-positive
        # a substring check on the whole Dockerfile.
        dockerfile = render_agents_dockerfile(include_agents=["scribe"])
        apt_block_start = dockerfile.index("RUN apt-get update")
        apt_block_end = dockerfile.index("rm -rf /var/lib/apt/lists/*")
        apt_block = dockerfile[apt_block_start:apt_block_end]
        assert "tmux" not in apt_block
        assert "ripgrep" not in apt_block
        # Always-on deps still present in the block.
        assert "ca-certificates" in apt_block
        assert "git" in apt_block

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
        assert "scribe" in dockerfile.split("\n")[0]


@pytest.mark.parametrize(
    "tool_name,expected_dep",
    [
        ("shell", "tmux"),
        ("grep", "ripgrep"),
        ("glob", "ripgrep"),
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
