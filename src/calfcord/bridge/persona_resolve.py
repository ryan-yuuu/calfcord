"""Pure persona resolution for the bridge (C8/C9/D-7).

After the migration the bridge no longer reads a registry for an agent's
``display_name``/``avatar_url``: a persona is derived entirely from the agent's
name — the webhook username *is* the name (C8), and the avatar is a
deterministic DiceBear seeded by it (C9). So :func:`persona_for` is a pure
function with no roster dependency, and it is handoff-correct because it is
keyed on the actual reply ``emitter`` (the node that really replied), not the
mention target.
"""

from __future__ import annotations

from calfcord.discord.avatar import dicebear_avatar_url
from calfcord.discord.persona import Persona


def persona_for(name: str) -> Persona:
    """The Discord persona for the agent ``name`` — its name as the webhook
    username and a deterministic DiceBear avatar seeded by that name."""
    return Persona(name=name, avatar_url=dicebear_avatar_url(name))
