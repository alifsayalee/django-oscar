"""Domain exceptions for the PayPal checkout API.

Every failure the API can surface is one of these, each carrying the HTTP status
the view should return and a caller-safe message. The PayPal gateway
(:mod:`apps.paypal_checkout.gateway`) is the single place that translates the
SDK's failure kinds into these types, so views never see a raw ``ApiError``,
``httpx`` exception or ``pydantic.ValidationError``.
"""


class PayPalCheckoutError(Exception):
    """Base class. ``status_code`` is the HTTP status the view returns."""

    status_code: int = 500
    default_message = "An unexpected error occurred."

    def __init__(self, message: object = None, *, code: object = None,
                 details: object = None, status_code: "int | None" = None) -> None:
        self.message = message or self.default_message
        self.code = code
        self.details = details
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class BadRequest(PayPalCheckoutError):
    """The caller's request was malformed or invalid (400)."""

    status_code = 400
    default_message = "The request was invalid."


class NotFound(PayPalCheckoutError):
    """The referenced resource does not exist, or is not the caller's (404)."""

    status_code = 404
    default_message = "Not found."


class Conflict(PayPalCheckoutError):
    """The request conflicts with the resource's current state (409)."""

    status_code = 409
    default_message = "The request conflicts with the current state."


class ProviderRejected(PayPalCheckoutError):
    """PayPal rejected the request for a reason the caller can act on.

    Used for the caller-fault statuses (400/404/409/422) coming back from
    PayPal, carrying PayPal's own message so an operator or shopper can see why.
    """

    status_code = 422
    default_message = "PayPal rejected the request."


class ProviderConfigError(PayPalCheckoutError):
    """Our own credentials/configuration were refused by PayPal (502).

    Covers a failed OAuth token fetch and 401/403 responses -- never the
    caller's fault, so it is never surfaced as a 401/403 to them.
    """

    status_code = 502
    default_message = "Payment provider configuration error."


class ProviderUnavailable(PayPalCheckoutError):
    """PayPal is unreachable or returned a server error (502/503)."""

    status_code = 502
    default_message = "Payment provider is currently unavailable."


class ProviderUnreadable(PayPalCheckoutError):
    """PayPal answered but the response could not be read; outcome unknown (502)."""

    status_code = 502
    default_message = "Payment provider returned an unreadable response."


class PaymentActionRequired(PayPalCheckoutError):
    """PayPal requires a browser approval (challenge) to continue.

    The task mandates that we STOP and report such a challenge rather than
    building an approval round-trip, so the API returns this as an explicit
    error the operator can see.
    """

    status_code = 409
    default_message = (
        "PayPal requires additional buyer approval in a browser for this card. "
        "This build does not implement a browser approval round-trip."
    )
