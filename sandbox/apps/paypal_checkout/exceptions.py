"""Boundary exceptions for the PayPal checkout API.

Every failure the SDK can raise is translated into one of these in the gateway,
so the rest of the code (and the views) reason about a single, small failure
vocabulary rather than the SDK's exception zoo. Each carries the HTTP status the
API should return and, where relevant, whether the upstream outcome is unknown.
"""


class ApiClientError(Exception):
    """The API caller made a mistake (bad input, missing/foreign resource).

    Maps to a 4xx on our own API. ``status_code`` defaults to 400.
    """

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ProviderError(Exception):
    """Base class for failures that originate at (or in talking to) PayPal."""

    status_code = 502

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class ProviderConfigError(ProviderError):
    """Our credentials/configuration were rejected — nothing was attempted.

    This is *our* misconfiguration, never the caller's fault, so it surfaces as a
    502 rather than leaking a 401/403.
    """

    status_code = 502


class ProviderRejected(ProviderError):
    """PayPal rejected the request for a reason the caller can act on.

    Carries the provider status so genuinely caller-caused faults (400/404/409/
    422) can be passed through, while our-fault statuses (401/403/429) are mapped
    away by the view layer.
    """

    def __init__(self, message, status_code=422, provider_status=None, debug_id=None):
        super().__init__(message, status_code=status_code)
        self.provider_status = provider_status
        self.debug_id = debug_id


class ProviderUnavailable(ProviderError):
    """A transport failure. ``outcome_unknown`` distinguishes never-sent (known:
    nothing happened, 502) from no-reply (may have landed, 504)."""

    def __init__(self, message, status_code=502, *, outcome_unknown=False):
        super().__init__(message, status_code=status_code)
        self.outcome_unknown = outcome_unknown


class ProviderUnreadable(ProviderError):
    """A response body could not be decoded. On a write the outcome is unknown."""

    def __init__(self, message, *, outcome_unknown=True):
        super().__init__(message, status_code=502)
        self.outcome_unknown = outcome_unknown


class ChallengeRequired(ProviderError):
    """PayPal answered a card payment with a shopper challenge (e.g. 3-D Secure)
    that would require a browser approval round-trip.

    Per the task this is a STOP-and-report condition: we do not build an approval
    flow. With the sandbox test card this should not occur.
    """

    def __init__(self, message="Card payment requires shopper approval in a browser "
                               "(e.g. 3-D Secure); no browser approval flow is implemented."):
        super().__init__(message, status_code=409)
