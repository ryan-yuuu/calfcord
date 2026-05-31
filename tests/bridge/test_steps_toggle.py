"""Unit tests for the inline step-transcript expand/collapse toggle.

Covers the three pieces of :mod:`calfkit_organization.bridge.steps_toggle`:

* :func:`render_steps` — round-trips a ``ModelMessagesTypeAdapter`` blob
  through ``_render_delta`` and truncates at the limit;
* :func:`build_toggle_button` — label / custom_id / style of the
  collapsed-state button;
* :class:`StepsToggleView._toggle` — the defer-first expand→collapse
  round-trip against a fake ``discord.Interaction`` and a real in-memory
  :class:`TranscriptStore`, plus the missing-row ephemeral followup.

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

from calfkit_organization.bridge import steps_toggle
from calfkit_organization.bridge.steps_toggle import (
    _EXPANDED_LABEL,
    _STEPS_SEP,
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


# --- render_steps -------------------------------------------------------------


def test_render_steps_round_trips_known_delta() -> None:
    text, count = render_steps(_delta_json(), limit=2000)
    assert count == 3
    # Each part rendered and joined with a blank line, in order.
    assert "Let me check." in text
    assert "**Calling `weather`**" in text
    assert '{"c":"Tokyo"}' in text
    assert "**`weather` returned**" in text
    assert "18C" in text
    # Blank-line join between the three parts.
    assert text.count("\n\n") >= 2


def test_render_steps_truncates_at_limit() -> None:
    # A small limit forces truncation; the result must fit and carry the
    # marker, while the count still reflects the true number of parts.
    text, count = render_steps(_delta_json(), limit=40)
    assert count == 3
    assert len(text) <= 40
    assert text.endswith("… (truncated)")


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


def _fake_interaction(*, content: str, message_id: int = _FINAL_MESSAGE_ID) -> MagicMock:
    """Build a fake ``discord.Interaction`` for a component click.

    ``response.defer`` / ``edit_original_response`` / ``followup.send`` are
    AsyncMocks; ``message.content`` / ``message.id`` carry the current reply
    state the callback reads.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock(return_value=None)
    interaction.edit_original_response = AsyncMock(return_value=None)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock(return_value=None)
    interaction.message = MagicMock()
    interaction.message.id = message_id
    interaction.message.content = content
    return interaction


def _button(view: StepsToggleView) -> discord.ui.Button:
    """The single decorator-defined button on the view (callback target)."""
    items = [item for item in view.children if isinstance(item, discord.ui.Button)]
    assert len(items) == 1
    return items[0]


async def test_callback_expands_then_collapses_round_trip(store: TranscriptStore) -> None:
    try:
        view = StepsToggleView(store)
        button = _button(view)

        # --- First click: EXPAND (content starts collapsed = just the reply).
        expand_it = _fake_interaction(content=_REPLY_TEXT)
        await button.callback(expand_it)

        # Defer was called first, exactly once, as a component update (no
        # thinking spinner).
        expand_it.response.defer.assert_awaited_once_with()
        expand_it.edit_original_response.assert_awaited_once()
        expand_kwargs = expand_it.edit_original_response.call_args.kwargs
        expanded_content = expand_kwargs["content"]
        # Content is reply + sep + steps.
        assert expanded_content.startswith(_REPLY_TEXT + _STEPS_SEP)
        assert "**Calling `weather`**" in expanded_content
        # Button relabelled to the hide form.
        expand_view = expand_kwargs["view"]
        expand_btn = _button_from_plain_view(expand_view)
        assert expand_btn.label == _EXPANDED_LABEL
        assert expand_btn.custom_id == _TOGGLE_CUSTOM_ID

        # --- Second click: COLLAPSE (feed the expanded content back in).
        collapse_it = _fake_interaction(content=expanded_content)
        await button.callback(collapse_it)

        collapse_it.response.defer.assert_awaited_once_with()
        collapse_it.edit_original_response.assert_awaited_once()
        collapse_kwargs = collapse_it.edit_original_response.call_args.kwargs
        # Collapsed back to exactly the original reply.
        assert collapse_kwargs["content"] == _REPLY_TEXT
        collapse_btn = _button_from_plain_view(collapse_kwargs["view"])
        # Relabelled to the collapsed N-steps form (3 parts in the seed).
        assert collapse_btn.label == "⤵ 3 steps"
        assert collapse_btn.custom_id == _TOGGLE_CUSTOM_ID
    finally:
        await store.close()


def _button_from_plain_view(view: discord.ui.View) -> discord.ui.Button:
    buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
    assert len(buttons) == 1
    return buttons[0]


async def test_callback_missing_row_sends_ephemeral_followup(tmp_path: pathlib.Path) -> None:
    store = TranscriptStore(tmp_path / "state" / "transcripts.sqlite3")
    await store.connect()
    try:
        view = StepsToggleView(store)
        button = _button(view)

        # No row seeded → the lookup misses for any message id.
        interaction = _fake_interaction(content=_REPLY_TEXT, message_id=42)
        await button.callback(interaction)

        # Deferred first, then the ephemeral "no longer available" followup;
        # the message is never edited.
        interaction.response.defer.assert_awaited_once_with()
        interaction.followup.send.assert_awaited_once()
        followup_kwargs = interaction.followup.send.call_args.kwargs
        assert followup_kwargs["ephemeral"] is True
        assert "no longer available" in interaction.followup.send.call_args.args[0]
        interaction.edit_original_response.assert_not_awaited()
    finally:
        await store.close()


async def test_callback_swallows_edit_http_exception(store: TranscriptStore) -> None:
    """A failed edit is logged and swallowed, never raised out of the callback."""
    try:
        view = StepsToggleView(store)
        button = _button(view)
        interaction = _fake_interaction(content=_REPLY_TEXT)
        response = MagicMock(status=500, reason="boom")
        interaction.edit_original_response = AsyncMock(side_effect=discord.HTTPException(response, "edit failed"))
        # Must not raise.
        await button.callback(interaction)
        interaction.edit_original_response.assert_awaited_once()
    finally:
        await store.close()


async def test_callback_expand_too_long_reply_shows_placeholder(store: TranscriptStore) -> None:
    """When the reply nearly fills the cap, the steps block degrades to a
    short placeholder rather than an empty/garbled render."""
    try:
        view = StepsToggleView(store)
        button = _button(view)
        long_reply = "x" * 1990  # leaves < _MIN_STEPS_BUDGET room after the sep
        interaction = _fake_interaction(content=long_reply)
        await button.callback(interaction)
        content = interaction.edit_original_response.call_args.kwargs["content"]
        assert content.startswith(long_reply + _STEPS_SEP)
        assert content.endswith(steps_toggle._STEPS_TOO_LONG_BLOCK)
    finally:
        await store.close()
