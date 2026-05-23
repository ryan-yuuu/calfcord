"""Channel-history fetching and agent-POV projection for agent invocations.

Three public exports:

* :class:`HistoryRecord` — JSON-serializable snapshot of one Discord message.
  Built by the fetcher at fetch time so identity resolution (webhook
  display_name → ``agent_id``) happens once and downstream consumers
  (router, fan-out, synthesized-in, assistants) don't need
  :class:`~calfkit_organization.bridge.registry.AgentRegistry` access.
* :class:`ChannelHistoryFetcher` — thin wrapper around
  :meth:`discord.abc.Messageable.history` with a per-channel TTL cache
  and graceful Discord-error degradation. Lives in the bridge process;
  the other deployments (router, agents, tools) never call this — they
  receive history through Kafka envelopes.
* :func:`project_history` — pure function that turns a list of
  :class:`HistoryRecord` into a ``list[ModelMessage]`` projected from a
  specific agent's POV (self → :class:`ModelResponse`; others →
  :class:`ModelRequest` with ``<author>`` prefix in
  :class:`UserPromptPart` content).

**Why projection deliberately does NOT merge adjacent same-role**
messages: pydantic-ai's :func:`_clean_message_history`
(``calfkit/_vendor/pydantic_ai/_agent_graph.py:1386``) auto-merges
adjacent same-type messages with compatible instructions before the
provider mapper sees the list. Verified at two call sites: the
``UserPromptNode`` (line 213) and ``ModelRequestNode._prepare_request``
(line 526) — both run before any ``model.request()`` call, on every
provider. Our constructed messages have ``instructions=None`` and no
provider metadata, so the merge conditions are always met. Doing our
own merge would be duplicate work.

**Why projection DOES drop leading**
:class:`~calfkit._vendor.pydantic_ai.messages.ModelResponse` entries:
pydantic-ai's ``_clean_message_history`` merges but never drops. If
the oldest fetched record is the agent's own webhook reply, the
projected history would start with a ``ModelResponse``, and Anthropic
rejects request bodies whose first message is ``assistant`` role. Drop
is iterative — we keep popping leading responses until we either hit
a request or the list is empty (the latter is fine; empty
``message_history`` is valid).

**Why projection DOES drop empty-content** records: Discord lets
through messages with empty ``content`` (system messages like
"ryan pinned a message", attachment-only posts). Pydantic-ai's
Anthropic mapper has a ``len(user_content_params) > 0`` guard at
``models/anthropic.py:740`` that silently skips empty user content,
and OpenAI accepts but burns tokens. Dropping at projection time is
cleaner and saves Kafka envelope bytes.

**Why the fetcher uses ``source_channel_id``** (not the wire's
``channel_id``): the bridge's normalizer flattens Discord threads to
their parent channel ID for *topic routing* (so all messages in a
thread group share one Kafka topic with the parent). For *history
fetching* we want the actual channel the message landed in — the
thread itself, not the parent. The wire's ``source_channel_id`` field
preserves that.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

import discord
from calfkit._vendor.pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from calfkit_organization.agents.identifier import AGENT_ID_PATTERN

if TYPE_CHECKING:
    # Top-level import would cycle: ``bridge.registry`` imports
    # ``router.definition``, ``router.fanout`` imports
    # ``_compat.invoke``, and ``_compat.invoke`` bottom-imports
    # :class:`HistoryRecord` for its ``model_rebuild``. Lazy import
    # via ``TYPE_CHECKING`` keeps the type hint without triggering the
    # cycle — at runtime the ``from __future__ import annotations``
    # directive above keeps ``AgentRegistry`` as a string.
    from calfkit_organization.bridge.registry import AgentRegistry

logger = logging.getLogger(__name__)


_DEFAULT_CACHE_TTL_SECONDS = 2.0
"""Default TTL for the per-channel fetch cache. Tuned to absorb router
fan-out bursts — a single ambient message fans out into N synthesized
slash invocations, all hitting :meth:`ChannelHistoryFetcher.fetch`
within a few hundred milliseconds with the same
``(source_channel_id, before_message_id, limit)`` key. 2 seconds is
long enough to coalesce the burst without serving meaningfully stale
data on the next user message."""

_DEFAULT_CACHE_MAX_ENTRIES = 100
"""Soft upper bound on cache size. The fetcher evicts LRU entries beyond
this. 100 channels-in-flight is far above any realistic concurrent
burst on a single bridge process — the bound is a safety net against
unbounded growth under pathological load, not a tuned operating point."""

_FORBIDDEN_LOG_DEDUP_MAX = 4096
"""Soft upper bound on the Forbidden-error log-dedup set.

Sized for channels, not for messages: ``_seen_message_ids`` at
:attr:`gateway.DiscordIngressGateway._seen_message_ids` is bounded
at :data:`gateway._SEEN_MESSAGE_IDS_CAPACITY` (1024) because message
ids churn naturally — old entries are inherently stale. Forbidden
state is *sticky*: a channel that lacks Read Message History today
lacks it tomorrow, so each channel's entry should persist long enough
to keep deduping. With a per-guild ceiling of ~500 channels (Discord
limit) plus threads, 4096 channels per bridge process covers any
realistic multi-guild deployment without unbounded growth."""

_DISCORD_HISTORY_MAX_LIMIT = 100
"""Discord's per-call REST cap for ``channel.history(limit=...)``. The
fetcher enforces this so a caller asking for more (e.g. via an
operator-edited frontmatter) doesn't surprise-fail at the Discord
layer — we cap silently and return up to 100 records."""


class HistoryRecord(BaseModel):
    """JSON-serializable snapshot of one Discord message.

    Built once by :class:`ChannelHistoryFetcher` at fetch time. Downstream
    consumers (router, fan-out, synthesized-in, assistant invocations)
    receive these in :attr:`MetadataEnvelope.history` or via
    :class:`BridgeIngress.handle`'s ``prefetched_history`` kwarg, and
    pass them straight to :func:`project_history` without needing
    registry access of their own.

    Fields are kept minimal: only what :func:`project_history` reads.
    Adding attachments/embeds support is a v2+ concern (token budget
    matters more than richer content at v1).
    """

    model_config = ConfigDict(frozen=True)

    message_id: int
    """Discord message id. Carried for potential future use (session
    boundary detection, edit/delete event correlation) — currently not
    consumed by :func:`project_history`."""

    created_at: datetime
    """Original message creation time. tz-aware (Discord returns UTC).
    Same forward-compat rationale as ``message_id``."""

    content: str
    """Raw message content. May be empty (system messages, attachment-only
    posts); :func:`project_history` filters those out."""

    author_display_name: str = Field(min_length=1)
    """User-visible name to use in the ``<author>`` prefix when projecting
    to a non-self ``UserPromptPart``. Derived from ``message.author.display_name``
    (falling back to ``message.author.name``) at fetch time. Required to
    be non-empty so the projected ``<...>`` prefix is never bare brackets."""

    author_agent_id: str | None = None
    """Set when the author is a webhook whose ``display_name`` matches a
    registered agent (resolved against :class:`AgentRegistry` at fetch
    time). ``None`` for humans, third-party bots, and webhook posts from
    removed/renamed personas that no longer match any registered agent.
    :func:`project_history` uses this to decide self vs. other.

    Validated via :meth:`_validate_author_agent_id` (full-string match
    against :data:`AGENT_ID_PATTERN`) so a non-None value is constrained
    to the same character set / length :class:`AgentDefinition.agent_id`
    enforces. This prevents a malformed record from accidentally
    satisfying ``record.author_agent_id == self_agent_id`` if a future
    code path constructs records bypassing the fetcher (which only
    assigns from a validated registry entry).

    The reason we use a custom validator instead of :data:`AgentId` is
    that pydantic's ``StringConstraints(pattern=...)`` performs partial
    (search-style) matching, not full-string match. A value like
    ``"aaaUPPERCASE"`` slips through because the substring ``"aaa"``
    matches the pattern anywhere in the string. Mirroring
    :meth:`AgentDefinition._validate_agent_id`'s use of
    :meth:`re.Pattern.fullmatch` is the right enforcement."""

    @field_validator("author_agent_id")
    @classmethod
    def _validate_author_agent_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not AGENT_ID_PATTERN.fullmatch(v):
            raise ValueError(
                f"author_agent_id must match [a-z0-9_-]{{1,32}}, got {v!r}"
            )
        return v


class ChannelHistoryFetcher:
    """Fetches recent Discord channel history as :class:`HistoryRecord` lists.

    Constructed in the bridge process from the gateway's
    :class:`discord.Client`. Other deployments do not instantiate this
    class — they receive history through Kafka envelopes.

    Behavior contract:

    * **Bounded**: caller-supplied ``limit`` is clamped to
      :data:`_DISCORD_HISTORY_MAX_LIMIT` (Discord's per-call cap). A
      ``limit`` of 0 short-circuits (no Discord call) and returns ``[]``.
    * **Cached**: results are cached for
      :data:`_DEFAULT_CACHE_TTL_SECONDS` keyed on
      ``(source_channel_id, before_message_id, limit)``. The cache is a
      bounded LRU (:data:`_DEFAULT_CACHE_MAX_ENTRIES`); older entries
      evict on insert. Defensive copies on read keep callers from
      mutating the cache.
    * **Fail-safe**: every Discord error
      (:class:`discord.NotFound`, :class:`discord.Forbidden`,
      :class:`discord.HTTPException`) is caught and turns into an empty
      list with a WARN log. The fetcher never raises into the agent
      invocation path — missing history is acceptable; a broken
      invocation isn't.
    * **Permission dedup**: :class:`discord.Forbidden` (missing Read
      Message History) logs once per channel id via a bounded LRU
      (:data:`_FORBIDDEN_LOG_DEDUP_MAX`), so misconfigured channels
      don't flood logs.

    Order returned: oldest-first. Discord's
    :meth:`~discord.abc.Messageable.history` returns newest-first by
    default; this class reverses internally so the projected
    ``message_history`` reads in chronological order (which is what
    LLM providers and humans both expect).
    """

    def __init__(
        self,
        discord_client: discord.Client,
        registry: AgentRegistry,
        *,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        cache_max_entries: int = _DEFAULT_CACHE_MAX_ENTRIES,
    ) -> None:
        self._client = discord_client
        self._registry = registry
        self._cache_ttl = cache_ttl_seconds
        self._cache_max = cache_max_entries
        self._cache: OrderedDict[
            tuple[int, int, int], tuple[float, list[HistoryRecord]]
        ] = OrderedDict()
        self._forbidden_log_dedup: OrderedDict[int, None] = OrderedDict()
        # Single-flight in-flight map: concurrent calls with the same
        # ``(source_channel_id, before_message_id, limit)`` key share a
        # single Discord REST call. Without this, the TTL cache only
        # coalesces *sequential* bursts (first caller's result is cached
        # by the time the second arrives) — but a router fan-out
        # triggers N concurrent fetches simultaneously, and all N would
        # otherwise race past the empty cache and each hit Discord
        # independently. The single-flight map ensures one fetch
        # serves all N callers.
        self._in_flight: dict[
            tuple[int, int, int], asyncio.Future[list[HistoryRecord]]
        ] = {}

    async def fetch(
        self,
        *,
        source_channel_id: int,
        before_message_id: int,
        limit: int,
    ) -> list[HistoryRecord]:
        """Fetch up to ``limit`` records older than ``before_message_id``.

        Args:
            source_channel_id: The actual Discord channel the triggering
                message landed in (thread or top-level). The bridge's
                normalizer collapses threads to their parent for
                topic-routing purposes; this argument must be the
                un-collapsed value so the right history is fetched.
            before_message_id: Discord snowflake to anchor the fetch.
                The returned records are strictly older than this id
                (Discord's ``before=`` is exclusive). Typical caller
                passes ``wire.message_id``.
            limit: Maximum records to return. Clamped to
                :data:`_DISCORD_HISTORY_MAX_LIMIT`. ``0`` is treated as
                "history disabled" and short-circuits without a Discord
                call.

        Returns:
            Oldest-first list of :class:`HistoryRecord`. Empty on any
            failure or cap. The contract is "never raises into the
            invocation path" — any exception from Discord or from
            record construction is logged and absorbed into an empty
            list.
        """
        limit = min(max(0, limit), _DISCORD_HISTORY_MAX_LIMIT)
        if limit == 0:
            return []

        key = (source_channel_id, before_message_id, limit)

        # Cache check (fresh) — first try.
        cached = self._cache.get(key)
        if cached is not None and monotonic() - cached[0] < self._cache_ttl:
            self._cache.move_to_end(key)
            return list(cached[1])

        # Single-flight check — if another coroutine is already fetching
        # this same key, join its future instead of starting a parallel
        # Discord call. This is the core of the fan-out coalescing
        # guarantee documented at :attr:`_in_flight`.
        in_flight = self._in_flight.get(key)
        if in_flight is not None:
            return list(await in_flight)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[HistoryRecord]] = loop.create_future()
        self._in_flight[key] = future
        try:
            records = await self._do_fetch(
                source_channel_id=source_channel_id,
                before_message_id=before_message_id,
                limit=limit,
            )
            # Happy path: cache, resolve the shared future for any
            # concurrent followers, return a defensive copy to the
            # leader. These steps live INSIDE the try block so that any
            # exception they raise (cache helper bug, ``set_result``
            # on a misused future, etc.) flows through the same
            # contract-enforcing except handlers below — the "fetcher
            # never raises into invocation path" guarantee covers the
            # full body, not just the Discord call.
            self._cache_and_return(key, monotonic(), records)
            if not future.done():
                future.set_result(records)
            return list(records)
        except asyncio.CancelledError:
            # The leader's task is being cancelled (e.g., bridge
            # shutdown). Do NOT propagate this cancellation to passive
            # followers awaiting the shared future — they have their
            # own cancellation scopes and would otherwise see a
            # ``CancelledError`` they never asked for. Resolve the
            # shared future with the documented empty-history sentinel
            # so followers complete normally; re-raise so the leader's
            # own stack sees the cancel.
            if not future.done():
                future.set_result([])
            raise
        except Exception:
            # Defense-in-depth: :meth:`_do_fetch` is contractually
            # total — its own try/except absorbs every documented
            # Discord error class into ``[]``. Reaching this branch
            # means a truly unexpected exception (a bug in
            # :meth:`_to_record`, a future ``discord.py`` shape change
            # that slipped past the inner sweep, or a defect in the
            # cache helper). Log loudly at this layer so the failure is
            # observable, then degrade per the public fetcher contract
            # ("never raises into invocation path"): every caller —
            # the leader AND any concurrent followers — gets ``[]``.
            logger.exception(
                "history fetch single-flight raised unexpectedly; "
                "serving [] to all callers source_channel_id=%d "
                "before_message_id=%d limit=%d",
                source_channel_id,
                before_message_id,
                limit,
            )
            if not future.done():
                future.set_result([])
            return []
        finally:
            self._in_flight.pop(key, None)

    async def _do_fetch(
        self,
        *,
        source_channel_id: int,
        before_message_id: int,
        limit: int,
    ) -> list[HistoryRecord]:
        """Inner fetch implementation — never invoked concurrently for
        the same key (the outer :meth:`fetch` enforces single-flight).

        Catches every documented Discord error class plus a defensive
        ``Exception`` sweep around record construction so the public
        ``fetch`` contract ("never raises into invocation path")
        holds even if a future ``discord.py`` release returns
        unexpected ``Message`` shapes.
        """
        channel = await self._resolve_channel(source_channel_id)
        if channel is None:
            return []

        try:
            messages = [
                m
                async for m in channel.history(
                    limit=limit,
                    before=discord.Object(id=before_message_id),
                )
            ]
        except discord.Forbidden:
            self._log_forbidden_once(source_channel_id)
            return []
        except discord.NotFound:
            logger.warning(
                "channel_id=%d: history fetch returned 404; channel may have been deleted",
                source_channel_id,
            )
            return []
        except discord.HTTPException as e:
            logger.warning(
                "channel_id=%d: history fetch failed status=%s: %s",
                source_channel_id,
                e.status,
                e,
            )
            return []

        # discord.py returns newest-first; reverse for chronological order.
        # Wrap projection in a defensive sweep: if a future ``discord.py``
        # version returns a ``Message`` shape missing an attribute we
        # read in :meth:`_to_record`, we'd otherwise raise into the
        # invocation path (violating the "never raises" contract).
        try:
            return [self._to_record(m) for m in reversed(messages)]
        except Exception:
            logger.exception(
                "channel_id=%d: failed to project messages into HistoryRecords; "
                "returning empty history. This indicates an unexpected "
                "discord.Message shape; investigate.",
                source_channel_id,
            )
            return []

    async def _resolve_channel(
        self, channel_id: int
    ) -> discord.abc.Messageable | None:
        """Return the channel object for ``channel_id`` or ``None`` on failure.

        Tries the gateway client's in-memory cache first (no REST call),
        falling back to a REST ``fetch_channel`` lookup. The fallback
        log is INFO because a cache miss is informative (the gateway
        missed a guild-create / channel-create event) but not an error
        — fetch_channel works fine.
        """
        channel = self._client.get_channel(channel_id)
        if channel is not None:
            return channel
        logger.info(
            "channel_id=%d not in discord.Client cache; falling back to REST fetch_channel",
            channel_id,
        )
        try:
            return await self._client.fetch_channel(channel_id)
        except discord.NotFound:
            logger.warning(
                "channel_id=%d: fetch_channel returned 404; channel does not exist or bot lacks access",
                channel_id,
            )
            return None
        except discord.Forbidden:
            self._log_forbidden_once(channel_id)
            return None
        except discord.HTTPException as e:
            logger.warning(
                "channel_id=%d: fetch_channel failed status=%s: %s",
                channel_id,
                e.status,
                e,
            )
            return None

    def _to_record(self, msg: Any) -> HistoryRecord:
        """Translate one ``discord.Message`` into a :class:`HistoryRecord`.

        Identity resolution (webhook display_name → ``agent_id``) mirrors
        :meth:`MessageNormalizer._build_author` so live invocations and
        replayed history use the same self-recognition primitive.
        """
        author_display_name = (
            getattr(msg.author, "display_name", None) or msg.author.name
        )
        author_agent_id: str | None = None
        if msg.webhook_id is not None:
            spec = self._registry.by_display_name(author_display_name)
            if spec is not None:
                author_agent_id = spec.agent_id
        return HistoryRecord(
            message_id=msg.id,
            created_at=msg.created_at,
            content=msg.content,
            author_display_name=author_display_name,
            author_agent_id=author_agent_id,
        )

    def _cache_and_return(
        self,
        key: tuple[int, int, int],
        timestamp: float,
        records: list[HistoryRecord],
    ) -> list[HistoryRecord]:
        """Insert into cache + evict LRU + return a defensive copy.

        Defensive copy prevents a caller mutating the list from
        corrupting subsequent cache hits.
        """
        self._cache[key] = (timestamp, records)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return list(records)

    def _log_forbidden_once(self, channel_id: int) -> None:
        """Log a Forbidden once per channel, with a bounded LRU dedup set.

        Bounded by :data:`_FORBIDDEN_LOG_DEDUP_MAX` (see its docstring
        for the channel-vs-message-id sizing rationale).
        """
        if channel_id in self._forbidden_log_dedup:
            self._forbidden_log_dedup.move_to_end(channel_id)
            return
        self._forbidden_log_dedup[channel_id] = None
        while len(self._forbidden_log_dedup) > _FORBIDDEN_LOG_DEDUP_MAX:
            self._forbidden_log_dedup.popitem(last=False)
        logger.warning(
            "channel_id=%d: Read Message History permission missing; "
            "agent invocations in this channel will run without history. "
            "Grant the bot 'Read Message History' to enable.",
            channel_id,
        )


def project_history(
    records: Sequence[HistoryRecord],
    self_agent_id: str | None,
) -> list[ModelMessage]:
    """Project records into a ``list[ModelMessage]`` from one agent's POV.

    Args:
        records: Oldest-first :class:`HistoryRecord` list, typically
            from :meth:`ChannelHistoryFetcher.fetch` or
            ``MetadataEnvelope.history``.
        self_agent_id: The agent the resulting history will be sent to.
            Records whose ``author_agent_id`` matches become
            :class:`ModelResponse` (the agent's own prior turns); all
            others become :class:`ModelRequest` with the speaker's
            display_name prefixed into the
            :class:`UserPromptPart` content as ``<name>``. Passing
            ``None`` (used by the router) treats everything as
            ``ModelRequest`` — the router is an outside observer with
            no prior turns in the channel.

    Returns:
        A list suitable for ``Client.invoke_node(message_history=...)``.
        May be empty.

    **What this function deliberately does NOT do**:

    * It does *not* merge consecutive same-role entries. Pydantic-ai's
      :func:`_clean_message_history`
      (``calfkit/_vendor/pydantic_ai/_agent_graph.py:1386``) handles
      that automatically before any provider mapper sees the list. Our
      constructed messages satisfy its merge conditions
      (``instructions=None``; no provider metadata).
    * It does *not* peel a trailing :class:`ModelRequest` to merge with
      the staged user_prompt — same reason; pydantic-ai's clean
      merges the trailing history ``ModelRequest`` with the staged
      one automatically.

    **What it DOES do**:

    * Drops records with empty/whitespace-only ``content``. Anthropic's
      mapper would silently emit zero-block user messages (guarded out
      at ``models/anthropic.py:740``) and OpenAI would waste tokens —
      cleaner to filter here.
    * Drops leading :class:`ModelResponse` entries iteratively.
      Pydantic-ai's clean merges but never drops, and Anthropic
      rejects requests whose first message is ``assistant``. If the
      oldest fetched record is the agent's own webhook reply, we'd
      open with a ``ModelResponse`` and Anthropic would 400.
    """
    out: list[ModelMessage] = []
    for r in records:
        if not r.content.strip():
            continue
        is_self = (
            self_agent_id is not None and r.author_agent_id == self_agent_id
        )
        if is_self:
            out.append(ModelResponse(parts=[TextPart(content=r.content)]))
        else:
            prefix = f"<{r.author_display_name}> "
            out.append(
                ModelRequest(parts=[UserPromptPart(content=prefix + r.content)])
            )
    while out and isinstance(out[0], ModelResponse):
        out.pop(0)
    return out
