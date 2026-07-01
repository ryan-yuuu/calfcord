"""Default avatar URL helper used as the per-agent fallback persona avatar.

Kept in its own module — separate from :mod:`calfcord.discord.persona` —
so callers that need only the URL string (notably
:func:`calfcord.bridge.persona_resolve.persona_for`, which derives the
avatar at persona-resolution time) don't pull in the ``discord.py`` library
import surface of ``persona.py``.
"""

from __future__ import annotations


def dicebear_avatar_url(seed: str) -> str:
    """Return a deterministic DiceBear "glass" avatar URL for ``seed``.

    DiceBear's "glass" style (https://www.dicebear.com) renders abstract
    frosted-gradient blobs; same seed → same image, no auth required.
    Used as the default persona avatar source for calfkit agents so each
    agent gets a stable, recognizable identity without us hosting images.
    """
    return f"https://api.dicebear.com/9.x/glass/png?seed={seed}"
