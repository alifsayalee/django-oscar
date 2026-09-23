"""Application-facing failures for the Maxio subscription capability.

The service layer converts every SDK / transport / decode failure into one of these, so
views deal with a single failure type carrying the HTTP status to answer with and a
caller-safe message. Provider detail is logged, never surfaced verbatim.
"""


class SubscriptionError(Exception):
    """Base class. Carries the HTTP status the boundary should answer with."""

    status_code: int = 500

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class ConfigurationError(SubscriptionError):
    """The integration is misconfigured (missing credentials, bad settings) or the
    provider refused *us* (401/403). Never the caller's fault."""

    status_code = 502


class ProviderUnavailable(SubscriptionError):
    """A transport failure. ``outcome_unknown`` distinguishes 'never sent' (502, nothing
    happened) from 'may have landed' (504, reconcile before any retry)."""

    def __init__(self, message: str, *, status_code: int, outcome_unknown: bool) -> None:
        super().__init__(message, status_code=status_code)
        self.outcome_unknown = outcome_unknown


class ProviderUnreadable(SubscriptionError):
    """A response we could not decode. On a success body the outcome is unknown (502);
    on a rejection body only the detail was lost."""

    status_code = 502


class CallerError(SubscriptionError):
    """The caller's input is genuinely at fault (bad plan handle, not found, conflict,
    validation). The status is theirs to see."""

    status_code = 400


class ProviderRejected(SubscriptionError):
    """The provider rejected the request with a mapped 4xx we attribute to the caller."""

    status_code = 422
