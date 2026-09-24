"""
The one place a Maxio SDK failure becomes this app's failure.

Every call site -- reads included -- goes through ``translate`` so the same
kind of failure gets the same HTTP answer everywhere.
"""
from __future__ import annotations

import logging

import httpx
from maxio_advanced_billing.core import ApiError
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1

logger = logging.getLogger('apps.subscriptions.maxio')

# Failures raised before the request left: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Provider statuses that are a verdict on the caller's input -- a named list, not a range.
CALLER_FAULT_STATUSES = (400, 404, 409, 422)


class BillingError(Exception):
    """A failure with the HTTP status this API answers and whether anything may have happened upstream."""

    def __init__(self, status_code: int, code: str, message: str, *, outcome_unknown: bool = False,
                 details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.details = details or []


class BillingNotConfigured(BillingError):
    def __init__(self) -> None:
        super().__init__(503, 'billing_not_configured', 'Subscription billing is not configured.')


class ProviderRejected(BillingError):
    """The provider refused the caller's request (their input)."""


class ProviderUnavailable(BillingError):
    """The provider could not be reached, refused *us*, or answered unreadably."""


class OutcomeUnknown(BillingError):
    """A write may have landed and a lookup by its reference could not confirm it either way."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504, 'outcome_unknown',
            'Billing did not confirm the request; it may still complete. Retry the same request later.',
            outcome_unknown=True)
        self.reference = reference


def provider_messages(error: object) -> list[str]:
    """The human-readable messages of a typed Maxio error body, if it carries any."""
    if isinstance(error, ErrorListResponse1):
        return [str(message) for message in error.errors]
    if isinstance(error, CustomerErrorResponse1) and isinstance(error.errors, list):
        return [str(message) for message in error.errors]
    return []


def translate(exc: Exception, *, action: str) -> BillingError:
    """
    Map an SDK / transport failure onto a ``BillingError``.

    Use for reads and for failures the safe write has already classified; the
    safe write itself decides whether a write failure may have landed.
    """
    if isinstance(exc, BillingError):
        return exc
    if isinstance(exc, ApiError):
        status = exc.status_code
        logger.warning('Maxio %s failed: HTTP %s', action, status)
        if status in CALLER_FAULT_STATUSES:
            return ProviderRejected(
                status, 'rejected_by_billing', 'Billing rejected the request.',
                details=provider_messages(exc.error))
        if status in (401, 403):
            logger.error('Maxio refused our credentials (HTTP %s) during %s', status, action)
            return ProviderUnavailable(502, 'billing_unavailable', 'Billing is unavailable.')
        if status == 429:
            return ProviderUnavailable(503, 'billing_rate_limited', 'Billing is busy; try again shortly.')
        return ProviderUnavailable(502, 'billing_unavailable', 'Billing is unavailable.',
                                   outcome_unknown=status >= 500)
    if isinstance(exc, NEVER_SENT):
        logger.warning('Maxio %s never sent: %s', action, type(exc).__name__)
        return ProviderUnavailable(502, 'billing_unreachable', 'Billing could not be reached.')
    if isinstance(exc, httpx.RequestError):
        logger.warning('Maxio %s got no response: %s', action, type(exc).__name__)
        return ProviderUnavailable(504, 'billing_no_response', 'Billing did not respond.', outcome_unknown=True)
    if isinstance(exc, ValueError):
        # pydantic.ValidationError is a ValueError: a body we could not read.
        logger.warning('Maxio %s returned an unreadable body: %s', action, type(exc).__name__)
        return ProviderUnavailable(502, 'billing_unreadable', 'Billing returned an unreadable response.')
    raise exc
