"""Boundary exception types for the PayPal integration.

The service layer converts every failure kind the SDK can produce (see
python-error-handling) into exactly one of these, so views map a single, small
family of exceptions to HTTP status codes rather than reaching into SDK
internals. Each carries an ``http_status`` used by the view layer.
"""


class PayPalError(Exception):
    """Base class for all PayPal boundary failures."""

    http_status = 502

    def __init__(self, message, *, http_status=None, debug_id=None):
        super().__init__(message)
        self.message = message
        self.debug_id = debug_id
        if http_status is not None:
            self.http_status = http_status


class PayPalConfigError(PayPalError):
    """Credentials/token problem — nothing was ever sent to PayPal.

    This is our own misconfiguration, not a rejection of a valid request.
    """

    http_status = 500


class PayPalRejected(PayPalError):
    """PayPal returned a typed 4xx error for an otherwise well-formed request.

    Actionable by the caller; surfaced as a client 4xx.
    """

    def __init__(self, status_code, message, *, debug_id=None, name=None, details=None):
        super().__init__(message, http_status=status_code, debug_id=debug_id)
        self.name = name
        self.details = details or []


class PayPalFailure(PayPalError):
    """PayPal returned an undocumented/opaque error (RawError arm)."""

    http_status = 502


class PayPalUnreadable(PayPalError):
    """A response body could not be decoded — the outcome is unknown."""

    http_status = 502


class PayPalUnavailable(PayPalError):
    """A transport-level failure — PayPal could not be reached."""

    http_status = 503


class OperationConflict(PayPalError):
    """The requested action is not valid in the resource's current state.

    Used for domain-level guards (e.g. refunding more than was captured, or an
    authorization that can no longer be renewed) with an operator-actionable
    message.
    """

    http_status = 409
