"""
One translation from a Maxio failure to what this API answers.
"""
import logging

import httpx
from maxio_advanced_billing.core import UNSET, ApiError, RawError
from maxio_advanced_billing.models import CustomerError, CustomerErrorResponse1, ErrorListResponse1

from .safe_write import NEVER_SENT

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False,
                 details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code  # the status this API answers
        self.message = message
        self.outcome_unknown = outcome_unknown  # whether anything may have happened upstream
        self.details = details or []


def provider_messages(error: object) -> list[str]:
    """The validation messages of a typed error body; nothing from a raw one."""
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return list(errors)
        if isinstance(errors, CustomerError) and errors.customer is not UNSET:
            return [str(errors.customer)]
        return []
    if isinstance(error, str):
        return [error]
    return []


def from_api_error(e: ApiError) -> ProviderError:
    status = e.status_code
    raw = e.error.text()[:500] if isinstance(e.error, RawError) else ''
    logger.warning('Maxio answered HTTP %s (%s) %s', status, type(e.error).__name__, raw)
    if status in (401, 403):
        # Our credentials, not the caller's.
        return ProviderError(502, 'The billing provider refused our credentials.')
    if status == 429:
        return ProviderError(503, 'The billing provider is rate-limiting requests; try again shortly.')
    if 400 <= status < 500:
        return ProviderError(status, 'The billing provider rejected the request.',
                             details=provider_messages(e.error))
    return ProviderError(502, 'The billing provider is unavailable.')


def translate(exc: Exception) -> ProviderError:
    """Map an exception raised by an SDK call to this API's answer."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        return from_api_error(exc)
    if isinstance(exc, NEVER_SENT):
        logger.warning('Maxio request never sent: %s', type(exc).__name__)
        return ProviderError(502, 'The billing provider could not be reached; nothing was sent.')
    if isinstance(exc, httpx.RequestError):
        logger.warning('Maxio request got no response: %s', type(exc).__name__)
        return ProviderError(504, 'The billing provider did not answer.', outcome_unknown=True)
    if isinstance(exc, ValueError):
        # pydantic's ValidationError or a non-JSON body on a 2xx.
        logger.warning('Unreadable Maxio response: %s', type(exc).__name__)
        return ProviderError(502, 'The billing provider sent an unreadable response.', outcome_unknown=True)
    raise exc
