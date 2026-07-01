"""Discord ↔ calfkit bridge (the pure ``Client`` caller surface).

The package keeps only a tiny convenience re-export — the wire DTOs. Import the
bridge submodules directly (``from calfcord.bridge.gateway import main``,
``from calfcord.bridge.history import …``) rather than widening this surface: the
bridge is composed of focused modules, not one façade.
"""

from calfcord.bridge.wire import WireAuthor, WireMessage

__all__ = [
    "WireAuthor",
    "WireMessage",
]
