"""Unit tests for the pure persona resolver (C8/C9/D-7)."""

from __future__ import annotations

from calfcord.bridge.persona_resolve import persona_for
from calfcord.discord.avatar import dicebear_avatar_url
from calfcord.discord.persona import Persona


def test_persona_for_uses_name_and_seeded_avatar() -> None:
    assert persona_for("scribe") == Persona(name="scribe", avatar_url=dicebear_avatar_url("scribe"))


def test_persona_for_is_deterministic() -> None:
    assert persona_for("conan") == persona_for("conan")


def test_distinct_names_get_distinct_avatars() -> None:
    a, b = persona_for("scribe"), persona_for("conan")
    assert a.name != b.name
    assert a.avatar_url != b.avatar_url
