"""Single-host process supervision via Process Compose.

The supervisor turns the substrate/roster lifecycle (``docs/design/onboarding-redesign.md``)
into a generated Process Compose project the user never edits. This package is
deliberately I/O- and broker-free at its core: :mod:`calfcord.supervisor.compose`
is a pure config generator that can be imported anywhere.
"""
