"""Domain exceptions for the PayPal integration.

Every failure kind that can cross the gateway boundary maps to exactly one of
these, each carrying the HTTP status the API layer should answer with. This
keeps the distinction the SDK's error skill insists on: a provider 4xx is the
caller's fault (surface 4xx), a transport/decode/config failure is not
(surface 5xx), and an unreadable *success* body is "outcome unknown" (5xx),
never a domain "no".
"""
from __future__ import annotations


class PaymentError(Exception):
    """Base for everything the gateway/service layer raises."""

    http_status = 400
    code = "payment_error"

    def __init__(self, message: str, *, code: str | None = None, detail=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail

    def with_status(self, http_status: int) -> "PaymentError":
        """Override the HTTP status and return self (for one-line raises)."""
        self.http_status = http_status
        return self


class PaymentRejected(PaymentError):
    """PayPal rejected the request (a 4xx we should echo to the caller)."""

    http_status = 422
    code = "payment_rejected"

    def __init__(self, message, *, provider_status=None, code=None, detail=None):
        super().__init__(message, code=code, detail=detail)
        self.provider_status = provider_status
        # Echo a client 4xx; 5xx from the provider is a provider failure.
        if provider_status and 400 <= provider_status < 500:
            self.http_status = provider_status


class PaymentConflict(PaymentError):
    """A state conflict the operator must act on (e.g. auth can't be renewed)."""

    http_status = 409
    code = "payment_conflict"


class PaymentConfigError(PaymentError):
    """Credentials/config rejected — nothing was sent. Our misconfiguration."""

    http_status = 502
    code = "payment_config_error"


class PaymentProviderError(PaymentError):
    """PayPal answered an undocumented/5xx error (RawError arm)."""

    http_status = 502
    code = "payment_provider_error"


class PaymentUnavailable(PaymentError):
    """Transport failure — provider unreachable; outcome unknown."""

    http_status = 504
    code = "payment_unavailable"


class PaymentUnreadable(PaymentError):
    """A response we could not decode/trust — outcome unknown, not a 'no'."""

    http_status = 502
    code = "payment_unreadable"


class ChallengeRequired(PaymentError):
    """PayPal requires a browser approval (3DS). We stop rather than build one."""

    http_status = 402
    code = "challenge_required"


class ReauthorizationNeeded(Exception):
    """Internal signal: the authorization is stale and capture must reauthorize.

    Not surfaced to callers — the service catches it and renews the auth.
    """
