"""Tests for ``calfcord logs [component] [-f]`` (``calfcord.cli.logs``).

``logs`` tails the per-process supervisor logs Process Compose writes under
``$CALFCORD_HOME/state/logs/<name>.log`` (the §13.2 ``log_location`` contract).
These exercise the four shapes the command must honour without touching the real
supervisor, real timing, or real stdout:

* no component  → stream every *existing* component log, each line labeled;
* a named one   → stream just that file, unlabeled;
* unknown name  → a clean ``error:`` + exit 1 (typo, not a crash);
* missing dir   → a clean actionable ``error:`` + exit 1 (workspace not started);
* a valid name with no file yet → "no logs yet for <name>" + exit 0 (not an error);
* ``follow``    → picks up *appended* bytes, bounded by an injected sleep so the
  poll loop never busy-spins or blocks the test, and stops cleanly on Ctrl-C.

The output sink and the follow clock are injected, so a follow test drives the
loop deterministically and ends it by raising ``KeyboardInterrupt`` from the
fake sleep — the same signal a real Ctrl-C delivers.
"""

from __future__ import annotations

from pathlib import Path

from calfcord.cli import logs as logs_mod


def _write_log(home: Path, name: str, body: str) -> Path:
    """Write ``<home>/state/logs/<name>.log`` with ``body`` and return its path."""
    log_dir = home / "state" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{name}.log"
    path.write_text(body, encoding="utf-8")
    return path


def _sink() -> tuple[list[str], object]:
    """A capturing output sink + the list it appends to (one print per call)."""
    lines: list[str] = []

    def out(*args: object) -> None:
        lines.append(" ".join(str(a) for a in args))

    return lines, out


# --- single named component ------------------------------------------------


def test_tail_named_component_prints_its_contents(tmp_path: Path) -> None:
    _write_log(tmp_path, "broker", "broker line one\nbroker line two\n")
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component="broker", out=out)

    assert code == 0
    joined = "\n".join(lines)
    assert "broker line one" in joined
    assert "broker line two" in joined


def test_tail_named_component_is_unlabeled(tmp_path: Path) -> None:
    # A single explicit component streams raw — no "broker | " prefix; the label
    # is only useful to disambiguate the merged all-components view.
    _write_log(tmp_path, "bridge", "connected as bot\n")
    lines, out = _sink()

    logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component="bridge", out=out)

    assert any(line == "connected as bot" for line in lines)
    assert not any("bridge |" in line for line in lines)


# --- unknown component -----------------------------------------------------


def test_tail_unknown_component_errors(tmp_path: Path) -> None:
    (tmp_path / "state" / "logs").mkdir(parents=True)
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component="nope", out=out)

    assert code == 1
    assert any(line.startswith("error:") and "nope" in line for line in lines)


def test_tail_unknown_component_lists_valid_names(tmp_path: Path) -> None:
    # The error must be actionable: name the components the operator *can* tail so
    # a typo is one glance from fixed.
    (tmp_path / "state" / "logs").mkdir(parents=True)
    lines, out = _sink()

    logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component="brokr", out=out)

    joined = "\n".join(lines)
    assert "broker" in joined and "bridge" in joined


# --- missing log dir (workspace never started) -----------------------------


def test_tail_missing_logs_dir_is_clean_error(tmp_path: Path) -> None:
    # No state/logs at all → the workspace was never started. A clean actionable
    # error + exit 1, NOT a traceback.
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component=None, out=out)

    assert code == 1
    joined = "\n".join(lines)
    assert joined.startswith("error:") or any(line.startswith("error:") for line in lines)


def test_tail_missing_logs_dir_points_at_start(tmp_path: Path) -> None:
    lines, out = _sink()

    logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component=None, out=out)

    assert any("calfcord start" in line for line in lines)


# --- valid name, no file yet (never clocked in) ----------------------------


def test_tail_valid_component_without_a_file_is_not_an_error(tmp_path: Path) -> None:
    # The logs dir exists (workspace started) but this slot never ran, so it has
    # no file yet — informational, return 0.
    _write_log(tmp_path, "broker", "up\n")  # ensures the dir exists
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component="tools", out=out)

    assert code == 0
    assert any("no logs yet" in line and "tools" in line for line in lines)


# --- no component: stream every existing component log ----------------------


def test_tail_all_streams_each_existing_log_labeled(tmp_path: Path) -> None:
    _write_log(tmp_path, "broker", "broker boot\n")
    _write_log(tmp_path, "bridge", "bridge boot\n")
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component=None, out=out)

    assert code == 0
    joined = "\n".join(lines)
    # Each line is prefixed with its component so a merged view is readable.
    assert any("broker" in line and "broker boot" in line for line in lines)
    assert any("bridge" in line and "bridge boot" in line for line in lines)
    assert "broker boot" in joined and "bridge boot" in joined


def test_tail_all_discovers_agent_logs_via_detect_agents(tmp_path: Path) -> None:
    # An agent id is NOT a hardcoded name — it comes from the agents dir, exactly
    # as `detect_agents` (the seam `start`/`agent list` use) enumerates it.
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "scribe.md").write_text(
        "---\nname: scribe\nmodel: gpt-5-nano\n---\nYou are scribe.\n", encoding="utf-8"
    )
    _write_log(tmp_path, "scribe", "scribe answered\n")
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=agents, component="scribe", out=out)

    assert code == 0
    assert any("scribe answered" in line for line in lines)


def test_tail_all_with_empty_logs_dir_says_no_logs_yet(tmp_path: Path) -> None:
    # The workspace started (dir exists) but nothing has produced output yet.
    # The merged view must say so plainly rather than printing nothing.
    (tmp_path / "state" / "logs").mkdir(parents=True)
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component=None, out=out)

    assert code == 0
    assert any("no logs yet" in line for line in lines)


def test_tail_all_skips_absent_files_without_error(tmp_path: Path) -> None:
    # Only broker has run; the other declared slots have no file. Streaming all
    # must not error on the absent ones — it just shows what exists.
    _write_log(tmp_path, "broker", "only broker ran\n")
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component=None, out=out)

    assert code == 0
    assert any("only broker ran" in line for line in lines)


# --- follow ----------------------------------------------------------------


def test_tail_follow_streams_appended_lines_then_stops(tmp_path: Path) -> None:
    # `follow` emits existing content, then polls for *appended* bytes. The fake
    # sleep appends a new line on its first call (simulating the live process
    # writing more), then raises KeyboardInterrupt on its second call to end the
    # loop — exactly the signal a real Ctrl-C delivers. The loop must surface the
    # appended line and exit cleanly (return 0), without busy-spinning.
    log_path = _write_log(tmp_path, "broker", "first\n")
    lines, out = _sink()

    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("second\n")
            return
        raise KeyboardInterrupt

    code = logs_mod.tail(
        tmp_path,
        agents_dir=tmp_path / "agents",
        component="broker",
        follow=True,
        out=out,
        sleep=fake_sleep,
    )

    assert code == 0
    joined = "\n".join(lines)
    assert "first" in joined
    assert "second" in joined
    # The loop must have polled (slept), not busy-spun.
    assert calls["n"] >= 1


def test_tail_follow_handles_a_truncated_rotated_file(tmp_path: Path) -> None:
    # Log rotation can shrink the live file out from under the follower. The poll
    # must reset its offset to the new (smaller) end rather than slicing past it
    # and re-streaming stale bytes, and must not crash. The fake sleep truncates
    # the file on its first call, then ends the loop on its second.
    log_path = _write_log(tmp_path, "broker", "before rotate\n")
    lines, out = _sink()

    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            log_path.write_text("", encoding="utf-8")  # rotation truncates to empty
            return
        raise KeyboardInterrupt

    code = logs_mod.tail(
        tmp_path,
        agents_dir=tmp_path / "agents",
        component="broker",
        follow=True,
        out=out,
        sleep=fake_sleep,
    )

    assert code == 0
    # The pre-rotation content streamed once; the empty post-rotation file adds
    # nothing and certainly does not re-emit the old line.
    assert sum(1 for line in lines if "before rotate" in line) == 1


def test_tail_follow_waits_for_a_not_yet_created_file(tmp_path: Path) -> None:
    # `follow` on a valid slot whose file does not exist yet must WAIT (poll)
    # rather than error — the process may clock in moments later. The fake sleep
    # creates the file on its first call, then ends the loop on its second.
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)  # workspace started; this slot just hasn't run
    late = log_dir / "tools.log"
    lines, out = _sink()

    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            late.write_text("tools came online\n", encoding="utf-8")
            return
        raise KeyboardInterrupt

    code = logs_mod.tail(
        tmp_path,
        agents_dir=tmp_path / "agents",
        component="tools",
        follow=True,
        out=out,
        sleep=fake_sleep,
    )

    assert code == 0
    assert any("tools came online" in line for line in lines)


def test_tail_follow_unknown_component_errors_without_polling(tmp_path: Path) -> None:
    # The unknown-name guard fires before the follow loop, so a bad name never
    # enters a poll that would spin forever.
    (tmp_path / "state" / "logs").mkdir(parents=True)
    _lines, out = _sink()

    def fake_sleep(_seconds: float) -> None:
        raise AssertionError("must not poll on an unknown component")

    code = logs_mod.tail(
        tmp_path,
        agents_dir=tmp_path / "agents",
        component="bogus",
        follow=True,
        out=out,
        sleep=fake_sleep,
    )

    assert code == 1


# --- non-UTF-8 bytes (one-shot dump must not crash) ------------------------


def test_tail_named_component_with_non_utf8_bytes_does_not_crash(tmp_path: Path) -> None:
    # "Always show what the broker said before it died" must hold even when a log
    # line contains non-UTF-8 bytes (a partial multibyte write, a binary splat, a
    # mis-encoded child). The one-shot dump must decode tolerantly — exit 0 with a
    # Unicode replacement char — rather than raising an uncaught UnicodeDecodeError
    # (a ValueError main() does not catch) and crashing with a traceback.
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    # 0xFF is never a valid standalone UTF-8 byte; strict decoding raises on it.
    (log_dir / "broker.log").write_bytes(b"good line\nbad \xff byte\n")
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component="broker", out=out)

    assert code == 0
    joined = "\n".join(lines)
    assert "good line" in joined
    assert "�" in joined  # the offending byte became the replacement char


def test_tail_all_with_non_utf8_bytes_does_not_crash(tmp_path: Path) -> None:
    # The merged (labeled) one-shot view must be just as tolerant: a single
    # non-UTF-8 component log cannot take down the whole "logs" command.
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "broker.log").write_bytes(b"broker \xff splat\n")
    lines, out = _sink()

    code = logs_mod.tail(tmp_path, agents_dir=tmp_path / "agents", component=None, out=out)

    assert code == 0
    assert any("�" in line for line in lines)


# --- shared supervisor-log stem (no cross-module literal drift) -------------


def test_supervisor_log_stem_agrees_with_lifecycle_filename() -> None:
    # logs.py and lifecycle.py must name the supervisor's own log identically; a
    # drift between the stem logs reads and the filename lifecycle writes would
    # make `calfcord logs process-compose` silently miss the file. Both must
    # derive from the single shared stem compose.py owns, so the stem + ".log"
    # reconstructs lifecycle's filename exactly.
    from calfcord.supervisor import compose, lifecycle

    assert f"{compose.SUPERVISOR_LOG_STEM}.log" == lifecycle._SUPERVISOR_LOG_FILENAME


def test_logs_uses_the_shared_supervisor_log_stem() -> None:
    # The logs module must not carry its own hardcoded "process-compose" literal;
    # the supervisor-log name it tails must be the shared compose.py constant so
    # the two cannot drift.
    from calfcord.supervisor import compose

    assert logs_mod._SUPERVISOR_LOG_NAME == compose.SUPERVISOR_LOG_STEM
