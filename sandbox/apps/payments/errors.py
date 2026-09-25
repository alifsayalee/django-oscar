"""
The error boundary: every failure a PayPal call can produce becomes one
``ApiProblem`` that the views render as JSON.

``str()`` of an SDK exception is never shown to a caller, and nothing here
logs a request or response body.
"""
import logging

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError
from paypal.models import Error
from pydantic import ValidationError

logger = logging.getLogger('apps.payments')

# Failures raised before the request left: nothing can have reached PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class ApiProblem(Exception):
    """An error answer from this API."""

    def __init__(self, status_code, code, message, *, outcome_unknown=False, extra=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.extra = extra or {}

    def as_dict(self):
        body = {'error': self.code, 'message': self.message}
        if self.outcome_unknown:
            body['outcomeUnknown'] = True
        body.update(self.extra)
        return body


class OutcomeUnknown(ApiProblem):
    """A write may have reached PayPal and its result could not be read."""

    def __init__(self, ref, message=None):
        super().__init__(
            504, 'outcome_unknown',
            message or ('PayPal did not give a readable answer; the operation may have '
                        'happened. Repeat the same request to settle it.'),
            outcome_unknown=True, extra={'reference': ref})
        self.ref = ref


class AmountMismatch(ApiProblem):
    def __init__(self, ref, echoed_amount, echoed_currency):
        super().__init__(
            409, 'amount_mismatch',
            'PayPal reported %s %s, which is not the amount requested; the payment '
            'has been flagged for review.' % (echoed_amount, echoed_currency),
            extra={'reference': ref})


def provider_message(e: ApiError) -> tuple[str, list[str]]:
    """PayPal's own message and issue codes, when the body is the typed ``Error``."""
    if isinstance(e.error, Error):
        issues = []
        details = e.error.details
        if isinstance(details, list):
            issues = [d.issue for d in details]
        return e.error.message, issues
    return 'PayPal rejected the request (HTTP %s).' % e.status_code, []


def translate(exc: BaseException, *, action: str) -> ApiProblem:
    """Map an SDK / transport failure onto this API's answer."""
    if isinstance(exc, ApiProblem):
        return exc
    if isinstance(exc, ApiError):
        if isinstance(exc.error, OAuthProviderError) or exc.status_code in (401, 403):
            logger.error('PayPal refused our credentials during %s (HTTP %s)', action, exc.status_code)
            return ApiProblem(502, 'payment_provider_configuration',
                              'The payment provider refused this shop\'s credentials.')
        if exc.status_code == 429:
            return ApiProblem(503, 'payment_provider_busy',
                              'The payment provider is rate-limiting requests; try again shortly.')
        if 400 <= exc.status_code < 500:
            message, issues = provider_message(exc)
            if isinstance(exc.error, Error):
                logger.info('PayPal rejected %s: %s %s (debug id %s)', action,
                            exc.error.name, issues, exc.error.debug_id)
            elif isinstance(exc.error, RawError):
                logger.info('PayPal rejected %s with HTTP %s', action, exc.status_code)
            status = 409 if exc.status_code == 409 else 422
            return ApiProblem(status, 'payment_provider_rejected', message,
                              extra={'providerIssues': issues})
        return ApiProblem(502, 'payment_provider_error', 'The payment provider failed.',
                          outcome_unknown=True)
    if isinstance(exc, (ValidationError, ValueError)):
        logger.error('Unreadable PayPal response during %s', action)
        return ApiProblem(502, 'payment_provider_unreadable',
                          'The payment provider sent an unreadable answer.', outcome_unknown=True)
    if isinstance(exc, NEVER_SENT):
        logger.warning('PayPal unreachable during %s: %s', action, type(exc).__name__)
        return ApiProblem(502, 'payment_provider_unreachable',
                          'The payment provider could not be reached; nothing was sent.')
    if isinstance(exc, httpx.RequestError):
        logger.warning('No answer from PayPal during %s: %s', action, type(exc).__name__)
        return ApiProblem(504, 'payment_provider_timeout',
                          'The payment provider did not answer.', outcome_unknown=True)
    raise exc
