"""Provider-integration sanity tests for the conversation-history feature.

Gated on real API keys. These tests do not run in normal CI; they exist
as a tripwire so a future pydantic-ai release that removes
:func:`_clean_message_history` (the auto-merge of adjacent same-role
messages, see ``calfkit/_vendor/pydantic_ai/_agent_graph.py:1386``)
surfaces as a clear test failure rather than a silent production bug.

Why this matters: the design intentionally does NOT merge consecutive
:class:`ModelRequest`s inside :func:`build_message_history` (see
``bridge/history.py`` module + function docstrings). The correctness of the
"boundary between history and staged user_prompt is well-formed" invariant
rests entirely on pydantic-ai's auto-merge running at one of the two call
sites (``_agent_graph.py`` lines 213 and 526).

If `_clean_message_history` is ever removed / changed:
    - Anthropic rejects with HTTP 400 ("messages must alternate")
    - OpenAI silently tolerates (no error, but the assistant may produce
      lower-quality output)

Either way, our unit tests in `test_history.py` would still pass (they're
unit-scoped). This file is the live alarm.

The tests build a canonical history via :func:`build_message_history` whose
tail ENDS in multiple consecutive ``ModelRequest``s — a shape that
pydantic-ai's auto-merge must consolidate before sending to the provider —
and feed it to a real ``pydantic_ai.Agent.run``. If the auto-merge silently
disappears in an upstream release, the Anthropic test fails with a
``400 messages must alternate`` (or equivalent); the OpenAI test still passes
but the regression is half-detected (model output quality may degrade).

Run manually::

    OPENAI_API_KEY=... ANTHROPIC_API_KEY=... \
        uv run pytest tests/bridge/test_history_provider_integration.py -v -m integration
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
)

from calfcord.bridge.history import HistoryRecord, build_message_history

pytestmark = pytest.mark.integration


def _record(content: str, author: str, *, is_agent: bool = False) -> HistoryRecord:
    return HistoryRecord(
        message_id=1,
        created_at=datetime.now(UTC),
        content=content,
        author_display_name=author,
        is_agent=is_agent,
    )


def _history_with_adjacent_users() -> list:
    """Build a canonical history whose tail is multiple consecutive
    ``ModelRequest`` entries (the case pydantic-ai's auto-merge must
    consolidate before the provider mapper sees it).
    """
    records = [
        _record("can you help me?", "ryan"),
        _record("sure, what do you need?", "Scribe", is_agent=True),
        _record("planning a meeting", "ryan"),
        _record("tuesday afternoon", "ryan"),  # consecutive user
        _record("also need help with the prep", "ryan"),  # consecutive user
    ]
    return build_message_history(records)


def _has_anthropic() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _has_openai() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _assert_history_has_adjacent_user_requests(history: list) -> None:
    """Sanity check: confirm the built history actually has the
    adjacent-same-role shape this test is supposed to exercise.
    Without this, a subtle change to ``build_message_history`` could make
    the test vacuously pass.
    """
    request_runs = 0
    max_run = 0
    for m in history:
        if isinstance(m, ModelRequest):
            request_runs += 1
            max_run = max(max_run, request_runs)
        else:
            request_runs = 0
    assert max_run >= 2, (
        "test invariant: built history must contain >=2 consecutive "
        "ModelRequest entries to exercise pydantic-ai's auto-merge path; "
        f"got max consecutive-request run of {max_run}"
    )


@pytest.mark.skipif(not _has_anthropic(), reason="ANTHROPIC_API_KEY not set")
async def test_pydantic_ai_anthropic_auto_merges_adjacent_user_messages() -> None:
    """Construct a pydantic-ai Agent with an Anthropic model and send
    it a ``message_history`` whose tail has multiple consecutive
    ``ModelRequest`` entries. If pydantic-ai's ``_clean_message_history``
    auto-merge still runs, the call succeeds. If it's removed, Anthropic
    rejects with a 400 ``messages must alternate`` error.

    This is the real alarm — exercising the actual production code path.
    """
    from calfkit._vendor.pydantic_ai import Agent
    from calfkit._vendor.pydantic_ai.models.anthropic import AnthropicModel

    history = _history_with_adjacent_users()
    _assert_history_has_adjacent_user_requests(history)

    model = AnthropicModel("claude-haiku-4-5")
    agent: Agent = Agent(
        model=model,
        system_prompt="You are Scribe. Be concise.",
    )

    # If pydantic-ai stops auto-merging, the underlying anthropic API
    # call raises a ``BadRequestError`` (``messages must alternate``).
    # We assert no exception — the run succeeds and we get text out.
    result = await agent.run(
        "what time should we meet?",
        message_history=history,
    )
    assert result.output, "Anthropic returned an empty response"


@pytest.mark.skipif(not _has_openai(), reason="OPENAI_API_KEY not set")
async def test_pydantic_ai_openai_handles_adjacent_user_messages() -> None:
    """OpenAI tolerates adjacent same-role messages at the API layer,
    but pydantic-ai's auto-merge still runs uniformly. This test pins
    the success path against OpenAI; a failure here is unlikely to be
    pydantic-ai (OpenAI accepts the raw shape anyway) — more likely
    a misconfiguration."""
    from calfkit._vendor.pydantic_ai import Agent
    from calfkit._vendor.pydantic_ai.models.openai import OpenAIChatModel

    history = _history_with_adjacent_users()
    _assert_history_has_adjacent_user_requests(history)

    model = OpenAIChatModel("gpt-4o-mini")
    agent: Agent = Agent(
        model=model,
        system_prompt="You are Scribe. Be concise.",
    )

    result = await agent.run(
        "what time should we meet?",
        message_history=history,
    )
    assert result.output, "OpenAI returned an empty response"
