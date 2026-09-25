"""
The one failure type the API layer answers from, and the one place a PayPal
SDK failure becomes it.
"""
from typing import Any

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError
from paypal.models import DefaultError, Error

# Failures raised before the request left: nothing can have happened at PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class PaymentAPIError(Exception):
    """
    A failure with the HTTP status, a stable machine code and a message
    written for the caller. ``outcome_unknown`` says whether something may
    have happened at PayPal that this app could not confirm.
    """

    def __init__(self, status, code, message, *, outcome_unknown=False, paypal=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.paypal = paypal or {}

    def as_dict(self):
        body = {'code': self.code, 'message': self.message}
        if self.outcome_unknown:
            body['outcomeUnknown'] = True
        if self.paypal:
            body['paypal'] = self.paypal
        return {'error': body}


def paypal_detail(e: ApiError) -> dict:
    """The fields of PayPal's error body worth showing; never the raw body."""
    err = e.error
    if isinstance(err, (Error, DefaultError)):
        detail: dict[str, Any] = {'name': err.name, 'message': err.message, 'debugId': err.debug_id}
        issues = []
        for d in getattr(err, 'details', None) or []:
            issue = {'issue': d.issue}
            if isinstance(d.description, str):
                issue['description'] = d.description
            issues.append(issue)
        if issues:
            detail['issues'] = issues
        return detail
    if isinstance(err, OAuthProviderError):
        return {'name': str(err.error)}
    if isinstance(err, RawError):
        return {'status': err.status_code}
    return {}


def issues_of(e: ApiError) -> set[str]:
    return {i['issue'] for i in paypal_detail(e).get('issues', [])}


def summarize(e: ApiError) -> str:
    detail = paypal_detail(e)
    parts = [str(detail.get('name') or 'HTTP %s' % e.status_code)]
    for issue in detail.get('issues', []):
        parts.append('%s: %s' % (issue['issue'], issue.get('description', '')))
    return '; '.join(parts)


def translate(e: Exception, *, action: str) -> PaymentAPIError:
    """
    Map a failed PayPal call onto our boundary. Applied the same way at every
    call site; the safe write handles the may-have-landed cases before this.
    """
    if isinstance(e, ApiError):
        if isinstance(e.error, OAuthProviderError) or e.status_code in (401, 403):
            return PaymentAPIError(
                502, 'paypal_configuration_error',
                'PayPal refused this site\'s API credentials.', paypal=paypal_detail(e))
        if e.status_code == 429:
            return PaymentAPIError(503, 'paypal_rate_limited', 'PayPal is rate-limiting requests; try again shortly.')
        if 400 <= e.status_code < 500:
            return PaymentAPIError(
                422 if e.status_code in (400, 422) else e.status_code, 'paypal_rejected',
                'PayPal rejected the %s: %s' % (action, summarize(e)), paypal=paypal_detail(e))
        return PaymentAPIError(
            502, 'paypal_unavailable', 'PayPal failed while processing the %s.' % action,
            outcome_unknown=True, paypal=paypal_detail(e))
    if isinstance(e, NEVER_SENT):
        return PaymentAPIError(502, 'paypal_unreachable', 'PayPal could not be reached; nothing was sent.')
    if isinstance(e, httpx.RequestError):
        return PaymentAPIError(
            504, 'paypal_no_response', 'PayPal did not answer the %s.' % action, outcome_unknown=True)
    if isinstance(e, ValueError):
        return PaymentAPIError(
            502, 'paypal_unreadable', 'PayPal\'s answer to the %s could not be read.' % action,
            outcome_unknown=True)
    raise e
