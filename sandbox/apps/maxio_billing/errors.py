"""
The one place Maxio failures become this app's failure type.

Every SDK call - reads included - goes through `guarded_read` or the safe write,
so a caller sees one exception type carrying the status to answer with and
whether anything may have happened at Maxio.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from maxio_advanced_billing.core import ApiError
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1

# Failures raised before the request left: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


class BillingError(Exception):
    """A failure this app answers with `status_code` and a message written for the caller."""

    def __init__(self, status_code: int, message: str, *, code: str = 'error',
                 details: list[str] | None = None, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.details = details or []
        # Whether the provider may have acted even though no answer arrived.
        self.outcome_unknown = outcome_unknown


class ProviderError(BillingError):
    """Maxio refused, failed, or could not be reached."""


class OutcomeUnknown(BillingError):
    """A write may have landed at Maxio and a lookup by its reference could not settle it."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504, 'The billing provider did not confirm the outcome yet. Retry the same request '
            'to check it; it will not create a second one.',
            code='outcome_unknown', outcome_unknown=True)
        self.reference = reference


class BillingNotConfigured(BillingError):
    def __init__(self) -> None:
        super().__init__(503, 'Subscription billing is not configured on this site.',
                         code='billing_not_configured')


def _flatten(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [m for item in value for m in _flatten(item)]
    if isinstance(value, dict):
        return [f'{key}: {m}' for key, item in value.items() for m in _flatten(item)]
    return [str(value)]


def provider_messages(error: object) -> list[str]:
    """The provider's own validation messages, for the typed error arms in scope."""
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        return _flatten(error.to_dict(exclude_unset=True).get('errors', {}))
    if isinstance(error, str):
        return [error]
    return []


def provider_error(status: int, error: object) -> ProviderError:
    """Map a Maxio error status + decoded body to what this app answers."""
    if status in (401, 403):
        # Our credentials, not the caller's: never pass a 401 through.
        return ProviderError(502, 'The billing provider refused our credentials.',
                             code='provider_auth')
    if status == 429:
        return ProviderError(503, 'The billing provider is rate limiting; try again shortly.',
                             code='provider_rate_limited')
    if 400 <= status < 500:
        return ProviderError(status, 'The billing provider rejected the request.',
                             code='provider_rejected', details=provider_messages(error))
    return ProviderError(502, 'The billing provider is unavailable.', code='provider_unavailable',
                         outcome_unknown=status >= 500)


@contextmanager
def guarded_read() -> Iterator[None]:
    """Error ladder for a read: nothing to reconcile, but every failure kind still mapped."""
    try:
        yield
    except ApiError as e:
        raise provider_error(e.status_code, e.error) from e
    except NEVER_SENT as e:
        raise ProviderError(502, 'The billing provider could not be reached.',
                            code='provider_unreachable') from e
    except httpx.RequestError as e:
        raise ProviderError(504, 'The billing provider did not answer in time.',
                            code='provider_timeout') from e
    except ValueError as e:  # pydantic ValidationError, or a non-JSON body
        raise ProviderError(502, 'The billing provider sent an unreadable response.',
                            code='provider_unreadable') from e
