"""
Every failure the API reports, and the one map from PayPal failures onto them.

Callers see a JSON body ``{"error": {"code", "message", ...}}`` and a status
that says whose problem it is: a request PayPal refused because of what the
caller sent keeps PayPal's 4xx; our credentials or quota (401/403/429) and
PayPal outages are 5xx; a write that may have landed is 504 with
``outcomeUnknown`` set.
"""
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
from paypal.core import ApiError, OAuthProviderError, RawError
from paypal.models import Error

# Failures raised before the request left this process: nothing reached PayPal.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class ApiProblem(Exception):
    """An error answer of this API."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {'error': {'code': self.code, 'message': self.message, **self.extra}}


class ProviderError(ApiProblem):
    """PayPal failed or refused; ``outcome_unknown`` says whether it may have acted."""

    def __init__(self, status: int, code: str, message: str, *,
                 outcome_unknown: bool = False, paypal: dict[str, Any] | None = None) -> None:
        extra: dict[str, Any] = {'outcomeUnknown': outcome_unknown}
        if paypal:
            extra['paypal'] = paypal
        super().__init__(status, code, message, **extra)
        self.outcome_unknown = outcome_unknown
        self.paypal = paypal or {}


class OutcomeUnknown(ProviderError):
    """A write that may or may not have happened at PayPal; kept for a later check."""

    def __init__(self, ref: str) -> None:
        super().__init__(
            504, 'outcome_unknown',
            'PayPal did not confirm the outcome of this request. It has been '
            'recorded and will be checked; repeating the same request is safe.',
            outcome_unknown=True)
        self.ref = ref


class AmountMismatch(ProviderError):
    """PayPal acted, but not for the amount we asked for."""

    def __init__(self, ref: str, asked: object, got: object) -> None:
        super().__init__(
            409, 'needs_review',
            'PayPal processed an amount (%s) different from the one requested '
            '(%s); an operator must review this payment.' % (got, asked))
        self.ref = ref


def paypal_detail(error: object) -> dict[str, Any]:
    """The safe, loggable part of a PayPal error body (never request data)."""
    if isinstance(error, Error):
        issues = [
            {'issue': d.issue, **({'description': d.description} if isinstance(d.description, str) else {})}
            for d in (error.details if isinstance(error.details, list) else [])
        ]
        return {'name': error.name, 'message': error.message, 'debugId': error.debug_id, 'issues': issues}
    if isinstance(error, OAuthProviderError):
        return {'name': error.error}
    if isinstance(error, RawError):
        return {'status': error.status_code}
    return {}


def issues_of(error: object) -> set[str]:
    if isinstance(error, Error) and isinstance(error.details, list):
        return {d.issue for d in error.details}
    return set()


def provider_error(status: int, error: object) -> ProviderError:
    """Map a PayPal error answer onto this API's error."""
    detail = paypal_detail(error)
    if isinstance(error, OAuthProviderError) or status in (401, 403):
        return ProviderError(502, 'paypal_credentials_rejected',
                             'PayPal refused this shop\'s credentials.', paypal=detail)
    if status == 429:
        return ProviderError(503, 'paypal_rate_limited',
                             'PayPal is rate-limiting requests; try again shortly.', paypal=detail)
    if 400 <= status < 500:
        if isinstance(error, Error):
            issues = ', '.join(i['issue'] for i in detail.get('issues', []))
            message = 'PayPal refused the request: %s%s' % (
                error.message, ' (%s)' % issues if issues else '')
        else:
            message = 'PayPal refused the request.'
        return ProviderError(422 if status in (400, 422) else status,
                             'paypal_refused', message, paypal=detail)
    return ProviderError(502, 'paypal_unavailable', 'PayPal is unavailable.',
                         outcome_unknown=status >= 500, paypal=detail)


@contextmanager
def paypal_read() -> Iterator[None]:
    """Error boundary around a PayPal *read* (nothing to reconcile on failure)."""
    try:
        yield
    except ApiError as e:
        raise provider_error(e.status_code, e.error) from e
    except NEVER_SENT as e:
        raise ProviderError(502, 'paypal_unreachable', 'PayPal could not be reached.') from e
    except httpx.RequestError as e:
        raise ProviderError(504, 'paypal_timeout', 'PayPal did not answer in time.') from e
    except ValueError as e:  # pydantic ValidationError, or a body that is not JSON
        raise ProviderError(502, 'paypal_unreadable', 'PayPal returned an unreadable answer.') from e
