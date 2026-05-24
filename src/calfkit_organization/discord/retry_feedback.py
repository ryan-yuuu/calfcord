"""Shared retry-with-feedback policy for Discord-rejected LLM replies.

Two callers post LLM-generated text to Discord and both want the same
"if Discord rejects it, tell the LLM and let it adapt" loop:

* :mod:`calfkit_organization.bridge.outbox` — channel replies, posted
  fire-and-forget via ``client.invoke_node`` on retry.
* :mod:`calfkit_organization.tools.private_chat` — A2A audit
  projections, posted synchronously inside the caller's
  ``execute_node`` RPC on retry.

The two orchestration mechanisms cannot share their transport (one is
async-via-Kafka-consumer, the other is await-inside-RPC), but they DO
share their **policy** and their **content construction**: the retry
budget, the error classification, the ``<system-reminder>`` wording,
the ``message_history`` shape, and the chunk-split fallback. This
module is the single source of truth for those.

Process boundary: this module sits beneath both consumers and depends
only on stdlib, ``discord``, and ``pydantic_ai`` message types — no
project imports — so both ``bridge`` and ``tools`` can import it
without inducing a cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

import discord
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

# --- Constants ----------------------------------------------------------------

MAX_REPLY_RETRY_ATTEMPTS: Final[int] = 2
"""Number of LLM retries triggered after the original post failure.
Total LLM attempts before falling back to chunk-splitting =
1 (original) + MAX_REPLY_RETRY_ATTEMPTS = 3. Picked at 2 because:

* Discord 4xx errors (length, formatting) are usually self-correcting
  on attempt 2 once the LLM is told the problem.
* Each retry is a full LLM round-trip (~5-15s); 2 retries adds at most
  ~30s before chunked fallback. 3+ retries makes the user wait too
  long with no visible signal.
* Bounded LLM cost: pathological retry loops cost ~3x a normal
  invocation, never unbounded."""

CHUNK_SAFE_SIZE: Final[int] = 1990
"""Max chars per chunk in the chunk-split fallback. Discord's hard
content limit is 2000; the 10-char safety buffer absorbs the occasional
emoji / encoding surprise that tips a 1999-char string over the limit."""

NON_AGENT_FIXABLE_STATUSES: Final[frozenset[int]] = frozenset({
    401,   # unauthorized — bot token invalid
    403,   # forbidden — missing Manage Webhooks / View Channel / etc.
    404,   # not found — channel or webhook deleted
    429,   # rate limited — discord.py already retried internally
})
"""Discord HTTP statuses where retrying the agent with a revised reply
cannot possibly succeed. These are infrastructure / permission errors
that require operator action, not agent content adjustment. Callers
log WARN and drop the reply on these statuses.

5xx is NOT in this set — :func:`classify_error` returns ``"transient"``
for those, because the inner per-send retry that callers already do
smooths transient 5xx; only a *persistent* 5xx reaches this layer.

:class:`discord.RateLimited` is also classified as ``"drop"`` even
though it has no HTTP ``status`` (it inherits from
:class:`discord.DiscordException` directly); see :func:`classify_error`."""

_RETRY_REMINDER_OVERRIDES: dict[tuple[int, int], str] = {}
"""Per-(HTTP status, JSON error code) overrides for the retry-reminder
text. Empty by default — the generic template surfaces Discord's own
error message to the LLM, which modern frontier models reliably parse
and adapt to. Populate only when empirical evidence shows the LLM
needs more pointed guidance for a specific Discord error code.

Format: ``(status, code): "Custom reminder body."`` Both status and
code are concrete integers — no wildcard support in v1."""


# --- Error classification -----------------------------------------------------


def classify_error(
    error: discord.DiscordException,
) -> Literal["drop", "transient", "agent_fixable"]:
    """The single source of truth for what to do with a failed Discord post.

    Returns:
        * ``"drop"`` — non-agent-fixable infra (statuses in
          :data:`NON_AGENT_FIXABLE_STATUSES`, :class:`discord.RateLimited`,
          or a :class:`discord.DiscordException` that's neither
          :class:`HTTPException` nor :class:`RateLimited`). Caller should
          log+abandon — the LLM cannot fix permission, auth, or
          rate-limit issues by re-thinking its reply.
        * ``"transient"`` — 5xx that survived the sender's inner retry.
          Caller should log+abandon (bridge) or raise (A2A); a
          content-retry won't fix a Discord-side outage.
        * ``"agent_fixable"`` — 4xx the LLM can plausibly correct
          (content-too-long, invalid embed, banned formatting). Caller
          should fire a retry-with-feedback.
    """
    if isinstance(error, discord.RateLimited):
        return "drop"
    if not isinstance(error, discord.HTTPException):
        return "drop"
    if error.status in NON_AGENT_FIXABLE_STATUSES:
        return "drop"
    if error.status >= 500:
        return "transient"
    return "agent_fixable"


# --- Retry envelope construction ---------------------------------------------


def build_retry_reminder(
    error: discord.HTTPException,
    failed_text: str,
) -> str:
    """Build the system-reminder-tagged user message for an agent retry.

    Generic by design: the LLM sees the literal Discord error text in
    a ``<system-reminder>`` block and is trusted to adapt. Modern
    frontier LLMs reliably parse Discord's own error strings (e.g.
    ``"Must be 2000 or fewer in length"``, ``"Cannot send an empty
    message"``, ``"Invalid embed URL"``) and adjust their next reply
    accordingly. No per-error-code customization is needed in v1; the
    override map slot at :data:`_RETRY_REMINDER_OVERRIDES` exists for
    future empirical cases where the generic message is insufficient.

    The ``<system-reminder>`` tag pattern is a convention frontier
    models trained with system-reminder-style data typically treat as
    out-of-band metadata even though it occupies a ``user``-role slot
    on the wire. The explicit "Do NOT mention this error" instruction
    inside the reminder body is the actual enforcement mechanism;
    the tag wrapper is the visual cue that helps the model recognize
    the convention.

    Args:
        error: The :class:`discord.HTTPException` raised by the
            persona-sender. The status, code, and body text are all
            surfaced to the LLM.
        failed_text: The exact reply text the agent emitted that
            Discord rejected. Used in the reminder to give the LLM
            length-context (``"length: 3187 chars"``) without
            duplicating the full failed content (which appears
            separately in the retry envelope's ``message_history``
            as a ``ModelResponse``).

    Returns:
        A string suitable to pass as ``user_prompt`` to
        :meth:`Client.invoke_node` (bridge) or
        :meth:`Client.execute_node` (A2A) for the retry envelope.
    """
    override = _RETRY_REMINDER_OVERRIDES.get((error.status, error.code))
    if override is not None:
        body = override
    else:
        # ``discord.HTTPException.text`` is the raw JSON-body text from
        # Discord (e.g. ``"Invalid Form Body\nIn content: Must be 2000
        # or fewer in length."``). Falls back to ``str(error)`` which
        # is discord.py's formatted ``"status: code: text"``.
        raw = error.text or str(error)
        body = (
            f"Your previous reply (length: {len(failed_text)} chars) was "
            f"rejected by Discord. The exact error:\n\n"
            f"  HTTP {error.status}: {raw}\n\n"
            f"Please respond again to the user's original question, "
            f"addressing the specific issue above. For example, if the "
            f"content was too long, be more concise; if it contained "
            f"banned formatting, rephrase without it."
        )
    return (
        "<system-reminder>\n"
        f"{body}\n\n"
        "This reminder is system-level — the user does NOT see it. "
        "Do NOT mention this error or that you are retrying.\n"
        "</system-reminder>"
    )


def build_retry_history(
    *,
    original_history: Sequence[ModelMessage],
    original_user_prompt: str,
    failed_text: str,
) -> list[ModelMessage]:
    """Build the retry envelope's ``message_history``.

    Used identically by both callers: original history, then the user's
    original prompt as ``ModelRequest``, then the LLM's rejected
    attempt as ``ModelResponse``. The new ``<system-reminder>``
    user prompt is passed separately by the caller (as the retry
    invocation's ``user_prompt``) — pydantic-ai's
    ``_clean_message_history`` then merges any adjacent same-role
    parts before the provider mapper sees the list, so the LLM ends
    up with a well-formed alternating conversation that ends on the
    system-reminder user turn.
    """
    return [
        *original_history,
        ModelRequest(parts=[UserPromptPart(content=original_user_prompt)]),
        ModelResponse(parts=[TextPart(content=failed_text)]),
    ]


# --- Chunk-split fallback content helper -------------------------------------


def chunk_split(text: str, *, max_chars: int = CHUNK_SAFE_SIZE) -> list[str]:
    """Split ``text`` into pieces each ≤ ``max_chars`` for posting as
    consecutive Discord messages.

    Boundary search is greedy from the largest unit down: paragraph
    (``"\\n\\n"``) → line (``"\\n"``) → sentence (``". "``) → word
    (``" "``) → hard cut. The search refuses to split earlier than
    ``max_chars // 2`` so we don't produce a tiny first chunk
    followed by a huge tail.

    Each chunk is right-stripped of trailing whitespace. The split
    preserves all non-boundary characters — joining chunks back with
    the boundary that produced each cut reconstructs (modulo
    boundary whitespace) the original text.

    Args:
        text: The full text to split. May be empty (returns ``[]``).
        max_chars: Maximum characters per chunk. Defaults to
            :data:`CHUNK_SAFE_SIZE` (1990) — Discord's 2000-char
            limit with a 10-char safety buffer.

    Returns:
        A list of chunks in original order. If ``text`` already fits,
        returns ``[text]``. An empty string returns ``[]``.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    min_split = max(1, max_chars // 2)

    while remaining:
        if len(remaining) <= max_chars:
            stripped = remaining.rstrip()
            if stripped:
                chunks.append(stripped)
            break

        candidate = remaining[:max_chars]
        cut_at = -1
        # Prefer larger structural boundaries.
        for separator in ("\n\n", "\n", ". ", " "):
            idx = candidate.rfind(separator)
            if idx >= min_split:
                cut_at = idx + len(separator)
                break

        if cut_at < 0:
            # No good boundary found; hard cut at max_chars.
            cut_at = max_chars

        chunk = remaining[:cut_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut_at:]

    return chunks
