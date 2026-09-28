"""
The single error boundary for Maxio calls: every SDK, transport and decode
failure becomes a ``ProviderError`` carrying the status we answer with and
whether the call may have taken effect at Maxio.
"""
import logging

import httpx
from maxio_advanced_billing.core import ApiError, RawError
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1

from .maxio import NEVER_SENT

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *,
                 outcome_unknown: bool = False, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or []


class OutcomeUnknown(Exception):
    """A write may or may not have landed at Maxio; only Maxio's answer settles it."""

    def __init__(self, reference: str) -> None:
        super().__init__(reference)
        self.reference = reference


class InvalidRequest(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_messages(error: object) -> list[str]:
    """The provider's own validation messages, from whichever typed arm arrived."""
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1) and isinstance(error.errors, list):
        return [str(message) for message in error.errors]
    if isinstance(error, str) and error:
        return [error]
    return []


def provider_error(status: int, error: object) -> ProviderError:
    """Map Maxio's answer to the error our caller sees."""
    if status in (401, 403):
        return ProviderError(502, 'billing_unavailable', 'The billing provider refused our credentials.')
    if status == 429:
        return ProviderError(503, 'billing_rate_limited', 'The billing provider is rate limiting; retry shortly.')
    if 400 <= status < 500:
        # The provider rejected what this caller asked for: hand the status back.
        return ProviderError(status, 'billing_rejected', 'The billing provider rejected the request.',
                             details=error_messages(error))
    return ProviderError(502, 'billing_unavailable', 'The billing provider is unavailable.',
                         outcome_unknown=status >= 500)


def translate(exc: Exception, *, operation: str) -> ProviderError:
    """Translate a failure of a *read* into a ProviderError. Reads change nothing at Maxio."""
    if isinstance(exc, ApiError):
        if isinstance(exc.error, RawError):
            logger.warning('Maxio %s failed: HTTP %s', operation, exc.status_code)
        else:
            logger.warning('Maxio %s failed: HTTP %s %s', operation, exc.status_code,
                           error_messages(exc.error))
        return provider_error(exc.status_code, exc.error)
    if isinstance(exc, NEVER_SENT):
        logger.warning('Maxio %s never sent: %s', operation, type(exc).__name__)
        return ProviderError(502, 'billing_unavailable', 'Could not reach the billing provider.')
    if isinstance(exc, httpx.RequestError):
        logger.warning('Maxio %s got no response: %s', operation, type(exc).__name__)
        return ProviderError(504, 'billing_timeout', 'The billing provider did not respond.')
    if isinstance(exc, ValueError):  # pydantic.ValidationError, or a non-JSON body
        logger.error('Maxio %s returned an unreadable body: %s', operation, type(exc).__name__)
        return ProviderError(502, 'billing_unreadable', 'The billing provider sent an unreadable response.')
    raise exc
