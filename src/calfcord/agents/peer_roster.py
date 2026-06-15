"""Build the per-invocation peer roster injected as ``temp_instructions``.

Per-invocation ``temp_instructions`` carries only the runtime data the
LLM cannot derive from its tool docstrings — specifically the current
peer roster, and (in channel context) the @-mention syntax documented
in :mod:`calfcord.bridge.normalizer` (which is bridge-level
behavior, not a tool, so no tool docstring covers it).

Two contexts share the same builder via a keyword-only flag:

* ``channel=True`` — the invocation came from a shared Discord channel
  (slash, @-mention, or router fan-out). Returns roster + @-mention
  rules. Tools-independent: the rules apply to any agent posting into
  a channel, whether or not it also has the ``private_chat`` tool.
* ``channel=False`` — the invocation came via A2A
  (:func:`~calfcord.tools.private_chat.private_chat`).
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

from calfcord.agents.phonebook import PhonebookEntry, format_roster_lines

_PRIVATE_CHAT_TOOL_NAME = "private_chat"

_MENTION_BLOCK = """\
In this channel you have the power to HAND OFF the user's task to
another agent by writing `@<agent_id>` (e.g. `@scribe`). This is a
heavy, one-way action — use it sparingly, and when you do use it,
keep the handoff message short.

Casual `@` use spawns back-and-forth invocations the user did not
ask for; verbose `@`-handoffs clog the channel with redundant
context. Both pollute the user's view of the conversation.

`@<agent_id>` is a VERB, not a noun, label, or tag. Specifically, it
means "I am done; you take over." It is NOT `cc`, NOT `mention`, NOT
`ping`, NOT `loop in`. The plain display name (e.g. `Scribe`,
`Conan`) is the noun — use it whenever you are talking ABOUT a peer
rather than handing them the task.

WHAT HAPPENS WHEN YOU WRITE `@<agent_id>`:
- Your message is posted to the channel in full, exactly as written.
- The mentioned agent runs immediately and replies in the channel.
- The next thing the user sees is the OTHER agent's reply.
- You do NOT get to continue, follow up, incorporate their answer,
  or finish what you started on this turn.
- A SINGLE valid `@<agent_id>` ANYWHERE in your message — start,
  middle, end, in a parenthetical, inside a bullet, after a long
  reply — fires the handoff. There is no safe position. Putting it
  at the end is NOT "send my reply first, then loop them in"; your
  reply is posted AND the handoff fires.
- The receiving agent reads the SAME channel as you. They already
  see every message in this conversation — including the user's
  request and anything you wrote before handing off.

Therefore: if you want a peer's input but plan to keep working on
the task yourself, the `@<agent_id>` token must NOT appear in your
message at all. Use the plain name, ask the user, or just finish
your own reply.

WRONG — each fires an unintended handoff. The wording shows the
agent is NOT trying to invoke, yet the `@` commits anyway:
- "Would you like me to reach out to @marketer to work on this?"
- "Maybe @scribe should handle this part."
- "Two options: keep going myself, or pass to @scribe — your call."
- "Let me know if you want me to ask @conan to take this."

FIX — drop the `@`; these are nouns, not verbs:
- "Would you like me to reach out to Marketer to work on this?"
- "Maybe Scribe should handle this part."
- "Two options: keep going myself, or pass to Scribe — your call."
- "Let me know if you want me to ask Conan to take this."

ALSO WRONG — these DO intend to invoke, but they assume a follow-up
turn that will never happen:
- "@scribe, can you polish this? I'll integrate your edits below."
   (there is no "below" — your message is final)
- "Step 1: @scribe drafts. Step 2: I write the conclusion."
   (step 1 fires; step 2 never gets a chance)
- "@scribe, draft section 1; I'll write section 2 in parallel."
   (there is no parallel — the handoff is sequential)

WHEN YOU DO HAND OFF — keep it to 1-2 lines. The receiving agent
already has every message in this channel; briefing them, restating
the user's request, or re-summarizing the task wastes tokens and
clutters the conversation for the user. Hand off, don't hand over.

WRONG — bloated handoff that re-briefs the receiving agent:
- "Hey @scribe, the user is writing a blog post and asked me to
   help. Here's what I gathered: [paragraph of requirements]. The
   tone they want is [paragraph]. I've outlined the structure but
   think the prose needs polish — can you handle that? Let me know
   if you have questions about any of the requirements above."
   → @scribe saw the user's request and your prior messages already.
     Everything before the `@` was wasted typing.

RIGHT — terse handoff, same channel context applies:
- "@scribe — take it from here."
- "This is more your area than mine. @scribe ?"
- "The prose side is yours: @scribe"

If you completed your part of the task on this turn, post that work
AND the handoff in the same message — the handoff message is your
only output, so anything not included here is lost. The 1-2-line
brevity rule above applies to the handoff sentence, not to work you
produced. Hand off the REMAINDER of the task, not the whole thing.

RIGHT — completed work plus handoff in one message:
- "Outline:
   1. Hook
   2. Three case studies
   3. CTA
   @scribe — prose pass is yours."
- "Numbers pulled: Q1 +12%, Q2 +8%, Q3 -3%. @conan, narrative is yours."

Mechanics (so you can predict exactly what will fire):
- The `@` token must be at the very start of the message OR
  directly preceded by whitespace to count. `foo@scribe` and
  `me@scribe.com` do NOT invoke anything.
- Mentions are case-insensitive (`@Scribe` == `@scribe`).
- EVERY `@<name>` token in your message is validated. If any one
  does not match an agent_id from the roster above, an error is
  shown to the user and nothing fires. Keep all `@`-tokens to valid
  ids, or omit them entirely.
- When a message contains multiple valid `@<agent_id>` tokens, only
  the FIRST invokes a peer; later valid mentions are inert text. So
  even an "extra" `@<agent>` you intended as decoration commits the
  first one as a real handoff.
- @-mentioning yourself has no effect: your own gate silently drops
  the message and no reply is posted — to the user it looks like you
  ignored them."""


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

    # ``target.tools is None`` means "all registered tools" — the loader
    # normalizes this for .md-loaded agents, but a code-built definition
    # may still carry None here. Treat None as having private_chat
    # available since the loader expansion would include it.
    if target.tools is not None and _PRIVATE_CHAT_TOOL_NAME not in target.tools:
        return None
    return f"Peer agents you can reach via the private_chat tool:\n{roster}"
