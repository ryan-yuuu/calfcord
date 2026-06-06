"""Guard: calfkit 0.5.4's always-on in-loop POV projection is a transparent
no-op for calfcord's bridge-side, pre-projected wire histories.

calfkit 0.5.4 ships Feature A ([#154]): the agent loop now projects an
agent-POV view over its cumulative ``message_history`` on every invocation.
That projection is **explicitly out of scope** for calfcord's adoption (design
doc §9) because calfcord's projection is bridge-side and fetch-per-turn
(:func:`calfcord.bridge.history.project_history`) and depends on bridge-only
stages that cannot move into the agent loop without breaking the distributed
deploy invariants.

The 0.5.4 bump is only *safe* because calfkit's projection is a verified
**transparent no-op** on calfcord's output. calfkit detects "multi-participant"
(and re-roles other participants into attributed ``ModelRequest`` turns,
changing the bytes the model sees) iff two or more **``ModelResponse.name``**
values or two or more **``UserPromptPart.name``** values appear in the history
(``calfkit/nodes/_projection.py:56-59``). calfcord never populates either
``name`` field: self turns become a bare ``ModelResponse`` and human/other turns
carry attribution **in the content** (the ``<author>`` prefix), not in the
``name`` field. So calfkit's multi-participant detection can never trigger on a
calfcord-projected history, calfkit takes its transparent pass-through branch,
and the model input stays byte-identical to pre-0.5.4.

This module pins that contract two ways:

1. **Structural** — every ``ModelResponse`` and every ``UserPromptPart`` that
   :func:`project_history` emits has ``name is None``. This is the load-bearing
   property; if a future projection change started stamping ``name``, calfkit's
   detection could engage and silently mutate model input.
2. **End-to-end** — feeding calfcord's output through calfkit's own ``project``
   returns a byte-identical history (no re-roling, no prefixing), which is the
   actual guarantee the structural property exists to ensure.

[#154]: https://github.com/calf-ai/calfkit-sdk/issues/154
"""

from __future__ import annotations

from datetime import UTC, datetime

from calfkit._vendor.pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)
from calfkit.nodes._projection import project

from calfcord.bridge.history import HistoryRecord, project_history


def _record(
    *,
    message_id: int = 1,
    content: str = "hi",
    author_display_name: str = "ryan",
    author_agent_id: str | None = None,
) -> HistoryRecord:
    return HistoryRecord(
        message_id=message_id,
        created_at=datetime.now(UTC),
        content=content,
        author_display_name=author_display_name,
        author_agent_id=author_agent_id,
    )


def _multi_participant_records() -> list[HistoryRecord]:
    """A history with multiple distinct humans AND multiple distinct agents.

    This is the exact shape that *would* trip calfkit's multi-participant
    detection if calfcord stamped ``name`` — two humans (ryan, alice), two
    agents (scribe, conan). The guard's whole point is that calfcord's
    projection keeps ``name`` empty even here.
    """
    return [
        _record(message_id=1, content="kick it off", author_display_name="ryan"),
        _record(
            message_id=2,
            content="on it",
            author_display_name="Scribe",
            author_agent_id="scribe",
        ),
        _record(message_id=3, content="me too?", author_display_name="alice"),
        _record(
            message_id=4,
            content="sure",
            author_display_name="Conan",
            author_agent_id="conan",
        ),
        _record(message_id=5, content="thanks all", author_display_name="ryan"),
    ]


class TestProjectionNameFieldNeverPopulated:
    """Structural guard: no emitted message ever carries a populated ``name``."""

    def test_response_name_is_none_self_pov(self) -> None:
        out = project_history(_multi_participant_records(), self_agent_id="scribe")
        responses = [m for m in out if isinstance(m, ModelResponse)]
        # scribe self-classified at least once → there is a ModelResponse to check.
        assert responses, "expected at least one self ModelResponse for scribe"
        assert all(m.name is None for m in responses)

    def test_user_prompt_part_name_is_none(self) -> None:
        out = project_history(_multi_participant_records(), self_agent_id="scribe")
        prompt_parts = [
            p
            for m in out
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        ]
        assert prompt_parts, "expected attributed human/other UserPromptParts"
        assert all(p.name is None for p in prompt_parts)

    def test_router_pov_none_also_leaves_name_unpopulated(self) -> None:
        """The router projects with ``self_agent_id=None`` (everything is a
        ModelRequest); that path must also never stamp ``name``."""
        out = project_history(_multi_participant_records(), self_agent_id=None)
        for m in out:
            if isinstance(m, ModelResponse):
                assert m.name is None
            elif isinstance(m, ModelRequest):
                assert all(
                    p.name is None
                    for p in m.parts
                    if isinstance(p, UserPromptPart)
                )

    def test_author_attribution_lives_in_content_not_name(self) -> None:
        """The ``<author>`` attribution is carried in content (so the model
        still sees who spoke) precisely *because* it is not in ``name`` (so
        calfkit's detection stays blind to it). Pin both halves together."""
        out = project_history(_multi_participant_records(), self_agent_id="scribe")
        ryan_req = next(
            m
            for m in out
            if isinstance(m, ModelRequest)
            and isinstance(m.parts[0], UserPromptPart)
            and "kick it off" in str(m.parts[0].content)
        )
        part = ryan_req.parts[0]
        assert isinstance(part, UserPromptPart)
        assert "<ryan>" in str(part.content)  # attribution is in content
        assert part.name is None  # ...and NOT in the name field


class TestCalfkitProjectionIsTransparentNoOp:
    """End-to-end guard: feeding calfcord's output through calfkit's own
    ``project`` returns a byte-identical history — the multi-participant
    branch never engages, so model input is unchanged by the 0.5.4 bump."""

    def test_calfkit_project_is_byte_identical_for_each_pov(self) -> None:
        records = _multi_participant_records()
        for viewer in ("scribe", "conan"):
            calfcord_out = project_history(records, self_agent_id=viewer)
            # calfkit projects POV inside its agent loop over this history.
            calfkit_out = project(list(calfcord_out), viewer)
            assert calfkit_out == calfcord_out, (
                f"calfkit projection mutated calfcord's history for viewer={viewer}; "
                "the multi-participant branch engaged (a name field leaked) — "
                "the 0.5.4 no-op assumption is broken"
            )

    def test_calfkit_does_not_reroll_or_prefix(self) -> None:
        """A focused restatement: roles and contents are preserved verbatim."""
        records = _multi_participant_records()
        calfcord_out = project_history(records, self_agent_id="scribe")
        calfkit_out = project(list(calfcord_out), "scribe")

        assert [type(m) for m in calfkit_out] == [type(m) for m in calfcord_out]
        for before, after in zip(calfcord_out, calfkit_out, strict=True):
            assert before.parts == after.parts
