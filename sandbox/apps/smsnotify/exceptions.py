"""Boundary exception types for the Twilio integration.

The gateway converts every SDK failure kind (ApiError, pydantic decode failure,
httpx transport failure) into one of these, so the rest of the app has a single,
small failure vocabulary and callers never see ``str(ApiError)`` or a traceback.
"""


class ProviderError(Exception):
    """Base for anything that went wrong talking to Twilio."""

    def __init__(self, message, *, status_code=502, outcome_unknown=False):
        super().__init__(message)
        self.message = message
        #: The HTTP status this maps to at our own API boundary.
        self.status_code = status_code
        #: Whether a write may have taken effect upstream despite the failure.
        self.outcome_unknown = outcome_unknown


class ProviderConfigError(ProviderError):
    """Our credentials/configuration were refused (401/403). Not the caller's fault."""

    def __init__(self, message="Messaging provider rejected our credentials."):
        super().__init__(message, status_code=502, outcome_unknown=False)


class ProviderUnavailable(ProviderError):
    """Provider is down, rate-limited, or unreachable."""


class ProviderRejected(ProviderError):
    """The provider rejected the request for a reason the caller can act on.

    Used e.g. for Lookup 404 (not a usable destination). ``detail`` carries a
    safe, provider-supplied reason where available.
    """

    def __init__(self, status_code, detail):
        super().__init__(detail, status_code=status_code, outcome_unknown=False)
        self.detail = detail


class ProviderUnreadable(ProviderError):
    """A response could not be decoded; the outcome is unknown."""

    def __init__(self, message="Provider response was unreadable; outcome unknown."):
        super().__init__(message, status_code=502, outcome_unknown=True)
