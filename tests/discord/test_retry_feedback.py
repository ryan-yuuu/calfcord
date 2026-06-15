"""Tests for the shared retry-with-feedback policy module.

Covers the pure helpers consumed by both
:mod:`calfcord.bridge.outbox` (channel replies, async
republish) and :mod:`calfcord.tools.private_chat` (A2A
audit projections, sync re-invocation):

* :func:`build_retry_reminder` — pure function shape, override map.
* :func:`build_retry_history` — message-history layout.
* :func:`chunk_split` — boundary search, hard-cut fallback.
* :func:`classify_error` — drop / transient / agent_fixable triage.

Orchestration-side tests (per-path retry plumbing, transport, fallback
posting) live with each consumer: bridge outbox cases in
``tests/bridge/test_outbox_retry.py``, A2A cases in
``tests/tools/test_private_chat.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from calfcord.discord.retry_feedback import (
    _RETRY_REMINDER_OVERRIDES,
    CHUNK_SAFE_SIZE,
    build_retry_history,
    build_retry_reminder,
    chunk_split,
    classify_error,
)


def _http_exc(
    exc_cls: type[discord.HTTPException], status: int, *, code: int = 0
) -> discord.HTTPException:
    """Build a discord HTTPException with status + JSON error code."""
    response = SimpleNamespace(status=status, reason="Test")
    return exc_cls(response, {"message": "synthetic", "code": code})


# ---------------------------------------------------------------------------
# build_retry_reminder
# ---------------------------------------------------------------------------


class TestBuildRetryReminder:
    def test_wraps_in_system_reminder_tags(self) -> None:
        err = _http_exc(discord.HTTPException, 400, code=50035)
        out = build_retry_reminder(err, "x" * 3000)
        assert out.startswith("<system-reminder>")
        assert out.rstrip().endswith("</system-reminder>")

    def test_includes_status_and_failed_length(self) -> None:
        err = _http_exc(discord.HTTPException, 400, code=50035)
        out = build_retry_reminder(err, "x" * 3187)
        assert "HTTP 400" in out
        assert "3187" in out

    def test_includes_do_not_mention_directive(self) -> None:
        err = _http_exc(discord.HTTPException, 400, code=50035)
        out = build_retry_reminder(err, "fail")
        assert "Do NOT mention" in out
        assert "user does NOT see" in out

    def test_uses_override_when_present(self) -> None:
        err = _http_exc(discord.HTTPException, 418, code=999)
        try:
            _RETRY_REMINDER_OVERRIDES[(418, 999)] = "Custom guidance for teapot."
            out = build_retry_reminder(err, "fail")
            assert "Custom guidance for teapot." in out
            # Generic body is suppressed.
            assert "HTTP 418" not in out
        finally:
            del _RETRY_REMINDER_OVERRIDES[(418, 999)]

    def test_override_with_different_code_falls_through_to_generic(self) -> None:
        """An override for ``(418, 42)`` does NOT match ``(418, 999)``.

        v1 ships with no wildcard support — override matching is exact
        on both ``status`` and ``code``. If a future use case needs
        wildcard semantics, the override-lookup function should be
        extended; YAGNI says not to ship that complexity preemptively.
        """
        err = _http_exc(discord.HTTPException, 418, code=999)
        try:
            _RETRY_REMINDER_OVERRIDES[(418, 42)] = "Specific code 42 only."
            out = build_retry_reminder(err, "fail")
            # The override does not match (different code); generic
            # template is used instead.
            assert "Specific code 42 only." not in out
            assert "HTTP 418" in out
        finally:
            del _RETRY_REMINDER_OVERRIDES[(418, 42)]


# ---------------------------------------------------------------------------
# build_retry_history
# ---------------------------------------------------------------------------


class TestBuildRetryHistory:
    def test_appends_user_prompt_then_failed_response(self) -> None:
        original: list = []
        out = build_retry_history(
            original_history=original,
            original_user_prompt="tell me a story",
            failed_text="once upon a time " * 200,
        )
        assert len(out) == 2
        assert isinstance(out[0], ModelRequest)
        assert isinstance(out[0].parts[0], UserPromptPart)
        assert out[0].parts[0].content == "tell me a story"
        assert isinstance(out[1], ModelResponse)
        assert isinstance(out[1].parts[0], TextPart)
        assert out[1].parts[0].content.startswith("once upon a time")

    def test_preserves_original_history_in_order(self) -> None:
        prior = [
            ModelRequest(parts=[UserPromptPart(content="hi")]),
            ModelResponse(parts=[TextPart(content="hello")]),
        ]
        out = build_retry_history(
            original_history=prior,
            original_user_prompt="next question",
            failed_text="failed answer",
        )
        # prior + ModelRequest + ModelResponse = 4 entries
        assert len(out) == 4
        assert out[0] is prior[0]
        assert out[1] is prior[1]

    def test_does_not_mutate_input_history(self) -> None:
        """The caller's history list must be untouched — both callers
        (bridge `_publish_retry`, A2A retry orchestrator) treat their
        snapshot as immutable."""
        original: list = [ModelRequest(parts=[UserPromptPart(content="seed")])]
        original_len = len(original)
        build_retry_history(
            original_history=original,
            original_user_prompt="x",
            failed_text="y",
        )
        assert len(original) == original_len  # not mutated

    def test_works_with_empty_history(self) -> None:
        out = build_retry_history(
            original_history=[],
            original_user_prompt="prompt",
            failed_text="failure",
        )
        assert len(out) == 2

    def test_accepts_tuple_input(self) -> None:
        """``PendingEntry.message_history`` is a tuple (frozen
        dataclass); the helper must accept any Sequence."""
        prior = (ModelRequest(parts=[UserPromptPart(content="a")]),)
        out = build_retry_history(
            original_history=prior,
            original_user_prompt="b",
            failed_text="c",
        )
        assert len(out) == 3


# ---------------------------------------------------------------------------
# chunk_split
# ---------------------------------------------------------------------------


class TestChunkSplit:
    def test_empty_returns_empty_list(self) -> None:
        assert chunk_split("") == []

    def test_short_returns_single_chunk(self) -> None:
        assert chunk_split("hello") == ["hello"]

    def test_long_splits_into_multiple(self) -> None:
        text = "x" * 5000
        chunks = chunk_split(text)
        assert len(chunks) >= 3
        for c in chunks:
            assert len(c) <= CHUNK_SAFE_SIZE

    def test_prefers_paragraph_boundary(self) -> None:
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}\n\n{second}"
        chunks = chunk_split(text)
        assert len(chunks) == 2
        assert chunks[0] == first
        assert chunks[1] == second

    def test_falls_back_to_line_boundary(self) -> None:
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}\n{second}"
        chunks = chunk_split(text)
        assert len(chunks) == 2
        assert chunks[0] == first
        assert chunks[1] == second

    def test_falls_back_to_sentence_boundary(self) -> None:
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}. {second}"
        chunks = chunk_split(text)
        assert len(chunks) == 2
        # Sentence cut keeps the period in the first chunk.
        assert chunks[0].endswith(".")

    def test_hard_cut_when_no_boundary(self) -> None:
        text = "x" * 4000  # no boundaries at all
        chunks = chunk_split(text)
        assert len(chunks) >= 3
        for c in chunks:
            assert len(c) <= CHUNK_SAFE_SIZE

    def test_preserves_total_content_modulo_boundary_whitespace(self) -> None:
        """All non-boundary chars survive across chunks."""
        first = "a" * 1500
        second = "b" * 1500
        text = f"{first}. {second}"
        chunks = chunk_split(text)
        joined = "".join(chunks)
        # The boundary characters (". ") get split off but the
        # alphabetic content is preserved exactly.
        assert "a" * 1500 in joined
        assert "b" * 1500 in joined


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_rate_limited_is_drop(self) -> None:
        """``discord.RateLimited`` has no HTTP status — discord.py's
        internal backoff has already given up. Retrying the LLM with
        new content cannot solve a rate-limit problem."""
        err = discord.RateLimited(retry_after=5.0)
        assert classify_error(err) == "drop"

    def test_non_http_exception_is_drop(self) -> None:
        """A ``DiscordException`` subclass that's neither
        ``HTTPException`` nor ``RateLimited`` (e.g.
        ``ConnectionClosed``) cannot have ``status`` accessed safely;
        fall through to drop rather than crash."""

        class CustomDiscordError(discord.DiscordException):
            pass

        assert classify_error(CustomDiscordError("???")) == "drop"

    @pytest.mark.parametrize("status", [401, 403, 404, 429])
    def test_non_agent_fixable_statuses_are_drop(self, status: int) -> None:
        err = _http_exc(discord.HTTPException, status)
        assert classify_error(err) == "drop"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_is_transient(self, status: int) -> None:
        err = _http_exc(discord.HTTPException, status)
        assert classify_error(err) == "transient"

    @pytest.mark.parametrize("status", [400, 422])
    def test_other_4xx_is_agent_fixable(self, status: int) -> None:
        """400 (content-too-long, invalid embed) and 422 (validation)
        are the canonical agent-fixable cases — the LLM can adjust its
        next reply to satisfy the constraint."""
        err = _http_exc(discord.HTTPException, status)
        assert classify_error(err) == "agent_fixable"

    def test_400_content_too_long_is_agent_fixable(self) -> None:
        """The specific case that motivated retry-with-feedback: Discord
        rejects content > 2000 chars with a 400 / code 50035."""
        err = _http_exc(discord.HTTPException, 400, code=50035)
        assert classify_error(err) == "agent_fixable"
