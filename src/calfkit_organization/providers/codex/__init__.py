"""ChatGPT subscription auth provider for Codex models.

Authenticates against OpenAI's Codex OAuth (the same client used by the
official ``codex`` CLI) and routes inference through
``https://chatgpt.com/backend-api/codex`` so requests are billed against
the operator's ChatGPT Plus/Pro subscription rather than API credits.

Agents opt in by setting ``provider: openai-codex`` in their frontmatter.
The runner must have valid cached credentials at startup; obtain them via
``uv run calfkit-auth codex login``.

Composition:
  - OAuth login/refresh flows: delegated to ``openhands-sdk`` (already a
    transitive dependency; its ``OpenAISubscriptionAuth`` handles PKCE,
    browser callback, device-code, and credential persistence).
  - Runtime token refresh: delegated to ``authlib``'s ``AsyncOAuth2Client``,
    used as the underlying ``httpx.AsyncClient`` for the OpenAI SDK so
    refresh-on-expiry happens transparently with no background task.
  - Codex CLI system prompt: fetched verbatim from ``openai/codex`` on
    process startup (with ETag-conditional refresh against an on-disk
    cache) so the ``instructions`` field of every request matches what
    the official Codex CLI sends. OpenAI's Codex backend explicitly
    fingerprints this field — see ``prompts.py`` and openai/codex#4433.
    The runner calls :func:`prewarm_codex_prompts` before constructing
    any model client.
  - Calfkit integration: ``CodexSubscriptionModelClient`` subclasses
    ``calfkit.providers.OpenAIResponsesModelClient`` and overrides
    ``_map_messages`` to substitute the verbatim per-model Codex prompt
    as ``instructions`` and smuggle the agent's real system prompt into
    a leading synthetic user message as ``input_text``.
"""

from calfkit_organization.providers.codex.factory_hook import (
    CodexNotLoggedInError,
    build_codex_subscription_client,
)
from calfkit_organization.providers.codex.prompts import (
    CodexPromptsUnavailableError,
    prewarm_codex_prompts,
)

__all__ = [
    "CodexNotLoggedInError",
    "CodexPromptsUnavailableError",
    "build_codex_subscription_client",
    "prewarm_codex_prompts",
]
