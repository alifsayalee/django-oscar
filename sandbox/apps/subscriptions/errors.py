"""
The one place Maxio SDK failures become this app's failures.

Every SDK call made by the service layer runs inside ``provider_errors()``,
which applies the same ladder everywhere:

* 401/403 (our credentials) -> 502, 429 (our quota) -> 503
* any other 4xx -> the same 4xx, with the provider's messages
* 5xx -> 502
* a body that does not decode -> 502 (unreadable)
* a transport failure that never left -> 502, known outcome
* a transport failure after sending -> 504, outcome unknown
"""
import contextlib
import logging
from collections.abc import Iterator

import httpx
from django.core.exceptions import ImproperlyConfigured
from maxio_advanced_billing.core import ApiError, RawError
from maxio_advanced_billing.models import CustomerError, CustomerErrorResponse1, ErrorListResponse1

logger = logging.getLogger('apps.subscriptions')

# Failures raised before the request left: nothing can have happened at Maxio.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class BillingError(Exception):
    status_code = 500
    code = 'billing_error'

    def __init__(self, message: str, *, status_code: int | None = None,
                 outcome_unknown: bool = False, details: list[str] | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.outcome_unknown = outcome_unknown
        self.details = details or []


class BillingNotConfigured(BillingError):
    status_code = 503
    code = 'billing_not_configured'


class InvalidRequest(BillingError):
    status_code = 400
    code = 'invalid_request'


class NotFound(BillingError):
    status_code = 404
    code = 'not_found'


class InProgress(BillingError):
    """Another request holds the claim for this write and is still waiting on Maxio."""
    status_code = 409
    code = 'in_progress'


class ProviderConfigError(BillingError):
    status_code = 502
    code = 'billing_provider_refused_credentials'


class ProviderRejected(BillingError):
    status_code = 422
    code = 'billing_provider_rejected'


class ProviderUnavailable(BillingError):
    status_code = 502
    code = 'billing_provider_unavailable'


class OutcomeUnknown(BillingError):
    status_code = 504
    code = 'outcome_unknown'

    def __init__(self, reference: str) -> None:
        super().__init__(
            'Maxio did not confirm the outcome yet; the request is recorded and will be '
            'reconciled. Repeat the same request to check on it.',
            outcome_unknown=True)
        self.reference = reference


def provider_messages(error: object) -> list[str]:
    """The human-readable messages carried by a typed Maxio error body, if any."""
    if isinstance(error, ErrorListResponse1):
        return [str(m) for m in error.errors]
    if isinstance(error, CustomerErrorResponse1):
        errors = error.errors
        if isinstance(errors, list):
            return [str(m) for m in errors]
        if isinstance(errors, CustomerError) and isinstance(errors.customer, str):
            return [errors.customer]
    if isinstance(error, str) and error:
        return [error]
    return []


def raw_text(error: object) -> str:
    if isinstance(error, RawError):
        return error.text()[:500]
    return ''


@contextlib.contextmanager
def provider_errors(*, write: bool = False) -> Iterator[None]:
    """Translate SDK failures raised inside the block (see module docstring)."""
    try:
        yield
    except ImproperlyConfigured as e:
        logger.error('Maxio is not configured: %s', e)
        raise BillingNotConfigured('Subscription billing is not configured.') from e
    except ApiError as e:
        status = e.status_code
        logger.warning('Maxio answered HTTP %s (%s) %s', status, type(e.error).__name__, raw_text(e.error))
        if status in (401, 403):
            raise ProviderConfigError('The billing provider refused our credentials.') from e
        if status == 429:
            raise ProviderUnavailable('The billing provider is rate-limiting us; try again shortly.',
                                      status_code=503) from e
        if 400 <= status < 500:
            messages = provider_messages(e.error)
            raise ProviderRejected(
                'The billing provider rejected the request.',
                status_code=status if status in (404, 409, 422) else 422,
                details=messages) from e
        raise ProviderUnavailable('The billing provider is unavailable.',
                                  outcome_unknown=write) from e
    except NEVER_SENT as e:
        logger.warning('Maxio unreachable (never sent): %s', type(e).__name__)
        raise ProviderUnavailable('The billing provider could not be reached.') from e
    except httpx.RequestError as e:
        logger.warning('Maxio request failed after sending: %s', type(e).__name__)
        raise ProviderUnavailable('The billing provider did not answer.',
                                  status_code=504, outcome_unknown=True) from e
    except ValueError as e:   # pydantic ValidationError, or a non-JSON body
        logger.error('Unreadable Maxio response: %s', type(e).__name__)
        raise ProviderUnavailable('The billing provider sent an unreadable response.',
                                  outcome_unknown=write) from e
