"""Prompt seam for the interactive CLI commands.

The flows in :mod:`calfcord.cli.init` (and PR 4's ``agent tools`` editor) must
be unit-testable without a TTY, so they never touch a prompting library
directly. Instead they take a :class:`Prompter` — a small Protocol covering the
four prompt shapes we use (single-select, free text, masked secret, yes/no).
Tests inject a scripted fake; production injects :class:`InquirerPrompter`.

:class:`InquirerPrompter` imports InquirerPy **lazily inside each method** so
that merely importing this module (which the argparse entry point does at
startup, and which tests do) never requires InquirerPy or a TTY. The import
cost is paid only when a real prompt is actually shown.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Prompter(Protocol):
    """The interactive operations the CLI flows depend on.

    A Protocol (not a base class) so a test fake satisfies it structurally and
    PR 4 can share the exact same seam. ``runtime_checkable`` lets callers/tests
    assert ``isinstance(obj, Prompter)`` if they want a cheap sanity guard.
    """

    def select(self, message: str, choices: list[tuple[str, str]], *, default: str | None = None) -> str:
        """Single-choice select. ``choices`` are ``(value, label)`` pairs; returns the chosen VALUE."""
        ...

    def text(self, message: str, *, default: str = "") -> str:
        """Free-text input. Returns the entered string (``default`` when the operator just hits enter)."""
        ...

    def secret(self, message: str) -> str:
        """Masked input for a secret. Returns ``""`` when the operator skips it."""
        ...

    def confirm(self, message: str, *, default: bool = False) -> bool:
        """Yes/no prompt. Returns the boolean answer."""
        ...


class InquirerPrompter:
    """Real :class:`Prompter` backed by InquirerPy.

    InquirerPy is imported inside each method on purpose — see the module
    docstring. Each method maps one-to-one onto an ``InquirerPy.inquirer``
    constructor and immediately ``.execute()``s it.
    """

    def select(self, message: str, choices: list[tuple[str, str]], *, default: str | None = None) -> str:
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice

        return inquirer.select(
            message=message,
            choices=[Choice(value=value, name=label) for value, label in choices],
            default=default,
        ).execute()

    def text(self, message: str, *, default: str = "") -> str:
        from InquirerPy import inquirer

        return inquirer.text(message=message, default=default).execute()

    def secret(self, message: str) -> str:
        from InquirerPy import inquirer

        # transformer hides the typed length from the post-answer echo; an empty
        # answer (operator skips) must come back as "" so callers can keep an
        # existing value rather than clobber it.
        return inquirer.secret(message=message, transformer=lambda _: "******").execute()

    def confirm(self, message: str, *, default: bool = False) -> bool:
        from InquirerPy import inquirer

        return inquirer.confirm(message=message, default=default).execute()


def make_prompter() -> Prompter:
    """Return the production prompter.

    A factory (rather than instantiating at import time) keeps this module
    import-cheap and gives PR 4 / future flows a single place to swap the
    backend if needed.
    """
    return InquirerPrompter()
