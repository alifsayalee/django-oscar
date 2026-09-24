"""
Every failure the API can answer with, and the one ladder that turns an SDK
or transport failure into one of them.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError, UnsetType
from paypal.models import Error

logger = logging.getLogger(__name__)

# Failures raised before the request left: nothing can have happened at PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# PayPal statuses that are a verdict on the caller's request.
CALLER_STATUSES = (400, 404, 409, 422)


class ApiProblem(Exception):
    """An error answer from this API: HTTP status, a stable code and a message written here."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {'code': self.code, 'message': self.message}
        body.update(self.extra)
        return {'error': body}


class ProviderError(ApiProblem):
    """A PayPal failure. ``outcome_unknown`` says whether anything may have happened at PayPal."""

    def __init__(self, status: int, code: str, message: str, *, outcome_unknown: bool, **extra: Any) -> None:
        super().__init__(status, code, message, outcome_unknown=outcome_unknown, **extra)
        self.outcome_unknown = outcome_unknown


class ProviderRejected(ProviderError):
    """PayPal refused the request itself (400/404/409/422): nothing happened."""

    def __init__(self, status: int, message: str, *, issue: str = '', description: str = '',
                 debug_id: str = '') -> None:
        super().__init__(422 if status == 400 else status, 'paypal_rejected', message,
                         outcome_unknown=False, paypalIssue=issue or None,
                         paypalDescription=description or None, paypalDebugId=debug_id or None)
        self.provider_status = status
        self.issue = issue
        self.description = description


class ProviderConfigError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__(502, 'paypal_configuration', message, outcome_unknown=False)


class ProviderUnavailable(ProviderError):
    pass


class OutcomeUnknown(ProviderError):
    """The write may have happened; the claim stays 'unknown' and is re-checked under the same reference."""

    def __init__(self, reference: str) -> None:
        super().__init__(504, 'outcome_unknown',
                         'PayPal did not confirm the outcome. Nothing will be repeated under a new '
                         'reference; retry the same request to re-check it.',
                         outcome_unknown=True, reference=reference)
        self.reference = reference


class AmountMismatch(ProviderError):
    def __init__(self, reference: str, amount: Any, currency: Any) -> None:
        super().__init__(409, 'amount_mismatch',
                         'PayPal recorded a different amount than was requested; the payment needs review.',
                         outcome_unknown=False, reference=reference,
                         paypalAmount=None if amount is None else str(amount), paypalCurrency=currency)


class InProgress(ApiProblem):
    def __init__(self, reference: str) -> None:
        super().__init__(409, 'in_progress',
                         'The same operation is already being processed; retry shortly.', reference=reference)


def _first_issue(error: Error) -> tuple[str, str]:
    details = error.details
    if isinstance(details, UnsetType) or not details:
        return '', ''
    first = details[0]
    description = first.description if not isinstance(first.description, UnsetType) else ''
    return first.issue, description


def rejection_details(exc: ApiError[Any]) -> tuple[str, str, str]:
    """(issue, description, debug_id) from a typed PayPal error body; empty strings otherwise."""
    if isinstance(exc.error, Error):
        issue, description = _first_issue(exc.error)
        return issue, description, exc.error.debug_id
    return '', '', exc.response.headers.get('paypal-debug-id', '')


def translate(exc: BaseException) -> ProviderError:
    """The one mapping from an SDK/transport failure to this API's answer."""
    if isinstance(exc, ApiError):
        if isinstance(exc.error, OAuthProviderError):
            logger.error('PayPal rejected the API credentials: %s', exc.error.error)
            return ProviderConfigError('PayPal rejected this site\'s API credentials.')
        status = exc.status_code
        issue, description, debug_id = rejection_details(exc)
        if status in (401, 403):
            logger.error('PayPal refused the call (%s, %s, debug_id=%s)', status, issue, debug_id)
            return ProviderConfigError('PayPal refused the call for this merchant account.')
        if status == 429:
            return ProviderUnavailable(503, 'paypal_rate_limited', 'PayPal is rate limiting; try again later.',
                                       outcome_unknown=False)
        if isinstance(exc.error, Error) and status in CALLER_STATUSES:
            message = description or exc.error.message
            return ProviderRejected(status, message, issue=issue, description=description, debug_id=debug_id)
        body = exc.error.text()[:200] if isinstance(exc.error, RawError) else ''
        logger.error('PayPal answered HTTP %s (debug_id=%s) %s', status, debug_id, body)
        return ProviderUnavailable(502, 'paypal_unavailable', 'PayPal could not process the request.',
                                   outcome_unknown=status >= 500)
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable(502, 'paypal_unreachable', 'PayPal could not be reached; nothing was sent.',
                                   outcome_unknown=False)
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(504, 'paypal_no_response', 'PayPal did not answer in time.',
                                   outcome_unknown=True)
    if isinstance(exc, ValueError):  # pydantic ValidationError or a non-JSON body
        return ProviderUnavailable(504, 'paypal_unreadable', 'PayPal\'s answer could not be read.',
                                   outcome_unknown=True)
    raise exc


__all__ = [
    'NEVER_SENT', 'ApiProblem', 'ProviderError', 'ProviderRejected', 'ProviderConfigError',
    'ProviderUnavailable', 'OutcomeUnknown', 'AmountMismatch', 'InProgress', 'translate',
    'rejection_details',
]
