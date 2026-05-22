"""Compatibility shims for working around calfkit SDK limitations.

This package exists for **temporary** workarounds that bridge gaps the
upstream SDK doesn't yet expose. Every shim here should carry a clear
FIXME pointing at the upstream cleanup that obviates it, and a "swap
back to ``client.invoke_node(...)``"-style migration note.

Today's contents (kept in sync with ``__all__`` below):

- :func:`invoke.invoke_node_with_metadata` — invokes a calfkit node
  with ``State.metadata`` populated. Used by the bridge ingress (to
  carry the original Discord wire through to the fan-out consumer)
  and by the router fan-out consumer (to carry synthesized wires to
  the bridge's synthesized-in consumer).
- :class:`invoke.MetadataEnvelope` — typed pydantic model for the
  ``state.metadata`` payload, with ``wire: WireMessage`` and
  ``phonebook: list[PhonebookEntry] | None`` fields. Producers
  construct it and consumers decode via :meth:`MetadataEnvelope.extract`.
- :data:`invoke.METADATA_KEY_WIRE` and
  :data:`invoke.METADATA_KEY_PHONEBOOK` — legacy string constants
  kept only for contract tests; do not import in new code.

The whole package goes away when upstream calfkit ships either of
the fixes tracked at https://github.com/calf-ai/calfkit-sdk/issues/144
— see the module FIXME in ``invoke.py`` for the cleanup recipe.
"""

from calfkit_organization._compat.invoke import (
    METADATA_KEY_PHONEBOOK,
    METADATA_KEY_WIRE,
    MetadataEnvelope,
    MetadataEnvelopeError,
    invoke_node_with_metadata,
    raise_envelope_error,
)

__all__ = [
    "METADATA_KEY_PHONEBOOK",
    "METADATA_KEY_WIRE",
    "MetadataEnvelope",
    "MetadataEnvelopeError",
    "invoke_node_with_metadata",
    "raise_envelope_error",
]
