"""
One failure ladder for every Maxio call.

Everything the SDK (or its httpx transport) can raise is translated here into a
``ProviderError`` carrying the status *our* API answers with and whether the
provider may have acted on the request (``outcome_unknown``).
"""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from maxio_advanced_billing.core import ApiError, RawError
from maxio_advanced_billing.models import CustomerErrorResponse1, ErrorListResponse1

# Failures raised before the request left this process: nothing can have landed.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)

# Maxio's answer to a create that reuses a reference (verified against the sandbox for
# both customers and subscriptions): the earlier attempt landed.
DUPLICATE_REFERENCE_MESSAGE = "Reference: must be unique"

# Provider statuses that are the caller's to fix, passed through by name.
CALLER_FAULT_STATUSES = (400, 409, 422)


class ProviderError(Exception):
    """Base for every failed Maxio interaction."""

    def __init__(self, status_code: int, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code  # the status our own API answers with
        self.message = message  # safe to show to our caller
        self.outcome_unknown = outcome_unknown  # the provider may have acted on it


class ProviderConfigError(ProviderError):
    """Our configuration or credentials are wrong - never the caller's fault."""


class ProviderNotFound(ProviderError):
    """The provider's own 404 - the only failure that means 'absent'."""


class ProviderRejected(ProviderError):
    """The provider refused the request; nothing happened."""

    def __init__(self, status_code: int, messages: list[str]) -> None:
        super().__init__(status_code, "; ".join(messages) or "Rejected by the billing provider.")
        self.messages = messages

    @property
    def duplicate_reference(self) -> bool:
        return any(DUPLICATE_REFERENCE_MESSAGE in m for m in self.messages)


class ProviderUnavailable(ProviderError):
    """Transport failure or rate limit."""


class ProviderUnreadable(ProviderError):
    """A response arrived that could not be decoded, or lacked a member we depend on."""


class ProviderFailure(ProviderError):
    """A provider-side error or an unmapped status."""


def _messages(error: object) -> list[str]:
    if isinstance(error, ErrorListResponse1):
        return list(error.errors)
    if isinstance(error, CustomerErrorResponse1):
        errors = error.to_dict().get("errors")
        if isinstance(errors, list):
            return [str(m) for m in errors]
        if isinstance(errors, dict):
            return [f"{k}: {v}" for k, v in errors.items()]
        return []
    if isinstance(error, RawError):
        try:
            body = error.json()
        except ValueError:
            return []
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list):
            return [str(m) for m in errors]
    return []


def _translate_api_error(exc: ApiError[object]) -> ProviderError:
    status = exc.status_code
    if status in (401, 403):
        return ProviderConfigError(502, "Billing provider refused our credentials.")
    if status == 429:
        return ProviderUnavailable(503, "Billing provider is rate limiting; try again shortly.")
    if status == 404:
        return ProviderNotFound(404, "Not found at the billing provider.")
    if status in CALLER_FAULT_STATUSES:
        return ProviderRejected(status, _messages(exc.error))
    if status >= 500:
        # A 5xx on a write may still have landed.
        return ProviderFailure(502, "Billing provider error.", outcome_unknown=True)
    return ProviderFailure(502, "Unexpected response from the billing provider.")


def translate(exc: BaseException) -> ProviderError:
    """Map any failure of an SDK call onto the ladder. Programming errors are re-raised."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ApiError):
        return _translate_api_error(exc)
    if isinstance(exc, ValidationError | ValueError):
        return ProviderUnreadable(502, "Unreadable response from the billing provider.", outcome_unknown=True)
    if isinstance(exc, NEVER_SENT):
        return ProviderUnavailable(502, "Billing provider unreachable; nothing was sent.")
    if isinstance(exc, httpx.RequestError):
        return ProviderUnavailable(504, "No response from the billing provider.", outcome_unknown=True)
    raise exc
