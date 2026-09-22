"""Boundary error type for the subscriptions app.

Every Maxio SDK failure — a rejected request, an unreadable response, a transport
failure — is translated into a single :class:`MaxioServiceError` at the service
boundary (see ``services._translate``), so the view layer branches on one type and
one HTTP status. Provider response bodies are never surfaced verbatim to callers.
"""

from __future__ import annotations


class MaxioServiceError(Exception):
    """A failure talking to Maxio, already mapped to an HTTP status for our own API.

    ``status_code`` is the status our endpoint should return (not necessarily the
    provider's). ``outcome_unknown`` is ``True`` only when a write may have taken
    effect upstream even though we could not confirm it (a read-timeout, an
    unreadable 2xx body) — the signal an operator needs to reconcile rather than
    blindly retry.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown


class PlanNotFound(MaxioServiceError):
    """The requested plan handle is not one the configured product family offers."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)
