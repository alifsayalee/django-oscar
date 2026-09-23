"""Domain-level exceptions for the PayPal checkout app.

The gateway translates every SDK/transport failure into one of these so the rest
of the code (and the HTTP layer) works with a single failure family carrying an
HTTP status and a caller-safe message.
"""


class PayPalError(Exception):
    """Base class. ``status_code`` is the HTTP status the API boundary returns."""

    status_code = 502
    # Whether the upstream request may have taken effect (timeout / dropped reply).
    outcome_unknown = False

    def __init__(self, message, *, status_code=None, outcome_unknown=None, detail=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if outcome_unknown is not None:
            self.outcome_unknown = outcome_unknown
        self.detail = detail


class PayPalConfigError(PayPalError):
    """Our credentials/configuration are wrong. Never the caller's fault."""

    status_code = 502


class PayPalRejected(PayPalError):
    """PayPal (or PayPal via us) rejected the request in a way the caller caused
    or an operator can act on — surfaced with the provider's own message."""

    status_code = 400


class PayPalUnavailable(PayPalError):
    """PayPal is unreachable or errored. ``outcome_unknown`` distinguishes a
    request that was never sent (502) from one that may have landed (504)."""

    status_code = 502


class PayPalUnreadable(PayPalError):
    """A response could not be decoded; the outcome is unknown."""

    status_code = 502
    outcome_unknown = True


# ---------------------------------------------------------------------------
# Application-level (not upstream) errors — bad request / state, caller's fault.
# ---------------------------------------------------------------------------

class ApiValidationError(PayPalError):
    status_code = 400


class NotFound(PayPalError):
    status_code = 404


class Conflict(PayPalError):
    """The requested action is not valid for the payment's current state, or an
    authorization can no longer be renewed. Message is operator-actionable."""

    status_code = 409
