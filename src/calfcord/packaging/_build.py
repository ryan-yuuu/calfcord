"""Shared infrastructure for the two packaging CLIs.

The CLIs (``cli_tools`` and ``cli_agents``) only differ in two places:
the templater they call and the input-validation rule for names. The
rest — argument parsing, tempdir management, ``docker buildx`` invocation,
success / failure output — is identical. This module owns that shared
shell so the per-command files stay focused on their command-specific
logic.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def make_parser(*, prog: str, item_kind: str, item_examples: str) -> argparse.ArgumentParser:
    """Build the shared argparse parser for both packaging CLIs.

    Args:
        prog: ``calfcord-package-tools`` or ``calfcord-package-agents``.
        item_kind: ``"tool"`` or ``"agent"`` (singular, for help text).
        item_examples: Concrete example values for the ``--help`` epilog
            (e.g. ``"terminal read_file search_files"`` or ``"scribe conan"``).
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            f"Build a slim calfcord Docker image hosting only the named "
            f"{item_kind}s. The build runs locally; ``docker push`` to a "
            f"registry is the operator's responsibility."
        ),
        epilog=f"Example: {prog} {item_examples} --tag my-image:1.0",
    )
    parser.add_argument(
        "names",
        nargs="+",
        metavar="NAME",
        help=f"One or more {item_kind} names to include in the image.",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help=(
            "Docker image tag to apply (e.g. ``my-image:1.0`` or "
            "``ghcr.io/me/foo:latest``). Required; no default because "
            "every build should be intentionally named."
        ),
    )
    parser.add_argument(
        "--context",
        type=Path,
        default=None,
        help=(
            "Override the Docker build context root. Default is the "
            "repo root (detected by walking up from this file's "
            "install location). Useful in tests."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the generated Dockerfile to stdout and exit without "
            "invoking docker. Use this to inspect what would be built "
            "or to feed the Dockerfile into a different builder."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Echo the docker buildx command before invoking it.",
    )
    return parser


def repo_root() -> Path:
    """Locate the repo root from this module's installed path.

    The CLI is installed inside ``site-packages/calfcord/packaging/``
    when used via ``uv run``; walk up until ``pyproject.toml`` appears.
    Falls back to the current working directory if the marker is
    missing — defensive against `pip install -e .` layouts.
    """
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def run_build(
    *,
    dockerfile_content: str,
    tag: str,
    context: Path,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Write the Dockerfile to a tempdir and invoke ``docker buildx build``.

    On ``dry_run``, prints the Dockerfile to stdout and returns 0
    without touching docker.

    On success, cleans up the tempdir. On failure, leaves it in place
    and prints the path so the operator can inspect.

    Returns the exit code (0 = success, non-zero = build failed or
    docker missing).
    """
    if dry_run:
        sys.stdout.write(dockerfile_content)
        return 0

    if shutil.which("docker") is None:
        sys.stderr.write(
            "error: 'docker' command not found on PATH. Install Docker "
            "Desktop or the docker CLI, then re-run.\n"
        )
        return 127

    # Widen the try-block to cover mkdtemp + write_text as well as the
    # buildx call: a fast Ctrl-C landing between mkdtemp and
    # subprocess.run would otherwise orphan the tempdir on disk without
    # the operator seeing the retained-at message. The `tempdir` local
    # is initialised before the try so the except can name it
    # regardless of when the interrupt arrives.
    tempdir = Path(tempfile.mkdtemp(prefix="calfcord-package-"))
    dockerfile_path = tempdir / "Dockerfile"
    try:
        dockerfile_path.write_text(dockerfile_content, encoding="utf-8")

        cmd = [
            "docker",
            "buildx",
            "build",
            "--tag",
            tag,
            "--file",
            str(dockerfile_path),
            str(context),
        ]
        if verbose:
            sys.stderr.write(f"+ {' '.join(cmd)}\n")
            sys.stderr.write(f"  (Dockerfile staged at {dockerfile_path})\n")

        result = subprocess.run(cmd, check=False)
    except OSError as e:
        sys.stderr.write(f"error: failed to invoke docker: {e}\n")
        sys.stderr.write(f"  Dockerfile retained at {dockerfile_path} for inspection.\n")
        return 1
    except KeyboardInterrupt:
        # Ctrl-C during a long build is common (multi-arch arm64 emulation
        # is slow). The default behavior would orphan the tempdir
        # without telling the operator where it went — print the
        # retained path explicitly before re-raising so the standard
        # SIGINT exit code surfaces and the dockerfile is recoverable.
        sys.stderr.write(
            f"\ninterrupted; Dockerfile retained at {dockerfile_path}\n"
        )
        raise

    if result.returncode != 0:
        # Leave the tempdir for forensic value — the operator likely
        # wants to read the Dockerfile to figure out what failed.
        sys.stderr.write(
            f"error: docker buildx build exited {result.returncode}.\n"
            f"  Generated Dockerfile retained at: {dockerfile_path}\n"
        )
        return result.returncode

    # Clean up only on success — keep the failure-case Dockerfile around.
    shutil.rmtree(tempdir, ignore_errors=True)
    sys.stderr.write(f"Built and tagged: {tag}\n")
    sys.stderr.write(
        f"  Inspect: docker image inspect {tag}\n"
        f"  Run:     docker run --rm {tag}\n"
        f"  Publish: docker push {tag}\n"
    )
    return 0
