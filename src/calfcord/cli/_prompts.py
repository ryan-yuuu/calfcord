"""Prompt seam for the interactive CLI commands.

The flows in :mod:`calfcord.cli.init` and the ``agent tools`` editor
(:mod:`calfcord.cli.agent_tools`) must be unit-testable without a TTY, so they
never touch a prompting library directly. Instead they take a
:class:`Prompter` — a small Protocol covering the five prompt shapes we use
(single-select, free text, masked secret, yes/no, multi-select checkbox).
Tests inject a scripted fake; production injects :class:`InquirerPrompter`.

:class:`InquirerPrompter` imports InquirerPy **lazily inside each method** so
that merely importing this module (which the argparse entry point does at
startup, and which tests do) never requires InquirerPy or a TTY. The import
cost is paid only when a real prompt is actually shown.

Both ``select`` and ``checkbox`` take a ``list[Choice]`` — a named
``(value, label, checked)`` triple — rather than two unnamed tuple shapes, so a
label/value transposition is a type error rather than a silent UI bug.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable


class Choice(NamedTuple):
    """A selectable row for :meth:`Prompter.select` / :meth:`Prompter.checkbox`.

    ``value`` is what the prompter returns when the row is chosen; ``label`` is
    the human-readable text shown to the operator. ``checked`` pre-checks the
    row in a ``checkbox`` and is ignored by ``select`` (single-select has no
    pre-check concept). Naming the fields removes the value/label ordering
    ambiguity the two anonymous tuple shapes used to invite.
    """

    value: str
    label: str
    checked: bool = False


@runtime_checkable
class Prompter(Protocol):
    """The interactive operations the CLI flows depend on.

    A Protocol (not a base class) so a test fake satisfies it structurally and
    both interactive flows share the exact same seam. ``runtime_checkable`` lets
    callers/tests ``isinstance(obj, Prompter)`` as a cheap guard — it asserts the
    required methods are present, not that their signatures conform.
    """

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        """Single-choice select; returns the chosen :attr:`Choice.value` (``checked`` ignored)."""
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

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        """Multi-select; returns the selected :attr:`Choice.value`s (``checked`` pre-checks a row)."""
        ...


class InquirerPrompter:
    """Real :class:`Prompter` backed by InquirerPy.

    InquirerPy is imported inside each method on purpose — see the module
    docstring. Each method maps one-to-one onto an ``InquirerPy.inquirer``
    constructor and immediately ``.execute()``s it. InquirerPy's own ``Choice``
    is imported as ``InquirerChoice`` to avoid clashing with our :class:`Choice`.
    """

    def select(self, message: str, choices: list[Choice], *, default: str | None = None) -> str:
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice as InquirerChoice

        return inquirer.select(
            message=message,
            choices=[InquirerChoice(value=c.value, name=c.label) for c in choices],
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

    def checkbox(self, message: str, choices: list[Choice], *, instruction: str = "") -> list[str]:
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice as InquirerChoice

        # ``enabled`` pre-checks a row; ``.execute()`` on a checkbox returns the
        # list of selected ``Choice.value``s — i.e. our :attr:`Choice.value`.
        return inquirer.checkbox(
            message=message,
            choices=[InquirerChoice(value=c.value, name=c.label, enabled=c.checked) for c in choices],
            instruction=instruction,
        ).execute()


def make_prompter() -> Prompter:
    """Return the production prompter.

    A factory (rather than instantiating at import time) gives both interactive
    flows / future flows a single place to swap the backend if needed.
    """
    return InquirerPrompter()
