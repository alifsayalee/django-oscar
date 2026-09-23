"""Application-facing errors the PayPal gateway raises.

The gateway converts every SDK failure kind into exactly one of these, so views
and services reason about a single hierarchy with an HTTP status attached,
rather than about httpx/pydantic/ApiError internals.
"""


class PayPalError(Exception):
    """Base class. ``http_status`` is what our own API should answer."""

    http_status = 502
    # Whether the upstream write may have taken effect despite the failure.
    outcome_unknown = False

    def __init__(self, message, *, http_status=None, outcome_unknown=None):
        super().__init__(message)
        self.message = message
        if http_status is not None:
            self.http_status = http_status
        if outcome_unknown is not None:
            self.outcome_unknown = outcome_unknown


class PayPalConfigError(PayPalError):
    """Our credentials/scopes were rejected (401/403/token fetch). Never the caller's fault."""

    http_status = 502


class PayPalUnavailable(PayPalError):
    """Transport failure, 5xx, rate limiting -- upstream problem, not the caller's."""

    http_status = 502


class PayPalRejected(PayPalError):
    """PayPal rejected the request for a reason the caller can act on (400/404/409/422)."""

    def __init__(self, status_code, message):
        super().__init__(message, http_status=status_code)
        self.status_code = status_code


class PayPalUnreadable(PayPalError):
    """A response could not be decoded; the outcome is unknown."""

    http_status = 502
    outcome_unknown = True


class PaymentChallengeRequired(PayPalError):
    """PayPal asked for a browser approval (3DS/PAYER_ACTION_REQUIRED).

    Per the task mandate we stop and report this rather than building an
    approval round-trip.
    """

    http_status = 502
