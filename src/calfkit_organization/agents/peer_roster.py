"""Build the per-invocation peer roster injected as ``temp_instructions``.

Per-invocation ``temp_instructions`` carries only the runtime data the
LLM cannot derive from its tool docstrings — specifically the current
peer roster, and (in channel context) the @-mention syntax documented
in :mod:`calfkit_organization.bridge.normalizer` (which is bridge-level
behavior, not a tool, so no tool docstring covers it).

Two contexts share the same builder via a keyword-only flag:

* ``channel=True`` — the invocation came from a shared Discord channel
  (slash, @-mention, or router fan-out). Returns roster + @-mention
  rules. Tools-independent: the rules apply to any agent posting into
  a channel, whether or not it also has the ``private_chat`` tool.
* ``channel=False`` — the invocation came via A2A
  (:func:`~calfkit_organization.tools.private_chat.private_chat`).
  Returns roster only, and only when the target has the
  ``private_chat`` tool. The tool's own docstring carries everything
  else; restating it here would be wasted tokens.

Operates on a :class:`PhonebookEntry` list rather than an
:class:`AgentRegistry` directly so the same helper works in both the
bridge (which converts its registry to a phonebook) and in any
decoupled deployment that received the phonebook via ``deps``.
"""

from __future__ import annotations

from collections.abc import Sequence

from calfkit_organization.agents.phonebook import PhonebookEntry, format_roster_lines

_PRIVATE_CHAT_TOOL_NAME = "private_chat"

_MENTION_BLOCK = """\
You can invoke another agent into THIS conversation by writing
`@<agent_id>` in your reply (e.g. `@scribe`). The mentioned agent
runs immediately and posts a reply into this channel, exactly as if
the user had typed the @-mention themselves.

CRITICAL: `@<agent_id>` is an INVOCATION verb, NOT a soft reference.

Use `@<agent_id>` ONLY when you genuinely want that agent to respond
on this turn. When you are merely talking ABOUT another agent —
naming them in a sentence, listing options, describing capabilities,
recapping who said what, asking the user whether to involve them —
use their plain display name (e.g. `Scribe`, `Conan`) WITHOUT the
`@`. The plain name is a noun; `@name` is a verb.

Failing to make this distinction will spam unintended invocations
and create back-and-forth agent loops the user did not ask for.

WRONG (each line below fires an unintended invocation):
- "I'll bring @scribe into this conversation"
- "Option (a): bring @scribe in for a tag-team"
- "I asked @scribe earlier and they said..."
- "@conan handles humor and @scribe handles prose"
- "Should I loop in @scribe?"
- "Want me to bring @conan in, or write it myself?"

RIGHT (referring to peers WITHOUT invoking them):
- "I'll bring Scribe into this conversation"
- "Option (a): bring Scribe in for a tag-team"
- "I asked Scribe earlier and they said..."
- "Conan handles humor and Scribe handles prose"
- "Should I loop in Scribe?"
- "Want me to bring Conan in, or write it myself?"

RIGHT (intentional invocation — you actually want them to respond now):
- "@scribe — can you help me tighten the prose here?"
- "If you want a second take: @scribe ?"
- "@conan, take it from here."

Mechanics (so you can predict exactly what will fire):
- The `@` token must be at the very start of the message OR
  directly preceded by whitespace to count. `foo@scribe` and
  `me@scribe.com` do NOT invoke anything.
- Mentions are case-insensitive (`@Scribe` == `@scribe`).
- EVERY `@<name>` token in your message is validated — including
  ones after the first. If any one of them does not match an
  agent_id from the roster above, an error is shown to the user
  and nothing fires. Keep all `@`-tokens to valid ids, or omit
  them entirely.
- When a message contains multiple valid `@<agent_id>` tokens,
  only the first invokes a peer; later valid mentions are inert
  decorative text.
- @-mentioning yourself has no effect: your own gate silently
  drops the message and no reply is posted — to the user it looks
  like you ignored them."""


def build_temp_instructions(
    phonebook: Sequence[PhonebookEntry],
    target_agent_id: str,
    *,
    channel: bool,
) -> str | None:
    """Return the ``temp_instructions`` to inject for an invocation of ``target_agent_id``.

    Args:
        phonebook: The full set of known agents (the target included).
            Either freshly built from the registry by the bridge or
            received as ``deps["phonebook"]`` by a downstream
            deployment.
        target_agent_id: The agent the invocation will be delivered to.
            Excluded from the advertised roster — an agent never needs
            to be told it can talk to itself.
        channel: ``True`` when the invocation came from a shared
            Discord channel (slash, @-mention, or router fan-out);
            ``False`` when it came via A2A (the ``private_chat`` tool).

    Returns:
        Channel context — a roster block followed by the @-mention
            rules. Tools-independent.
        A2A context — a roster block alone, and only when the target
            has the ``private_chat`` tool in its declared tools
            (otherwise ``None``, since the tool docstring covers
            everything else and the roster alone is meaningless
            without the tool to call).
        ``None`` in either context when ``target_agent_id`` is missing
        from the phonebook or has no peers to advertise.
    """
    target = next((e for e in phonebook if e.agent_id == target_agent_id), None)
    if target is None:
        return None
    peers = [e for e in phonebook if e.agent_id != target_agent_id]
    if not peers:
        return None
    roster = format_roster_lines(peers)

    if channel:
        return f"Other agents in this organization:\n{roster}\n\n{_MENTION_BLOCK}"

    if _PRIVATE_CHAT_TOOL_NAME not in target.tools:
        return None
    return f"Peer agents you can reach via the private_chat tool:\n{roster}"
