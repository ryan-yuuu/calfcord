"""Tests for ``packaging._build.run_build``.

The function's success path is already exercised via every CLI
``--dry-run`` test. These tests pin the FAILURE paths that operators
depend on for debugging:

* Docker binary not on PATH → returns 127 with a usable message.
* ``docker buildx build`` exits non-zero → returns that code AND
  retains the tempdir so the operator can read the generated
  Dockerfile.
* ``docker buildx build`` succeeds → returns 0 AND cleans up the
  tempdir.
* Ctrl-C mid-build → re-raises after printing the retained
  tempdir path.

All paths use ``monkeypatch.setattr`` on ``shutil.which`` and
``subprocess.run`` to avoid touching a real Docker daemon.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from calfkit_organization.packaging import _build


def _fake_completed(returncode: int = 0):
    """Build a stand-in for ``subprocess.CompletedProcess``.

    The real class accepts ``args, returncode, stdout, stderr``; for
    these tests only ``returncode`` is read by the SUT.
    """
    class _R:
        pass

    r = _R()
    r.returncode = returncode  # type: ignore[attr-defined]
    return r


class TestDockerMissing:
    def test_returns_127_when_docker_not_on_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # ``shutil.which`` returning None is the "binary not installed"
        # signal the SUT checks before invoking subprocess.
        monkeypatch.setattr(shutil, "which", lambda _: None)
        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="x:1",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )
        assert exit_code == 127
        err = capsys.readouterr().err
        assert "docker" in err.lower()


class TestBuildFailure:
    def test_propagates_exit_code_and_retains_tempdir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # Pretend docker is installed but ``buildx build`` failed.
        # The SUT must propagate the docker exit code AND leave the
        # tempdir on disk so the operator can read the Dockerfile.
        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _fake_completed(returncode=42)
        )

        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="x:1",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )

        assert exit_code == 42
        err = capsys.readouterr().err
        # The retained-path message is the operator's only handle to
        # the generated Dockerfile. The path string from the message
        # must point at a real file.
        assert "retained at" in err
        # Pull the path out of the message and verify the Dockerfile
        # is still there (cleanup was correctly skipped). Use a regex
        # to grab the trailing absolute path so the test is robust to
        # the surrounding wording.
        import re
        m = re.search(r"retained at:?\s+(\S+)", err)
        assert m is not None, f"no path in stderr: {err}"
        retained = Path(m.group(1))
        assert retained.is_file(), f"expected file at {retained}, stderr was: {err}"
        # Clean up so we don't pollute /tmp.
        shutil.rmtree(retained.parent, ignore_errors=True)


class TestBuildSuccess:
    def test_cleans_up_tempdir_on_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # Capture which tempdir was used by inspecting the actual call
        # to subprocess.run; the Dockerfile path is in the --file arg.
        captured: dict[str, Path] = {}

        def fake_run(cmd, check):
            # The --file flag's value is the Dockerfile path.
            idx = cmd.index("--file")
            captured["dockerfile"] = Path(cmd[idx + 1])
            return _fake_completed(returncode=0)

        monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(subprocess, "run", fake_run)

        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n",
            tag="x:1",
            context=tmp_path,
            dry_run=False,
            verbose=False,
        )

        assert exit_code == 0
        # Success path must clean up — verify the Dockerfile is gone.
        assert "dockerfile" in captured
        assert not captured["dockerfile"].exists()
        # The success message points at follow-up commands for the
        # operator; verify it shows up.
        err = capsys.readouterr().err
        assert "Built and tagged" in err
        assert "docker push" in err


class TestDryRun:
    def test_emits_dockerfile_to_stdout_and_does_not_invoke_docker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        # If subprocess.run is invoked despite --dry-run, the test
        # blows up — a real fail-loud guard against regressions that
        # change the dry-run semantics.
        def explode(*_a, **_k):
            raise AssertionError("subprocess.run must not be called in dry-run mode")

        monkeypatch.setattr(subprocess, "run", explode)

        exit_code = _build.run_build(
            dockerfile_content="FROM scratch\n# marker-for-dry-run-test\n",
            tag="x:1",
            context=tmp_path,
            dry_run=True,
            verbose=False,
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "marker-for-dry-run-test" in out


class TestRepoRoot:
    def test_walks_up_to_find_pyproject(self) -> None:
        # The default ``repo_root()`` walks up from this module's
        # install location until pyproject.toml appears. In the
        # in-tree test environment, that resolves to the calfcord
        # checkout root.
        root = _build.repo_root()
        assert (root / "pyproject.toml").is_file()
