"""
The one place a Maxio failure becomes this site's failure.

Every call site converts SDK/transport exceptions through
:func:`translate_provider_error`, so the same kind of failure always gets the
same HTTP status, and whether the provider may have acted is carried as a
flag rather than guessed by the caller.
"""
from __future__ import annotations

import logging

import httpx
from maxio_advanced_billing.core import ApiError, RawError
from maxio_advanced_billing.models import (
    CustomerError, CustomerErrorResponse1, ErrorListResponse1)

logger = logging.getLogger('apps.subscriptions.maxio')

# Failures that happen before the request leaves: nothing can have landed
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class BillingError(Exception):
    """A failure the API reports to its caller as ``status_code`` with a stable ``code``."""

    def __init__(self, status_code: int, code: str, message: str, *,
                 outcome_unknown: bool = False, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or []


class ProviderError(BillingError):
    """A Maxio failure, already mapped to the status this site answers with."""


class OutcomeUnknown(ProviderError):
    """A write may have landed at Maxio and a lookup could not settle it yet."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504, 'billing_outcome_unknown',
            'Maxio did not confirm the outcome yet; it is being reconciled. Retry the same request later.',
            outcome_unknown=True)
        self.reference = reference


def provider_messages(error: object) -> list[str]:
    """Human-readable messages from a typed Maxio error body (never from RawError)."""
    if isinstance(error, ErrorListResponse1):
        return [str(m) for m in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return [str(m) for m in errors]
        if isinstance(errors, CustomerError) and isinstance(errors.customer, str):
            return [errors.customer]
    return []


def translate_provider_error(e: Exception, *, write: bool = False) -> ProviderError:
    """
    Map an exception raised by a Maxio call to a ProviderError.

    ``write`` marks a call that may have changed state at Maxio: then a 5xx or
    an unanswered request carries ``outcome_unknown``.
    """
    if isinstance(e, ProviderError):
        return e
    if isinstance(e, ApiError):
        status = e.status_code
        if isinstance(e.error, RawError):
            logger.warning('Maxio HTTP %s: %s', status, e.error.text()[:500])
        else:
            logger.warning('Maxio HTTP %s: %s', status, type(e.error).__name__)
        if status in (401, 403):
            # Our credentials, not the caller's
            return ProviderError(502, 'billing_provider_auth', 'The billing provider refused our credentials.')
        if status == 429:
            return ProviderError(503, 'billing_provider_busy', 'The billing provider is rate limiting; retry later.')
        if 400 <= status < 500:
            messages = provider_messages(e.error)
            return ProviderError(
                status, 'billing_provider_rejected',
                'The billing provider rejected the request.', details=messages)
        return ProviderError(502, 'billing_provider_error', 'The billing provider failed.',
                             outcome_unknown=write)
    if isinstance(e, NEVER_SENT):
        logger.warning('Maxio unreachable: %s', type(e).__name__)
        return ProviderError(502, 'billing_provider_unreachable',
                             'The billing provider could not be reached.', outcome_unknown=False)
    if isinstance(e, httpx.RequestError):
        logger.warning('Maxio request failed after sending: %s', type(e).__name__)
        return ProviderError(504, 'billing_provider_timeout', 'The billing provider did not answer.',
                             outcome_unknown=write)
    if isinstance(e, ValueError):
        # pydantic's ValidationError is a ValueError, as is a non-JSON body
        logger.warning('Unreadable Maxio response: %s', type(e).__name__)
        return ProviderError(502, 'billing_provider_unreadable',
                             'The billing provider sent a response we could not read.',
                             outcome_unknown=write)
    raise e
