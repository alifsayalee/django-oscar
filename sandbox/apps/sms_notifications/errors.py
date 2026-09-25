"""
Failure types raised by the SMS notification layer.

Every provider failure is translated into one of these in exactly one place
(``gateway.translate_provider_failure``) so views answer them uniformly.
``status_code`` is what our API answers; ``outcome_unknown`` says whether the
provider may have acted even though we have no answer.
"""


class NotificationError(Exception):
    status_code = 500
    code = 'error'

    def __init__(self, message: str, *, status_code: int | None = None,
                 code: str | None = None, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.outcome_unknown = outcome_unknown


class ProviderNotConfigured(NotificationError):
    status_code = 503
    code = 'provider_not_configured'


class ProviderError(NotificationError):
    """The provider could not be used, or answered in a way we cannot act on."""
    status_code = 502
    code = 'provider_error'


class ProviderRejected(ProviderError):
    """The provider refused the request itself (a 4xx the caller's input caused)."""
    code = 'provider_rejected'


class OutcomeUnknown(ProviderError):
    """A write may have landed at the provider and no lookup could settle it."""
    status_code = 504
    code = 'outcome_unknown'

    def __init__(self, reference: str, message: str = 'The provider outcome is not yet known.') -> None:
        super().__init__(message, outcome_unknown=True)
        self.reference = reference


class Conflict(NotificationError):
    status_code = 409
    code = 'conflict'


class InvalidRequest(NotificationError):
    status_code = 400
    code = 'invalid_request'


class NotFound(NotificationError):
    status_code = 404
    code = 'not_found'
