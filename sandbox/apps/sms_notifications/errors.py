"""
Failure types raised by this app's provider layer.

Every provider failure a view can see is a ``ProviderError`` carrying the HTTP
status this application answers with and whether anything may have happened
at the provider. Messages are written here; provider bodies and phone numbers
never reach them.
"""
from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str, *,
                 outcome_unknown: bool = False, code: str = 'provider_error'):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.code = code


class ProviderRejected(ProviderError):
    """The provider refused the request because of what the caller sent."""


class ProviderUnavailable(ProviderError):
    """The provider could not be reached, refused us, or its answer was lost."""


class TwilioNotConfigured(Exception):
    """Credentials are missing, so no request may be sent."""
