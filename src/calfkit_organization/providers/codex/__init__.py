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
  - Calfkit integration: ``CodexSubscriptionModelClient`` subclasses
    ``calfkit.providers.OpenAIResponsesModelClient`` and overrides
    ``_map_messages`` to apply the Codex impersonation transformation
    (short fixed ``instructions`` + system prompt prepended to first user
    message). Required because the Codex backend rejects long/complex
    instruction blocks.
"""

from calfkit_organization.providers.codex.factory_hook import (
    CodexNotLoggedInError,
    build_codex_subscription_client,
)

__all__ = ["CodexNotLoggedInError", "build_codex_subscription_client"]
