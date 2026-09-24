"""
Failures the subscription API reports to its callers.

Every error carries the HTTP status this API answers with and whether the
outcome of a write is unknown (it may have taken effect at Maxio).
"""
from collections.abc import Iterable


class BillingError(Exception):
    status_code = 502
    code = 'billing_error'

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None,
                 details: Iterable[str] = (), outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.details = list(details)
        self.outcome_unknown = outcome_unknown


class BillingNotConfigured(BillingError):
    status_code = 503
    code = 'billing_not_configured'


class ProviderRejected(BillingError):
    """Maxio rejected the request because of what the caller asked for (400/404/409/422)."""
    code = 'rejected'


class ProviderUnavailable(BillingError):
    """Maxio could not be reached, refused us, or failed; not the caller's fault."""
    code = 'billing_unavailable'


class ProviderUnreadable(ProviderUnavailable):
    """Maxio answered, but the answer could not be read."""
    code = 'billing_unreadable'


class UnknownPlan(BillingError):
    status_code = 422
    code = 'unknown_plan'


class SubscriptionInProgress(BillingError):
    status_code = 409
    code = 'subscription_in_progress'


class SubscriptionNotFound(BillingError):
    status_code = 404
    code = 'not_found'
