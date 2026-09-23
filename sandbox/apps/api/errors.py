"""Boundary error types for the checkout API.

Every failure the API can produce is one of these, each carrying the HTTP status
the caller should see. The gateway layer (``paypal_gateway``) translates the
PayPal SDK's failures into these, following the mapping the ``python-error-handling``
guidance prescribes:

* the caller's own fault (bad input, unknown reference, real conflict) -> 400/404/409/422
* *our* fault talking to PayPal (credentials, quota) -> 502/503, never surfaced as the caller's
* a request that provably never reached PayPal -> 502, outcome known (nothing happened)
* a request that may have landed (timeout, dropped socket) -> 504, outcome unknown
"""


class ApiError(Exception):
    """Base class for anything we turn into a JSON error response.

    ``status_code`` is the HTTP status for the caller. ``outcome_unknown`` marks a
    write whose effect at PayPal we could not determine (it may have landed), so a
    caller/operator knows a retry is not automatically safe.
    """

    status_code = 400

    def __init__(self, message, *, status_code=None, code=None, outcome_unknown=False, extra=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.extra = extra or {}


class BadRequest(ApiError):
    status_code = 400


class NotFound(ApiError):
    status_code = 404


class Conflict(ApiError):
    """A legitimate conflict the caller can act on (e.g. wrong lifecycle state)."""

    status_code = 409


class Unprocessable(ApiError):
    status_code = 422


class PaymentChallengeRequired(ApiError):
    """PayPal asked for a browser approval (3-D Secure challenge).

    Per the task we STOP and report rather than building an approval round-trip.
    """

    status_code = 402


class ProviderConfigError(ApiError):
    """Our credentials/configuration were rejected by PayPal. Not the caller's fault."""

    status_code = 502


class ProviderUnavailable(ApiError):
    """PayPal was unreachable or failed on its side.

    ``outcome_unknown`` is False when the request provably never left (nothing
    happened) and True when it may have landed (reconcile before retrying).
    """

    status_code = 502
