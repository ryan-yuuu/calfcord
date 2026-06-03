---
# ============================================================================
# agents/agent.template.md — full frontmatter schema reference
# ============================================================================
#
# This file is a TEMPLATE, not a live agent. The bridge's agent loader
# (src/calfcord/agents/loader.py) skips any file whose name ends
# in ".template.md", so this file is ignored on boot even though it ends in
# ".md". To create a real agent, copy this file to "agents/<your-name>.md"
# and edit the fields below.
#
# IMPORTANT — field names are snake_case (e.g. `thinking_effort`), even
# though the Discord slash command uses kebab-case (`/thinking-effort`).
# The `extra="forbid"` validator (AgentDefinition) rejects misspelled keys
# at boot time so typos surface immediately rather than silently falling
# back to defaults.
#
# Field-ordering / comments note: when the `/thinking-effort` slash command
# rewrites a LIVE agent file, python-frontmatter dumps through PyYAML's
# safe_dump — keys end up alphabetized and comments are NOT preserved. This
# template is never loaded or rewritten, but its comments would likewise not
# survive on a live file: treat them as documentation, not as something that
# round-trips.

# ----------------------------------------------------------------------------
# Identity (all three required; immutable after first deploy — the slash
# command and channel routing both index by these).
# ----------------------------------------------------------------------------

# Internal identifier. Must match the filename stem (so `name: scribe`
# means the file is `agents/scribe.md`). Format: [a-z0-9_-]{1,32}.
# The Discord slash command is always `/<name>` — there is no separate
# slash field. The bridge currently uses @<name> text-prefix invocation
# by default and only registers /thinking-effort; per-agent invocation
# slashes are wired in code but disabled.
name: example

# What Discord users see as the bot's name on this agent's webhook
# replies. 1-80 chars. "Clyde" is rejected by Discord webhooks.
display_name: Example

# Short blurb shown in Discord's slash-command picker. 1-100 chars.
description: An example agent demonstrating the .md frontmatter schema.

# ----------------------------------------------------------------------------
# Appearance (optional)
# ----------------------------------------------------------------------------

# Avatar URL for the persona webhook reply. Omit (or set to `null`) to
# get the per-agent DiceBear default
# (https://api.dicebear.com/9.x/glass/png?seed=<name>). Set to any
# public image URL to override.
# avatar_url: https://example.com/my-avatar.png

# ----------------------------------------------------------------------------
# LLM (optional — see CALFKIT_AGENT_DEFAULT_PROVIDER / _DEFAULT_MODEL
# env vars and DEFAULT_PROVIDER in agents/factory.py for fallbacks)
# ----------------------------------------------------------------------------

# Which provider's model client to construct. One of:
#   - "anthropic"     Claude models via the Anthropic API (ANTHROPIC_API_KEY)
#   - "openai"        GPT models via the OpenAI API (OPENAI_API_KEY)
#   - "openai-codex"  OpenAI Codex models billed through a ChatGPT Plus/Pro
#                     subscription instead of API credits. Requires a
#                     one-time `uv run calfkit-auth codex login`; see
#                     calfcord.providers.codex.
# Omit to fall back to CALFKIT_AGENT_DEFAULT_PROVIDER, then to the
# project default ("anthropic"). See resolve_provider() in agents/factory.py.
provider: anthropic

# Provider-specific model name. Examples: "claude-sonnet-4-5",
# "claude-opus-4-7" (anthropic); "gpt-5-mini", "gpt-5-nano" (openai).
# Omit to use the provider's default:
#   anthropic    → claude-sonnet-4-5
#   openai       → gpt-5-mini
#   openai-codex → NO static default; leave `model` unset and the Codex
#                  client resolves the highest-priority model from its live
#                  catalog at construction. Pinning a slug here is exactly
#                  what caused retired Codex models to be sent.
# Precedence: model → CALFKIT_AGENT_DEFAULT_MODEL → provider default.
model: claude-sonnet-4-5

# ----------------------------------------------------------------------------
# Tools (optional). Each entry resolves at boot — builtin names against
# TOOL_REGISTRY, `mcp/...` selectors against the committed MCP catalog.
# Unknown builtins, unknown servers/tools, and malformed selectors all fail
# fast with an error listing the valid alternatives.
# ----------------------------------------------------------------------------

# Available builtins (see src/calfcord/tools/builtin/ for source):
#   - private_chat   one-on-one A2A conversation with another agent
#   - shell          run a shell command on the calfkit-tools host
#   - read_file      view a file's contents (with line numbers)
#   - write_file     create or overwrite a file
#   - edit_file      exact-string edit (replace_all optional)
#   - grep           search file contents (ripgrep-backed)
#   - glob           find files by name pattern
#   - web_fetch      fetch a URL and convert to markdown
#   - web_search     DuckDuckGo search
#   - todo_view      view the agent's task list
#   - todo_write     replace the agent's task list
#
# MCP-server tools (see docs/mcp-tools.md for the full architecture). Mix
# `mcp/...` selectors into this SAME list alongside builtin names. Two forms:
#   - mcp/<server>          ALL tools advertised by that MCP server.
#   - mcp/<server>/<tool>   ONE specific tool. `<tool>` is the raw MCP tool
#                           name and MAY contain hyphens (e.g. mcp/gmail/list-labels).
# Whichever form you use, the agent's LLM sees each selected tool under the
# flattened name `<server>_<tool>` (e.g. `gmail_search`, `gmail_list_labels`).
# Builtin names are unchanged. The agent process only advertises the MCP tool
# SCHEMA and dispatches calls over Kafka — it never opens an MCP connection
# and holds no MCP credentials; the separate calfkit-mcp bridge does that.
#
# Semantics of the `tools:` line:
#   - omitted entirely  → agent gets EVERY registered builtin and NO MCP
#                         tools. Convenient, but means a new agent ships with
#                         shell/write_file/edit_file access to the shared
#                         workspace — narrow the list if the agent takes input
#                         from untrusted users.
#   - tools: []         → agent gets NO tools (text-only).
#   - tools: [a, b]     → exactly those entries (builtins and/or mcp/...).
#
# KNOWN LIMITATION: there is no "all builtins PLUS some MCP tools" shorthand.
# The default expansion (omitting `tools:`) is builtins-only; adding an
# `mcp/...` selector requires an explicit list, which turns off the
# builtin default. To get both, list the builtins you want explicitly
# alongside the `mcp/...` selectors, e.g.:
#   tools: [shell, read_file, write_file, grep, mcp/gmail/search]
#
# Filesystem/shell tools share one workspace on the calfkit-tools host
# (CALFCORD_WORKSPACE_DIR, default state/workspace/). Every agent that
# declares them can read/edit any file in that workspace. See the
# project README's "Security model" section before adding shell/file
# tools to an agent that takes input from untrusted users.
tools: []

# ----------------------------------------------------------------------------
# Thinking effort (optional)
# ----------------------------------------------------------------------------

# Operator-tunable reasoning/thinking-budget tier. One of:
#
#   none    - extended thinking disabled
#   minimal - Anthropic budget=1024  | OpenAI reasoning_effort=minimal
#   low     - Anthropic budget=4000  | OpenAI reasoning_effort=low
#   medium  - Anthropic budget=10000 | OpenAI reasoning_effort=medium
#   high    - Anthropic budget=31999 | OpenAI reasoning_effort=high
#   xhigh   - Anthropic budget=48000 | OpenAI reasoning_effort=high
#   max     - Anthropic budget=63999 | OpenAI reasoning_effort=high
#
# (The "openai-codex" provider shares the OpenAI reasoning_effort ramp.)
#
# Omit the field entirely to skip the override (the agent uses whatever
# the model client / provider defaults to). Setting it to "none" is
# explicit — the operator chose to disable extended thinking.
#
# The /thinking-effort Discord slash command rewrites this field at
# runtime. Slash and @-mention paths pick up the new value on the next
# message; ambient channel messages need an agent restart to see it
# (the tier is baked into the calfkit Agent constructor at agent boot).
thinking_effort: medium

# ----------------------------------------------------------------------------
# Conversation history (optional)
# ----------------------------------------------------------------------------

# Number of recent channel messages the bridge fetches and projects into
# the model's message_history on every invocation. Integer 0-100 (100 is
# Discord's per-call REST cap); default 30.
#   - 0 disables history fetching entirely (no Discord REST call; the agent
#     runs with only the system prompt + the triggering message).
history_turns: 30

# ----------------------------------------------------------------------------
# Memory (optional)
# ----------------------------------------------------------------------------

# Opt in to a persistent per-agent notepad under memory/<name>/ in the
# shared workspace (one-fact-per-file plus a MEMORY.md index). When true,
# the factory appends a memory-instructions block to the agent's
# instructions at runtime — the system_prompt body below is left unchanged.
#
# A memory-enabled agent MUST also declare the read_file and write_file
# tools (it manages the notepad with them); the factory fails fast at boot
# otherwise. Omitting `tools:` grants all builtins and satisfies this.
# Default false.
memory: false

# ----------------------------------------------------------------------------
# Reserved fields — DO NOT set on a normal assistant agent
# ----------------------------------------------------------------------------
#
# role: "assistant" | "router". Defaults to "assistant", which is what every
# user-authored agent should be. "router" is reserved for the singleton
# built-in routing agent, constructed in code from a bundled router.md
# (not loaded from this directory). Wiring a second router trips a registry
# boot error. Leave this unset. (Routers declare NO tools at all, so no
# builtin names and no `mcp/...` selectors ever appear on a router.)
#
# publish_topic: reserved for routers (declares the Kafka topic their
# structured output is published to). The validator REJECTS an assistant
# that sets publish_topic, so leave it unset — assistants emit their reply
# to the inbound frame's callback_topic automatically.
---

The body of this .md file (everything after the closing `---` of the frontmatter) 
are the instructions fed verbatim to the
agent on every run. Nothing in here is templated or substituted.

Replace this body with the personality and
instructions you want the agent to embody.