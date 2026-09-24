"""
The one place a Maxio failure becomes this app's failure.

Every SDK call goes through ``provider_call()``, or through ``safe_write()``
for writes, so the same kind of failure answers the same way on every route.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from maxio_advanced_billing.core import ApiError, RawError, SdkBaseModel
from maxio_advanced_billing.models import ErrorListResponse1

# Failures raised before the request left: nothing can have happened upstream.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Provider statuses that are a verdict on the caller's input, passed through.
CALLER_FAULT_STATUSES = (400, 404, 409, 422)


class ProviderError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *,
                 outcome_unknown: bool = False, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code          # what our API answers
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown  # may something have happened upstream?
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            'error': self.code, 'message': self.message, 'outcomeUnknown': self.outcome_unknown}
        if self.detail is not None:
            body['detail'] = self.detail
        return body


class OutcomeUnknown(ProviderError):
    """A write may have landed and a lookup by its reference could not settle it."""

    def __init__(self, reference: str) -> None:
        super().__init__(
            504, 'outcome_unknown',
            'Billing did not confirm the outcome yet. Repeat the same request to check again.',
            outcome_unknown=True, detail={'reference': reference})
        self.reference = reference


class InvalidRequest(ProviderError):
    def __init__(self, status_code: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(status_code, code, message, detail=detail)


def error_detail(error: object) -> Any:
    """The provider's error body as something JSON-safe, for the caller or a log."""
    if isinstance(error, ErrorListResponse1):
        return error.errors
    if isinstance(error, SdkBaseModel):
        return error.to_dict(exclude_unset=True)
    if isinstance(error, RawError):
        return error.text()[:500] or None
    if isinstance(error, str):
        return error[:500]
    return None


def from_api_error(e: ApiError[Any]) -> ProviderError:
    status = e.status_code
    if status in CALLER_FAULT_STATUSES:
        return ProviderError(status, 'rejected_by_billing', 'Billing rejected the request.',
                             detail=error_detail(e.error))
    if status in (401, 403):
        # Our credentials, not the caller's.
        return ProviderError(502, 'billing_unavailable', 'Billing is unavailable.')
    if status == 429:
        return ProviderError(503, 'billing_busy', 'Billing is busy; try again shortly.')
    return ProviderError(502, 'billing_unavailable', 'Billing is unavailable.')


@contextmanager
def provider_call() -> Iterator[None]:
    """Translate a failed read (or a write whose outcome is already known) into ProviderError."""
    try:
        yield
    except ApiError as e:
        raise from_api_error(e) from e
    except NEVER_SENT as e:
        raise ProviderError(502, 'billing_unreachable', 'Billing could not be reached.') from e
    except httpx.RequestError as e:
        raise ProviderError(504, 'billing_timeout', 'Billing did not answer in time.') from e
    except ValueError as e:  # pydantic ValidationError, or a non-JSON body
        raise ProviderError(502, 'billing_unreadable', 'Billing sent an unreadable answer.') from e
