"""Cross-process fail-closed contract for the ambient-routing pipeline.

The ambient → router → fan-out → synthesized-in chain carries the
Discord :class:`~calfcord.bridge.wire.WireMessage` (and, on the ambient
hop, the phonebook and channel history) on calfkit's ``deps`` channel.
Each consumer reads those keys back from ``result.deps`` — the same
bare dict a tool reads as ``ctx.deps["key"]`` — and validates them
against their domain models.

A missing or malformed key is an **infrastructure contract violation**,
not an LLM-recoverable input problem: the upstream producer (the bridge
ingress, or the fan-out itself) is contractually required to pack a
well-formed payload. Per the project's error-handling convention such
failures ``raise`` rather than returning an ``"error: ..."`` string.

Kafka's ``AckPolicy.ACK_FIRST`` means re-raising produces no
redelivery — the envelope is already ACKed, so an operator ERROR log is
the only signal. :func:`raise_routing_contract_error` logs at ERROR and
raises so the consumer framework's ``exc_info`` trace surfaces the
cause.

This module is deliberately dependency-free (stdlib only): both the
router's fan-out consumer and the bridge's synthesized-in consumer
import it, and keeping it free of any ``calfcord.bridge`` /
``calfcord.agents`` import avoids the package-init import cycle that the
predecessor ``_compat`` shim had to work around with bottom-of-module
imports. Consumers do their own domain-model validation inline (the
same ``WireMessage.model_validate(ctx.deps["discord"])`` idiom the gates
and ``private_chat`` already use).
"""

from __future__ import annotations

import logging
from typing import NoReturn

logger = logging.getLogger(__name__)


class RoutingContractError(RuntimeError):
    """Raised when an ambient-routing consumer reads a malformed/missing deps payload.

    Subclass of :class:`RuntimeError` (not :class:`ValueError`) because
    this signals an infrastructure contract violation, not an
    input-validation outcome.

    Carries structured context (``correlation_id``, ``site``,
    ``reason``) so tests can assert on attributes rather than
    substring-matching messages, and so future structured-logging
    transport can extract fields cleanly.
    """

    def __init__(
        self,
        *,
        correlation_id: str,
        site: str,  # "fanout" or "synthesized-in"
        reason: str,
        cause: Exception | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.site = site
        self.reason = reason
        super().__init__(
            f"{site} infra error: {reason} correlation_id={correlation_id}"
        )
        if cause is not None:
            self.__cause__ = cause


def raise_routing_contract_error(
    *,
    correlation_id: str,
    site: str,
    reason: str,
    cause: Exception | None = None,
) -> NoReturn:
    """Log at ERROR and raise :class:`RoutingContractError`."""
    logger.error(
        "%s infra error: %s correlation_id=%s",
        site,
        reason,
        correlation_id,
        exc_info=cause is not None,
    )
    raise RoutingContractError(
        correlation_id=correlation_id,
        site=site,
        reason=reason,
        cause=cause,
    )
