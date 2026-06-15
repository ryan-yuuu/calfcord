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
# Tools (optional). Each entry is a builtin name resolved at boot against
# TOOL_REGISTRY (an unknown builtin fails fast, listing the valid alternatives),
# OR an `mcp/...` selector for tools hosted by an MCP server in mcp.json:
#   - mcp/<server>         every tool that server advertises
#   - mcp/<server>/<tool>  exactly that one tool
# MCP selectors are validated for syntax here; the agent resolves them per turn
# against the live capability advertisement, so a server's tool list can change
# with no agent restart (but editing these lines DOES need a restart, like a
# builtin). See docs/mcp-tools.md.
# ----------------------------------------------------------------------------

# Available builtins (vendored from calfkit-tools, plus first-party private_chat):
#   - private_chat   one-on-one A2A conversation with another agent
#   - terminal       run a shell command on the calfkit-tools host
#   - process        manage background processes started by terminal
#   - read_file      view a file's contents (with line numbers)
#   - write_file     create or overwrite a file
#   - patch          targeted edit (exact-string replace or V4A patch)
#   - search_files   search file contents or find files by name (ripgrep-backed)
#   - todo           view or replace the agent's task list
#   - execute_code   run Python on the calfkit-tools host
#   - web_search     web search
#   - web_extract    extract readable content from URLs
#   - web_fetch      fetch a URL and convert to markdown
#
# Per-agent isolation: the stateful tools (terminal, files, todo, …) key
# their state by the calling agent's identity, so one agent's shell session,
# working directory, and task list are invisible to another.
#
# Semantics of the `tools:` line:
#   - omitted entirely  → agent gets EVERY registered builtin (but NO MCP tools;
#                         MCP grants are always explicit). Convenient, but means a
#                         new agent ships with terminal/write_file/execute_code
#                         access to the shared workspace — narrow the list if the
#                         agent takes input from untrusted users.
#   - tools: []         → agent gets NO tools (text-only).
#   - tools: [a, b]     → exactly those builtins (and/or mcp/... selectors), e.g.
#                         tools: [read_file, mcp/github, mcp/docs/search]
#
# Filesystem/shell tools start in one shared workspace on the calfkit-tools
# host (CALFCORD_WORKSPACE_DIR, default state/workspace/). Each agent gets its
# own session there, but all sessions can reach the same files. See
# docs/security.md before adding terminal/file/execute_code tools to an agent
# that takes input from untrusted users.
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
# builtin names ever appear on a router.)
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