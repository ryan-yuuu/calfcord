"""Unit tests for the on-demand step-transcript view button.

Covers the three pieces of :mod:`calfkit_organization.bridge.steps_toggle`:

* :func:`render_steps` — round-trips a ``ModelMessagesTypeAdapter`` blob
  through ``_render_delta`` and returns the FULL untruncated text + count;
* :func:`build_toggle_button` — label / custom_id / style of the
  view-steps button;
* :class:`StepsToggleView._show_steps` — the defer-then-ephemeral-followup
  flow against a fake ``discord.Interaction`` and a real in-memory
  :class:`TranscriptStore`: a present row sends the steps content
  ephemerally, an oversized transcript attaches a ``discord.File``, and a
  missing row sends the ephemeral "no longer available" followup. The
  agent's reply is NEVER edited.

discord.py's gateway, Kafka, and the LLM stack are all mocked out. The
repo runs under ``asyncio_mode = "auto"`` (see ``pyproject.toml``), so
``async def test_...`` functions run without an explicit marker.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

from calfkit_organization.bridge.steps_toggle import (
    _DISCORD_MESSAGE_LIMIT,
    _TOGGLE_CUSTOM_ID,
    StepsToggleView,
    build_toggle_button,
    render_steps,
)
from calfkit_organization.bridge.transcripts import TranscriptRow, TranscriptStore

_FINAL_MESSAGE_ID = 90001
_REPLY_TEXT = "It's 18 degrees in Tokyo."


def _delta_json() -> str:
    """Serialize a known turn slice: preamble text + tool call + return.

    Renders to THREE step parts (one ``TextPart``, one ``ToolCallPart``,
    one ``ToolReturnPart``).
    """
    delta = [
        ModelResponse(
            parts=[
                TextPart(content="Let me check."),
                ToolCallPart(tool_name="weather", args={"c": "Tokyo"}, tool_call_id="t1"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name="weather", content="18C", tool_call_id="t1")]),
    ]
    return ModelMessagesTypeAdapter.dump_json(delta).decode()


def _big_delta_json(*, parts: int = 4, chars_per_return: int = 1000) -> str:
    """Serialize a turn slice whose JOINED render exceeds the Discord cap.

    ``_render_delta`` caps each individual part at ``STEP_CONTENT_MAX_CHARS``
    (1500), so a single oversized return can't push the total over 2000 on
    its own. We therefore use several tool-call/return pairs whose combined
    render is well over 2000 chars while each part stays under the per-part
    cap — exercising the file-attachment branch of the callback. ``X * N``
    is a recognizable marker we can assert survives in full.
    """
    delta: list[object] = []
    for i in range(parts):
        delta.append(
            ModelResponse(parts=[ToolCallPart(tool_name="dump", args={"n": i}, tool_call_id=f"t{i}")]),
        )
        delta.append(
            ModelRequest(
                parts=[ToolReturnPart(tool_name="dump", content="X" * chars_per_return, tool_call_id=f"t{i}")],
            ),
        )
    return ModelMessagesTypeAdapter.dump_json(delta).decode()


# --- render_steps -------------------------------------------------------------


def test_render_steps_returns_full_untruncated_text_and_count() -> None:
    text, count = render_steps(_delta_json())
    assert count == 3
    # Each part rendered and joined with a blank line, in order — nothing
    # truncated.
    assert "Let me check." in text
    assert "**Calling `weather`**" in text
    assert '{"c":"Tokyo"}' in text
    assert "**`weather` returned**" in text
    assert "18C" in text
    # Blank-line join between the three parts.
    assert text.count("\n\n") >= 2
    # No truncation marker — the full render is returned regardless of size.
    assert "truncated" not in text


def test_render_steps_does_not_truncate_large_render() -> None:
    # render_steps adds NO truncation of its own. Each part stays under the
    # per-part render cap, so the whole render comes back intact — and the
    # joined total exceeds the Discord message cap (driving the file branch
    # in the callback). One render per tool-call part + one per return part.
    text, count = render_steps(_big_delta_json(parts=4, chars_per_return=1000))
    assert count == 8
    # All 4000 marker chars survive (4 returns of 1000, each under the cap).
    assert text.count("X") == 4000
    assert len(text) > _DISCORD_MESSAGE_LIMIT
    # render_steps itself never appends a truncation marker.
    assert "(truncated)" not in text


# --- build_toggle_button ------------------------------------------------------


def test_build_toggle_button_singular_label() -> None:
    button = build_toggle_button(1)
    assert button.label == "⤵ 1 step"
    assert button.custom_id == _TOGGLE_CUSTOM_ID
    assert button.style == discord.ButtonStyle.secondary


def test_build_toggle_button_plural_label() -> None:
    assert build_toggle_button(0).label == "⤵ 0 steps"
    assert build_toggle_button(3).label == "⤵ 3 steps"


# --- StepsToggleView callback -------------------------------------------------


@pytest.fixture
async def store(tmp_path: pathlib.Path) -> TranscriptStore:
    """A real in-memory-on-disk store seeded with one transcript row."""
    s = TranscriptStore(tmp_path / "state" / "transcripts.sqlite3")
    await s.connect()
    await s.write_turn(
        TranscriptRow(
            correlation_id="corr-1",
            conversation_key="chan-100",
            agent_id="scheduler",
            final_message_id=str(_FINAL_MESSAGE_ID),
            delta_json=_delta_json(),
            created_at=1000,
        )
    )
    return s


def _fake_interaction(*, message_id: int = _FINAL_MESSAGE_ID) -> MagicMock:
    """Build a fake ``discord.Interaction`` for a component click.

    ``response.defer`` / ``followup.send`` are AsyncMocks; ``message.id``
    carries the clicked reply's id the callback looks the row up by. The
    callback never reads ``message.content`` (the reply is never edited),
    so it isn't set.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock(return_value=None)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock(return_value=None)
    interaction.message = MagicMock()
    interaction.message.id = message_id
    return interaction


def _button(view: StepsToggleView) -> discord.ui.Button:
    """The single decorator-defined button on the view (callback target)."""
    items = [item for item in view.children if isinstance(item, discord.ui.Button)]
    assert len(items) == 1
    return items[0]


async def test_callback_sends_steps_as_ephemeral_message(store: TranscriptStore) -> None:
    """A present row → ephemeral defer, then an ephemeral followup carrying
    the rendered steps content. The reply message is never edited."""
    try:
        view = StepsToggleView(store)
        button = _button(view)
        interaction = _fake_interaction()
        await button.callback(interaction)

        # Component defer must carry BOTH thinking=True and ephemeral=True;
        # ephemeral alone is silently dropped by discord.py 2.7.1.
        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["ephemeral"] is True
        # The steps content is inlined (no file) since it fits the cap.
        assert kwargs["file"] is discord.utils.MISSING
        content = kwargs["content"]
        assert "**Calling `weather`**" in content
        assert "18C" in content
    finally:
        await store.close()


async def test_callback_attaches_file_when_transcript_too_long(tmp_path: pathlib.Path) -> None:
    """A >2000-char render → the FULL transcript is attached as a
    ``discord.File`` (nothing truncated), still ephemeral."""
    s = TranscriptStore(tmp_path / "state" / "transcripts.sqlite3")
    await s.connect()
    big_message_id = 70707
    await s.write_turn(
        TranscriptRow(
            correlation_id="corr-big",
            conversation_key="chan-100",
            agent_id="scheduler",
            final_message_id=str(big_message_id),
            delta_json=_big_delta_json(parts=4, chars_per_return=1000),
            created_at=1000,
        )
    )
    try:
        view = StepsToggleView(s)
        button = _button(view)
        interaction = _fake_interaction(message_id=big_message_id)
        await button.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["ephemeral"] is True
        # File attached; content omitted (left at MISSING).
        attached = kwargs["file"]
        assert isinstance(attached, discord.File)
        assert attached.filename == "steps.md"
        assert kwargs["content"] is discord.utils.MISSING
        # The attached bytes are the FULL render — all four 1000-char
        # returns are present in their entirety, not truncated.
        attached.fp.seek(0)
        payload = attached.fp.read()
        assert payload.count(b"X") == 4000
        assert len(payload) > _DISCORD_MESSAGE_LIMIT
    finally:
        await s.close()


async def test_callback_missing_row_sends_ephemeral_followup(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(tmp_path / "state" / "transcripts.sqlite3")
    await store.connect()
    try:
        view = StepsToggleView(store)
        button = _button(view)

        # No row seeded → the lookup misses for any message id.
        interaction = _fake_interaction(message_id=42)
        await button.callback(interaction)

        # Deferred ephemerally first, then the "no longer available"
        # ephemeral followup.
        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        assert kwargs["ephemeral"] is True
        assert kwargs["content"] == "Step details are no longer available."
    finally:
        await store.close()


async def test_callback_swallows_followup_http_exception(store: TranscriptStore) -> None:
    """A failed followup is logged and swallowed, never raised out of the callback."""
    try:
        view = StepsToggleView(store)
        button = _button(view)
        interaction = _fake_interaction()
        response = MagicMock(status=500, reason="boom")
        interaction.followup.send = AsyncMock(side_effect=discord.HTTPException(response, "send failed"))
        # Must not raise.
        await button.callback(interaction)
        interaction.followup.send.assert_awaited_once()
    finally:
        await store.close()


async def test_callback_aborts_when_defer_fails(store: TranscriptStore) -> None:
    """A failed defer aborts the callback before any store read / followup —
    and never raises."""
    try:
        view = StepsToggleView(store)
        button = _button(view)
        interaction = _fake_interaction()
        response = MagicMock(status=500, reason="boom")
        interaction.response.defer = AsyncMock(side_effect=discord.HTTPException(response, "defer failed"))
        await button.callback(interaction)
        interaction.followup.send.assert_not_awaited()
    finally:
        await store.close()
