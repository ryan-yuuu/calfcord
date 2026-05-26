"""Entry point invoked by ``AgentFactory`` for the ``openai-codex`` provider.

Kept thin and self-contained so the factory's lazy import in
``agents/factory.py`` doesn't pull in authlib / the OpenAI SDK / OpenHands
auth machinery for projects that don't use Codex.
"""

from __future__ import annotations

from calfkit.providers.pydantic_ai.model_client import PydanticModelClient

from calfkit_organization.providers.codex.model_client import CodexSubscriptionModelClient
from calfkit_organization.providers.codex.token_store import get_credential_store, load_credentials


class CodexNotLoggedInError(RuntimeError):
    """Raised when an agent declares ``provider: openai-codex`` but no
    cached credentials exist.

    Surfaces at runner bootstrap (before any Discord traffic is served) so
    the operator gets a clear instruction rather than a stack trace on
    first message.
    """


def build_codex_subscription_client(model_name: str) -> PydanticModelClient:
    """Construct a Codex-backed model client for ``AgentFactory``."""
    store = get_credential_store()
    if load_credentials(store) is None:
        raise CodexNotLoggedInError(
            "No Codex credentials cached. Run: uv run calfkit-auth codex login"
        )
    return CodexSubscriptionModelClient(model_name=model_name, store=store)
