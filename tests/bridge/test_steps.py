"""Unit tests for the PURE step renderers in :mod:`calfcord.bridge.steps_render`.

After the calfkit-0.12 migration the live progress surface is driven by
:class:`~calfcord.bridge.progress.ProgressRenderer` off the normalized
``StepEvent`` stream (its lifecycle is covered in ``test_progress.py``), not by
a Kafka steps consumer. What remains here are the renderer's pure functions,
which take an input and return a string with no Discord, Kafka, or state:

* the compact LIVE renderer — :func:`render_step_line` over one ``StepEvent``
  (prose for ``agent_message``, an inline-code ``tool_name(args)`` line for
  ``tool_call``, a short fenced ``⎿`` block for ``tool_result``) plus the
  tail-window / progress-body shaping (``TestLiveRender``);
* the full ``⤵ steps`` transcript tree renderer over ``Sequence[ModelMessage]``
  (``TestTreeRender``) — byte-for-byte stable so persisted transcripts keep
  rendering identically;
* the backtick-fence neutralizer (``TestFenceSafe``).
"""

from __future__ import annotations

from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

import calfcord.bridge.steps_render as steps_render
from calfcord.bridge.step_events import StepEvent


def _step(
    kind: str,
    *,
    text: str = "",
    name: str | None = None,
    args: dict[str, object] | None = None,
) -> StepEvent:
    """Build a StepEvent for the renderer. correlation_id/depth/emitter are
    fixed — the pure renderer reads only ``kind``/``text``/``name``/``args``."""
    return StepEvent(
        kind=kind,  # type: ignore[arg-type]
        correlation_id="corr-1",
        depth=0,
        emitter="aksel",
        text=text,
        name=name,
        args=args,
    )


class TestLiveRender:
    """The compact live renderer (:func:`render_step_line`): prose text, an
    inline-code ``tool_name(args)`` call line, and a short fenced ``⎿`` result
    block of up to a few lines, plus the tail-window cap that keeps the in-place
    edit under Discord's 2000-char limit."""

    def test_agent_message_kept_as_prose(self) -> None:
        assert steps_render.render_step_line(_step("agent_message", text="On it — checking now.")) == (
            "On it — checking now."
        )

    def test_agent_message_is_stripped(self) -> None:
        # Leading/trailing whitespace is trimmed before truncation.
        assert steps_render.render_step_line(_step("agent_message", text="  hi there  ")) == "hi there"

    def test_whitespace_only_agent_message_renders_none(self) -> None:
        # An empty preamble (model emitted a tool call with no narrative) shows
        # nothing — the line is dropped, not posted blank.
        assert steps_render.render_step_line(_step("agent_message", text="   \n  ")) is None

    def test_long_agent_message_truncated_with_marker(self) -> None:
        rendered = steps_render.render_step_line(_step("agent_message", text="a" * 5000))
        assert rendered is not None
        assert len(rendered) == steps_render._LIVE_TEXT_MAX_CHARS
        assert rendered.endswith(steps_render.TRUNCATION_MARKER)

    def test_tool_call_renders_keyword_args_as_inline_code(self) -> None:
        rendered = steps_render.render_step_line(_step("tool_call", name="weather", args={"city": "Tokyo", "n": 5}))
        assert rendered == '`weather(city="Tokyo", n=5)`'

    def test_empty_args_render_bare_parens(self) -> None:
        assert steps_render.render_step_line(_step("tool_call", name="ping", args={})) == "`ping()`"

    def test_non_object_and_none_args_render_bare_parens(self) -> None:
        # The step seam coerces a non-object arg (a bare list/scalar/unparseable
        # JSON) to ``{}`` before it reaches the renderer, so by here a tool call
        # with no usable args is just an empty dict (or None) → ``name()``. This
        # replaces the old raw-JSON fallback ladder, which the seam made dead.
        assert steps_render.render_step_line(_step("tool_call", name="f", args={})) == "`f()`"
        assert steps_render.render_step_line(_step("tool_call", name="f", args=None)) == "`f()`"

    def test_nested_object_arg_values_render_as_compact_json(self) -> None:
        # Non-scalar arg VALUES are JSON-encoded with compact separators (no
        # space after ':'/','), keeping the call line tight.
        rendered = steps_render.render_step_line(
            _step("tool_call", name="q", args={"filter": {"gte": 5, "lt": 10}, "tags": [1, 2]})
        )
        assert rendered == '`q(filter={"gte":5,"lt":10}, tags=[1,2])`'

    def test_long_tool_call_line_truncated_inline(self) -> None:
        # A very long call line is cut on one line (no newline marker) so it
        # stays safe inside the inline-code span.
        rendered = steps_render.render_step_line(_step("tool_call", name="t", args={"pad": "x" * 5000}))
        assert rendered is not None
        assert len(rendered) <= steps_render._LIVE_TOOL_MAX_CHARS + 2  # + the wrapping backticks
        assert rendered.endswith("…`")

    def test_tool_result_renders_short_result_in_fenced_block(self) -> None:
        # ``⎿`` first line, wrapped in a fence so a stray ``` can't break out.
        assert steps_render.render_step_line(_step("tool_result", text="18C")) == "```\n⎿ 18C\n```"

    def test_tool_result_preserves_real_lines(self) -> None:
        # Real lines are PRESERVED (not collapsed): ⎿ first, continuation
        # aligned two spaces under it.
        rendered = steps_render.render_step_line(_step("tool_result", text="line1\nline2\nline3"))
        assert rendered == "```\n⎿ line1\n  line2\n  line3\n```"

    def test_tool_result_keeps_first_lines_and_marks_dropped_lines(self) -> None:
        content = "\n".join(f"line{i}" for i in range(5))
        rendered = steps_render.render_step_line(_step("tool_result", text=content))
        assert rendered is not None
        # Only the first _LIVE_RETURN_MAX_LINES survive; the DROPPED-lines marker
        # rides the last kept line, and the full result stays on the ⤵ view.
        assert "⎿ line0" in rendered
        assert "line2 … (truncated)" in rendered
        assert "line3" not in rendered

    def test_exactly_max_lines_is_not_marked_truncated(self) -> None:
        # A result of exactly _LIVE_RETURN_MAX_LINES short lines is complete —
        # no spurious "(truncated)" marker (guards the > vs >= edge).
        content = "\n".join(f"line{i}" for i in range(steps_render._LIVE_RETURN_MAX_LINES))
        rendered = steps_render.render_step_line(_step("tool_result", text=content))
        assert rendered is not None
        assert "(truncated)" not in rendered
        assert "…" not in rendered

    def test_single_backticks_in_result_preserved_in_fence(self) -> None:
        # Inside a code fence single/double backticks render literally — no need
        # to mangle them the way the old inline-code span did.
        assert steps_render.render_step_line(_step("tool_result", text="use `code` now")) == (
            "```\n⎿ use `code` now\n```"
        )

    def test_triple_backticks_in_result_neutralized(self) -> None:
        # A run of 3+ backticks would close the fence early; it is woven with
        # zero-width spaces so only the wrapping fence survives as a raw run.
        rendered = steps_render.render_step_line(_step("tool_result", text="```py"))
        assert rendered is not None
        assert rendered.count("```") == 2  # the wrapping fence only
        assert "\u200b" in rendered

    def test_oversized_tool_result_line_is_cut_on_that_line(self) -> None:
        rendered = steps_render.render_step_line(_step("tool_result", text="x" * 5000))
        assert rendered is not None
        # A single over-long line is cut with a trailing "…" ON that line; no
        # whole lines were dropped, so there is NO "(truncated)" marker.
        assert rendered.count("x") <= steps_render._LIVE_RETURN_LINE_MAX_CHARS
        assert rendered.rstrip("`\n").endswith("…")
        assert "(truncated)" not in rendered

    def test_handoff_renders_none(self) -> None:
        # Handoffs are claimed by the A2A dispatcher upstream and never reach the
        # live renderer; defensively, render_step_line shows nothing for one.
        assert steps_render.render_step_line(_step("handoff")) is None

    def test_tail_window_drops_oldest_and_marks_elision(self) -> None:
        lines = [f"line{i}" for i in range(100)]
        body = steps_render._tail_window(lines, max_chars=40)
        assert body.startswith(steps_render._HIDDEN_STEPS_MARKER)
        assert "line99" in body  # most recent survives
        assert "line0\n" not in body  # oldest dropped

    def test_tail_window_no_marker_when_everything_fits(self) -> None:
        body = steps_render._tail_window(["a", "b", "c"], max_chars=1000)
        assert body == "a\nb\nc"
        assert steps_render._HIDDEN_STEPS_MARKER not in body

    def test_progress_content_is_body_only_and_hard_clamped(self) -> None:
        lines = [f"`⎿ {'x' * 150}`" for _ in range(200)]
        content = steps_render._progress_content(lines)
        # No header line; the message IS the (tail-windowed) trace, never over
        # Discord's hard cap.
        assert not content.startswith("⚙ running…")
        assert len(content) <= steps_render._DISCORD_MESSAGE_LIMIT


class TestTreeRender:
    """The full ``⤵ steps`` transcript renderer (``_render_tree_blocks``):
    Claude-Code-style ``● tool(args)`` / ``⎿ result`` blocks, one per visual
    block (a tool call and its result are ONE block), no per-part truncation,
    paired by ``tool_call_id`` (handles parallel calls); a return whose call is
    absent from the slice renders standalone."""

    def test_text_then_call_pair_counts_as_two_blocks(self) -> None:
        delta = [
            ModelResponse(
                parts=[
                    TextPart(content="Let me check."),
                    ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1"),
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1")]),
        ]
        blocks = steps_render._render_tree_blocks(delta)
        # Prose block + ONE call/return block — the result is folded into its
        # call, so a tool use credits a single step.
        assert blocks == ["Let me check.", '```\n● weather(c="Tokyo")\n  ⎿  18C\n```']

    def test_multiline_result_nests_with_aligned_continuation(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"cmd": "ls"}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="shell", content="a\nb\nc", tool_call_id="t1")]),
        ]
        assert steps_render._render_tree_blocks(delta) == ['```\n● shell(cmd="ls")\n  ⎿  a\n     b\n     c\n```']

    def test_parallel_calls_pair_to_their_own_returns(self) -> None:
        delta = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="a"),
                    ToolCallPart(tool_name="news", args={"t": "tech"}, tool_call_id="b"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="weather", content="18C", tool_call_id="a"),
                    ToolReturnPart(tool_name="news", content="headline", tool_call_id="b"),
                ]
            ),
        ]
        # Two call/return blocks, each return matched to its call BY ID (not by
        # position) and rendered in call order.
        assert steps_render._render_tree_blocks(delta) == [
            '```\n● weather(c="Tokyo")\n  ⎿  18C\n```',
            '```\n● news(t="tech")\n  ⎿  headline\n```',
        ]

    def test_call_without_return_renders_call_line_alone(self) -> None:
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="slow", args={"x": 1}, tool_call_id="p")])]
        assert steps_render._render_tree_blocks(delta) == ["```\n● slow(x=1)\n```"]

    def test_orphan_return_renders_standalone_not_dropped(self) -> None:
        # A return whose call predates the slice must NOT be silently dropped —
        # that would also skew the step count gating the ⤵ button.
        delta = [ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="z")])]
        assert steps_render._render_tree_blocks(delta) == ["```\n⎿  18C\n```"]

    def test_no_per_part_truncation_in_full_view(self) -> None:
        big = "y" * 9000
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="dump", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="dump", content=big, tool_call_id="t1")]),
        ]
        rendered = steps_render._render_tree_blocks(delta)[0]
        # The full payload survives — the only bound is the overall message cap
        # (enforced by steps_toggle's file-attachment path), not a per-part cap.
        assert rendered.count("y") == 9000

    def test_triple_backticks_in_result_cannot_break_the_fence(self) -> None:
        delta = [
            ModelResponse(parts=[ToolCallPart(tool_name="echo", args={}, tool_call_id="t1")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="echo", content="```py\ncode\n```", tool_call_id="t1")]),
        ]
        rendered = steps_render._render_tree_blocks(delta)[0]
        # Only the wrapping fence survives as a raw triple-backtick run; the
        # embedded fences are woven with zero-width spaces.
        assert rendered.count("```") == 2
        assert "\u200b" in rendered

    def test_skips_prompt_parts(self) -> None:
        delta = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="system."),
                    UserPromptPart(content="hello"),
                ]
            ),
        ]
        assert steps_render._render_tree_blocks(delta) == []

    def test_parallel_call_with_one_missing_return(self) -> None:
        # Two parallel calls, only the first has returned this slice: the
        # paired call folds its result, the in-flight one renders alone.
        delta = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="a", args={}, tool_call_id="a"),
                    ToolCallPart(tool_name="b", args={}, tool_call_id="b"),
                ]
            ),
            ModelRequest(parts=[ToolReturnPart(tool_name="a", content="ra", tool_call_id="a")]),
        ]
        assert steps_render._render_tree_blocks(delta) == [
            "```\n● a()\n  ⎿  ra\n```",
            "```\n● b()\n```",
        ]

    def test_return_before_its_call_renders_once_not_twice(self) -> None:
        # Order-independence: a return that appears BEFORE its call in the
        # slice must fold into the call exactly once — never render both
        # standalone AND nested (which would also inflate the step count).
        delta = [
            ModelRequest(parts=[ToolReturnPart(tool_name="a", content="EARLY", tool_call_id="x")]),
            ModelResponse(parts=[ToolCallPart(tool_name="a", args={}, tool_call_id="x")]),
        ]
        assert steps_render._render_tree_blocks(delta) == ["```\n● a()\n  ⎿  EARLY\n```"]

    def test_full_view_preserves_arg_whitespace_fidelity(self) -> None:
        # collapse=False on the full view keeps inner whitespace in arg values
        # byte-for-byte (the live preview would collapse "a  b" -> "a b").
        delta = [ModelResponse(parts=[ToolCallPart(tool_name="run", args={"cmd": "a  b"}, tool_call_id="t1")])]
        assert steps_render._render_tree_blocks(delta) == ['```\n● run(cmd="a  b")\n```']


class TestFenceSafe:
    """``_fence_safe`` neutralizes runs of 3+ backticks (which would close a
    Discord code fence early regardless of the opening fence length) while
    leaving 1-2 backtick runs — which render literally inside a block —
    untouched."""

    def test_single_and_double_backtick_runs_untouched(self) -> None:
        assert steps_render._fence_safe("a `b` c") == "a `b` c"
        assert steps_render._fence_safe("``x``") == "``x``"

    def test_runs_of_three_or_more_are_woven_with_zwsp(self) -> None:
        for n in (3, 4, 6):
            out = steps_render._fence_safe("`" * n)
            assert "```" not in out  # no raw 3-run survives to close a fence
            assert out.count("`") == n  # every backtick preserved, just separated
            assert out.count("\u200b") == n - 1
